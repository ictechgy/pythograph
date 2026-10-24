"""분석 대상 프로젝트의 파일을 안전하게 찾고 읽고 파싱한다.

불변 조건:
- 분석 대상 코드는 import하거나 실행하지 않는다. 표준 라이브러리 `ast.parse`로만 읽는다.
- 심볼릭 링크는 따라가지 않는다(프로젝트 밖을 읽거나 순환하지 않기 위해서다). 순회에서는 따라갔다면 읽었을 소스를
  가릴 수 있는 링크만 세고, 이름 해석·선언 파일 읽기에서 만난 링크는 모두 센다.
- 파일 크기·순회 항목 수·깊이와 실행 하나가 파싱하는 소스 총량에 상한을 둔다. 넘거나 읽지 못한 파일은 실패로 기록해
  한계로 남긴다. 모듈 루트로 쓰는 `manage.py` 디렉터리 수에도 상한을 두고 넘은 수를 센다.
- 경로는 모두 프로젝트 상대 POSIX 문자열로 다룬다. 절대 경로를 출력·오류 문구에 싣지 않는다.
- 경로는 사실의 위치와 심볼 id가 되므로, 교환 형식이 쓸 수 없는 이름(UTF-8이 아님, 계약 금지 문자)의 파일은
  건너뛰고 센다.
"""

from __future__ import annotations

import ast
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath

from pythograph.source.syntax import MAX_AST_DEPTH, chain_too_long, tree_overflow
from pythograph.source.text import is_contract_safe

#: 읽을 파이썬 파일의 최대 크기(바이트)다. 더 크면 생성물로 보고 건너뛴다.
MAX_SOURCE_BYTES = 4 * 1024 * 1024

#: 실행 하나가 파싱하는 파이썬 소스의 총량(바이트, 파싱에 성공한 모듈만 센다)이다. 파싱한 구문 트리는 이름 해석이
#: 언제든 다시 쓰므로 명령이 끝날 때까지 모두 남고, 메모리는 소스 바이트에 비례한다. 실측(CPython 3.13): 보통 코드의
#: 트리는 바이트당 약 25~30 B이고 명령의 최대 RSS는 바이트당 약 30~55 B다(sympy 26.2 MB·1,532파일의 graph 0.96 GB,
#: Django 5.7 MB의 graph 0.33 GB). 64 MiB는 실측한 가장 큰 실제 프로젝트(sympy)의 2.5배이고, 보통 코드면 최대 RSS가
#: 약 2~4 GB라 작은 CI 러너(7 GB)에 들어간다. 리터럴 위주 소스(`DATA = [0, 1, …]`, `x = 1` 반복)는 트리와 부모 사전만
#: 바이트당 약 220~350 B라 예산을 채우면 15~25 GB가 될 수 있으므로, 그런 입력의 메모리 상한은 OS 제한이
#: 맡는다(README). 넘게 할 모듈은 파싱하지 않고 읽지 못한 소스로 남긴다(`source_budget_reason`).
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024

#: 모듈 하나의 구문 트리가 가질 수 있는 최대 노드 수다(문맥·연산자 잎 노드 포함). 파싱한 트리는 명령이 끝날 때까지
#: 남고 노드 하나가 트리와 부모 사전에 약 440 B를 쓰므로, 바이트 예산 안에서도 리터럴만 빽빽한 모듈은 트리가 원문의
#: 수백 배가 된다(`x = 1` 반복은 바이트당 약 0.7 노드라 4 MiB 파일 하나가 약 280만 노드·약 0.9 GB를 남긴다). 실측
#: (CPython 3.13, 설치본의 150 KB·표준 라이브러리의 100 KB가 넘는 파일 551개): 가장 큰 실제 모듈이 약 20만 노드다(sympy
#: `polys/benchmarks/bench_solvers.py` 446 KB. torch `testing/_internal/common_methods_invocations.py` 1.3 MB는 16만,
#: faker 주소 데이터 모듈 8.8만). 100만은 그 5배라 실제 모듈은 걸리지 않고, 넘는 모듈은 트리를 버리고 읽지 못한 소스로
#: 남긴다(`syntax_nodes_reason`). 그래서 모듈 하나가 남기는 트리는 약 0.45 GB까지다. 파싱하는 동안의 봉우리는 파일 크기
#: 상한(4 MiB)이 묶고, 상한 아래 모듈을 여러 개로 나눈 입력은 바이트 예산만 묶는다(README).
MAX_MODULE_SYNTAX_NODES = 1_000_000

#: 순회할 최대 디렉터리 항목 수다.
MAX_SCANNED_ENTRIES = 200_000

#: 순회할 최대 디렉터리 깊이다.
MAX_SCAN_DEPTH = 64

#: 모듈 루트로 쓰는 `manage.py` 디렉터리의 최대 수다((깊이, 경로) 순 앞에서부터). Django `startproject`는 프로젝트마다
#: `manage.py`를 하나 만들고, 서비스 여럿을 한 저장소에 둔 경우도 서비스 수만큼이다(Flask 앱은 없다).
#: 루트 항목 색인 뒤에도 모든 루트에 같은 첫 성분(`pkg/`)을 두면 이름 하나를 풀 때 루트마다 후보를 검사해야 하므로,
#: 루트 수가 곧 이름당 비용의 배수다(색인을 넣은 뒤에도 빈 `manage.py` 1,024개에서 고유 이름 400개를 푸는 graph가
#: 26초). 256은 실제 저장소보다 훨씬 넉넉하면서 그 배수를 묶는다. 넘은 디렉터리 수는 `Project.skipped_module_roots`로
#: 세어 명령마다 한계로 남긴다.
MAX_MODULE_ROOTS = 256

#: 지원하는 운영체제에서는 디렉터리 핸들에 상대적으로 열어 링크 교체 경쟁을 막는다.
_HAS_DIR_FD = (
    os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)

#: 순회에서 건너뛰는 디렉터리 이름이다(가상 환경·캐시·빌드 산출물·의존성).
SKIPPED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "site-packages",
        "venv",
        "env",
        "build",
        "dist",
        "htmlcov",
    }
)


@dataclass(frozen=True)
class SourceModule:
    """파싱한 파이썬 모듈 하나.

    Attributes:
        path: 프로젝트 상대 POSIX 경로.
        name: 점으로 이은 모듈 이름(모듈 루트 기준). 알 수 없으면 파일 경로에서 만든 이름.
        tree: `ast.Module`.
        has_bom: 파일이 UTF-8 BOM으로 시작했는지(첫 줄 열 보정용).
        is_package: `__init__.py`인지.
    """

    path: str
    name: str
    tree: ast.Module
    has_bom: bool
    is_package: bool


@dataclass(frozen=True)
class ReadFailure:
    """모듈을 읽거나 파싱하지 못한 이유. 문구에는 원인 종류만 싣는다."""

    path: str
    reason: str


@dataclass(frozen=True)
class SourceOmission:
    """캡처 단계에서 제외한 원본 경로와 이유다. 실제 파일 순회 통계와 구분한다."""

    path: str
    reason: str
    may_hide_source: bool


@dataclass
class Project:
    """분석 대상 프로젝트.

    Attributes:
        root: 프로젝트 realpath.
        module_roots: 모듈 이름을 파일로 풀 때 쓰는 프로젝트 상대 루트 목록(`""`는 프로젝트 루트). 제자리에서 바꿔도
            된다(`resolve_module`·`module_name`이 루트에 묶인 기억을 버린다).
        max_depth: 분석하는 구문 트리의 최대 중첩 깊이(넘으면 읽지 못한 소스). 명령마다 재귀가 쓰는 프레임이 달라
            명령이 고른다(`syntax.MAX_AST_DEPTH`·`syntax.MAX_EXTRACTION_AST_DEPTH`).
        forbidden_names: 경로(디렉터리 성분 포함)에 계약 금지 문자(제어 문자·U+2028/2029)가 있어 건너뛴 파이썬 파일 수.
        skipped_module_roots: 상한(`MAX_MODULE_ROOTS`) 밖이라 모듈 루트로 쓰지 않은 `manage.py` 디렉터리 수.
        parsed_source_bytes: 지금까지 파싱에 성공한 모듈의 소스 바이트 합(`MAX_TOTAL_SOURCE_BYTES`와 비교한다).
        budget_skipped_modules: 파싱 총량 예산을 넘게 해 파싱하지 않은 모듈 수.
    """

    root: Path
    module_roots: list[str] = field(default_factory=list)
    max_depth: int = MAX_AST_DEPTH
    _modules: dict[str, SourceModule | ReadFailure] = field(default_factory=dict)
    _files: list[str] | None = None
    skipped_links: int = 0
    scan_capped: bool = False
    unencodable_names: int = 0
    forbidden_names: int = 0
    scan_errors: int = 0
    skipped_module_roots: int = 0
    parsed_source_bytes: int = 0
    budget_skipped_modules: int = 0
    _skipped_link_paths: set[str] = field(default_factory=set)
    _resolved_modules: dict[str, str | None] = field(default_factory=dict)
    _resolved_roots: list[str] = field(default_factory=list)
    _root_entries: dict[str, frozenset[str] | None] = field(default_factory=dict)
    _root_names: frozenset[str] = field(default_factory=frozenset)
    source_omissions: tuple[SourceOmission, ...] = ()

    @classmethod
    def open(
        cls, root: Path, max_depth: int = MAX_AST_DEPTH, *, source_omissions: tuple[SourceOmission, ...] = ()
    ) -> Project:
        """프로젝트를 열고 모듈 루트를 정한다.

        모듈 루트는 `manage.py`가 있는 디렉터리들(Django가 `sys.path`에 넣는 곳, (깊이, 경로) 순 앞
        `MAX_MODULE_ROOTS`개), 프로젝트 루트, `src/` 순서다. 상한 밖 디렉터리 수는 `skipped_module_roots`로 센다.

        Args:
            root: 프로젝트 realpath(디렉터리).
            max_depth: 분석하는 구문 트리의 최대 중첩 깊이(기본은 graph 계열 상한).
            source_omissions: 별도 캡처가 제외한 입력이다. 새 스캔이 못 보는 원래 소스 경계를 보존한다.

        Returns:
            프로젝트.
        """
        project = cls(root=root, max_depth=max_depth, source_omissions=source_omissions)
        manage_directories = project._manage_directories()
        project.skipped_module_roots = max(len(manage_directories) - MAX_MODULE_ROOTS, 0)
        roots = [("" if directory == "." else directory) for directory in manage_directories[:MAX_MODULE_ROOTS]]
        roots.append("")
        if project.is_directory("src"):
            roots.append("src")
        project.module_roots = list(dict.fromkeys(roots))
        return project

    def _manage_directories(self) -> list[str]:
        """`manage.py`가 있는 디렉터리를 (깊이, 경로) 순으로 돌려준다(프로젝트 루트는 `.`).

        Returns:
            프로젝트 상대 디렉터리 목록.
        """
        return sorted(
            {
                str(PurePosixPath(path).parent)
                for path in self.python_files()
                if PurePosixPath(path).name == "manage.py"
            },
            key=lambda item: (item.count("/"), item),
        )

    def python_files(self) -> list[str]:
        """프로젝트의 파이썬 파일을 프로젝트 상대 경로로, 정렬해 돌려준다.

        Returns:
            `.py` 파일 경로 목록.
        """
        if self._files is None:
            self._files = sorted(self._walk())
        return self._files

    def _walk(self) -> list[str]:
        """디렉터리를 상한 안에서 순회한다. 심볼릭 링크와 건너뛸 디렉터리는 들어가지 않는다.

        Returns:
            발견한 `.py` 파일의 프로젝트 상대 경로.
        """
        found: list[str] = []
        pending: list[tuple[str, int]] = [("", 0)]
        scanned = 0
        while pending:
            directory, depth = pending.pop()
            remaining = MAX_SCANNED_ENTRIES - scanned
            entries: list[os.DirEntry[str]] = []
            try:
                with self.scan_directory(directory) as iterator:
                    for entry in islice(iterator, remaining + 1):
                        scanned += 1
                        entries.append(entry)
                    if scanned > MAX_SCANNED_ENTRIES:
                        # 파일시스템 열거 순서는 안정적이지 않다. 상한을 넘은 디렉터리는
                        # 임의의 접두사만 분석하지 않고 전부 제외해 부분 사실을 꾸미지 않는다.
                        self.scan_capped = True
                        return found
                    for entry in sorted(entries, key=lambda entry: entry.name):
                        try:
                            self._visit(entry, directory, depth, pending, found)
                        except OSError:
                            self.scan_errors += 1
            except OSError:
                self.scan_errors += 1
                continue
        return found

    def _visit(
        self, entry: os.DirEntry[str], directory: str, depth: int, pending: list[tuple[str, int]], found: list[str]
    ) -> None:
        """순회 항목 하나를 분류한다. 링크는 따라가지 않고 소스를 가릴 수 있을 때만 센다(`_link_may_hide_sources`).

        Args:
            entry: 디렉터리 항목.
            directory: 부모의 프로젝트 상대 경로.
            depth: 부모 깊이.
            pending: 더 들어갈 디렉터리 목록(수정한다).
            found: 발견한 파일 목록(수정한다).
        """
        relative = PurePosixPath(directory, entry.name).as_posix()
        if _is_link(entry.stat(follow_symlinks=False)):
            if _link_may_hide_sources(entry.name):
                self._record_link(relative)
            return
        if entry.is_dir(follow_symlinks=False):
            if _is_skipped_directory(entry.name):
                return
            if depth + 1 > MAX_SCAN_DEPTH:
                self.scan_capped = True
                return
            pending.append((relative, depth + 1))
        elif entry.name.endswith(".py") and entry.is_file(follow_symlinks=False):
            self._collect(relative, found)

    def _collect(self, relative: str, found: list[str]) -> None:
        """파이썬 파일을 발견 목록에 넣는다. 교환 형식에 실을 수 없는 경로는 건너뛰고 센다.

        경로는 모든 사실의 `location.path`와 심볼 id(`경로#이름`)가 된다. 계약이 금지하는 문자가 든 경로를 실으면
        소비자가 문서 전체를 거부하므로, 그 파일만 빼고 한계로 남긴다.

        Args:
            relative: 파일의 프로젝트 상대 경로.
            found: 발견한 파일 목록(수정한다).
        """
        if not _is_encodable(relative):
            # UTF-8이 아닌 파일 이름은 교환 형식에 쓸 수 없다. 건너뛰고 센다.
            self.unencodable_names += 1
        elif not is_contract_safe(relative):
            # 제어 문자(C0·C1, NEL)·U+2028/2029가 든 경로는 사실의 경로·usr로 실을 수 없다. 건너뛰고 센다.
            self.forbidden_names += 1
        else:
            found.append(relative)

    def is_directory(self, relative: str) -> bool:
        """프로젝트 상대 경로가 링크가 아닌 디렉터리인지 돌려준다.

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            디렉터리면 True.
        """
        try:
            mode = self._checked_stat(relative).st_mode
        except OSError:
            return False
        return stat.S_ISDIR(mode)

    def is_file(self, relative: str) -> bool:
        """프로젝트 상대 경로가 링크가 아닌 일반 파일인지 돌려준다.

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            일반 파일이면 True.
        """
        try:
            mode = self._checked_stat(relative).st_mode
        except OSError:
            return False
        return stat.S_ISREG(mode)

    def read_text(self, relative: str, limit: int = MAX_SOURCE_BYTES) -> str | None:
        """링크가 아닌 작은 UTF-8 텍스트 파일을 읽는다.

        Args:
            relative: 프로젝트 상대 경로.
            limit: 최대 바이트 수.

        Returns:
            내용(BOM 제거), 읽을 수 없으면 None.
        """
        if limit < 0:
            return None
        try:
            data = self._read_bytes(relative, limit)
        except OSError:
            return None
        if len(data) > limit:
            return None
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None

    def module(self, relative: str) -> SourceModule | ReadFailure:
        """파이썬 파일을 파싱한다(캐시).

        Args:
            relative: 프로젝트 상대 `.py` 경로.

        Returns:
            파싱한 모듈 또는 실패.
        """
        cached = self._modules.get(relative)
        if cached is None:
            cached = self._parse(relative)
            self._modules[relative] = cached
        return cached

    def parsed_modules(self) -> dict[str, SourceModule | ReadFailure]:
        """지금까지 파싱을 시도한 모듈과 결과를 돌려준다.

        Returns:
            경로 → 모듈 또는 실패.
        """
        return dict(self._modules)

    def _parse(self, relative: str) -> SourceModule | ReadFailure:
        """파일을 읽어 `ast`로 파싱한다. 어떤 실패도 예외로 새지 않게 이유로 바꾼다.

        구문 트리가 `max_depth`보다 깊은 모듈도 실패다. 재귀 분석기가 파이썬 재귀 한도를 넘겨 명령 전체를 끝내지
        않도록 파서가 거부한 경우와 같은 이유로 남긴다(실행 중인 파이썬 버전과 무관하게 같은 문구). 3.10은 긴 사슬을
        파싱하다 프로세스가 죽으므로 파싱 전에 사슬 길이를 먼저 본다(`chain_too_long`).

        파싱에 성공한 모듈의 바이트 합이 `MAX_TOTAL_SOURCE_BYTES`를 넘게 할 모듈은 파서를 부르지 않고 실패로 남긴다.
        조밀한 소스는 파싱하는 동안 메모리를 쓰므로 파싱 전에 보고, 3.10만 하는 사슬 판정보다 먼저 보므로 예산을 넘는
        모듈의 이유는 버전과 무관하다. 트리를 남기지 않는 실패(문법 오류·깊은 중첩·노드 수 초과)는 예산을 쓰지 않는다.
        파싱 순서는 명령마다 정해져 있어(경로 순, import로 먼저 닿은 모듈) 같은 입력이면 같은 모듈이 빠진다. 노드가
        `MAX_MODULE_SYNTAX_NODES`보다 많은 트리는 깊이를 재는 같은 순회에서 세어 버린다(남기지 않는다).

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            파싱한 모듈 또는 실패.
        """
        try:
            data = self._read_bytes(relative, MAX_SOURCE_BYTES)
        except OSError:
            return ReadFailure(relative, "unreadable file")
        if len(data) > MAX_SOURCE_BYTES:
            return ReadFailure(relative, "file larger than 4 MiB")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ReadFailure(relative, "file is not valid UTF-8")
        if self.parsed_source_bytes + len(data) > MAX_TOTAL_SOURCE_BYTES:
            self.budget_skipped_modules += 1
            return ReadFailure(relative, source_budget_reason())
        if chain_too_long(text):
            return ReadFailure(relative, "source nesting too deep to parse")
        try:
            tree = ast.parse(text, filename=PurePosixPath(relative).name)
        except (SyntaxError, ValueError):
            return ReadFailure(relative, "syntax error or syntax newer than the running Python")
        except (RecursionError, MemoryError):
            return ReadFailure(relative, "source nesting too deep to parse")
        overflow = tree_overflow(tree, self.max_depth, MAX_MODULE_SYNTAX_NODES)
        if overflow == "depth":
            return ReadFailure(relative, "source nesting too deep to parse")
        if overflow == "nodes":
            return ReadFailure(relative, syntax_nodes_reason())
        self.parsed_source_bytes += len(data)
        return SourceModule(
            path=relative,
            name=self.module_name(relative),
            tree=tree,
            has_bom=data.startswith(b"\xef\xbb\xbf"),
            is_package=PurePosixPath(relative).name == "__init__.py",
        )

    def _record_link(self, relative: str) -> None:
        """순회와 이름 해석에서 거부한 같은 링크를 중복 없이 센다."""
        if relative not in self._skipped_link_paths:
            self._skipped_link_paths.add(relative)
            self.skipped_links += 1

    def _checked_stat(self, relative: str) -> os.stat_result:
        """루트부터 모든 경로 성분을 lstat해 링크·상위 경로·비디렉터리 부모를 거부한다."""
        if not _inside(relative):
            raise OSError("not a project-relative path")
        path = self.root
        parts = PurePosixPath(relative).parts
        for index in range(len(parts) + 1):
            info = path.lstat()
            if _is_link(info):
                self._record_link(PurePosixPath(*parts[:index]).as_posix())
                raise OSError("symbolic link is not followed")
            if index == len(parts):
                return info
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("parent is not a directory")
            path /= parts[index]
        raise AssertionError("path has a final component")

    @contextmanager
    def _open_directory(self, relative: str) -> Iterator[int | Path]:
        """부모 핸들 기준 O_NOFOLLOW로 디렉터리를 연다. 미지원 환경은 경로를 재검사한다."""
        expected = self._checked_stat(relative)
        if not stat.S_ISDIR(expected.st_mode):
            raise OSError("not a directory")
        if not _HAS_DIR_FD:
            yield self.root / relative
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        try:
            for part in PurePosixPath(relative).parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if not os.path.samestat(expected, os.fstat(descriptor)):
                raise OSError("directory changed while opening")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def scan_directory(self, relative: str) -> Iterator[Iterator[os.DirEntry[str]]]:
        """검증한 디렉터리만 열거하고 항목을 분류할 때까지 핸들을 유지한다."""
        with self._open_directory(relative) as directory, os.scandir(directory) as entries:
            yield entries

    def _read_bytes(self, relative: str, limit: int) -> bytes:
        """검증한 일반 파일 핸들에서만 제한된 바이트를 읽어 검사·열기 사이 링크 교체를 막는다."""
        expected = self._checked_stat(relative)
        if not stat.S_ISREG(expected.st_mode):
            raise OSError("not a regular file")
        path = PurePosixPath(relative)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with self._open_directory(path.parent.as_posix()) as directory:
            if isinstance(directory, int):
                descriptor = os.open(path.name, flags, dir_fd=directory)
            else:
                descriptor = os.open(directory / path.name, flags)
            with os.fdopen(descriptor, "rb") as source:
                actual = os.fstat(source.fileno())
                if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(expected, actual):
                    raise OSError("file changed while opening")
                if not os.path.samestat(actual, self._checked_stat(relative)):
                    raise OSError("path changed while opening")
                return source.read(limit + 1)

    def module_name(self, relative: str) -> str:
        """파일 경로에서 모듈 이름을 만든다. 파일을 담은 가장 긴 모듈 루트를 기준으로 한다.

        Args:
            relative: 프로젝트 상대 `.py` 경로.

        Returns:
            점으로 이은 모듈 이름(`__init__`은 패키지 이름).
        """
        path = PurePosixPath(relative)
        best = self._longest_root(relative)
        inner = path.relative_to(best) if best else path
        parts = list(inner.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def _longest_root(self, relative: str) -> str:
        """경로를 담은(`<루트>/`로 시작하는) 가장 긴 비어 있지 않은 모듈 루트를 찾는다.

        모듈 루트를 모두 훑지 않고, 경로의 `/` 앞부분을 긴 것부터 루트 집합에 대조한다. 파싱한 파일마다 루트 수만큼
        비교하면 `manage.py`가 많은 저장소에서 파일 수 × 루트 수가 든다. 문자열로 자르므로 정규화하지 않은 루트·경로도
        예전(`relative.startswith(root + "/")` 중 가장 긴 루트)과 같은 결과다.

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            가장 긴 루트, 없으면 `""`(프로젝트 루트).
        """
        self._refresh_roots()
        end = relative.rfind("/")
        while end > 0:
            if relative[:end] in self._root_names:
                return relative[:end]
            end = relative.rfind("/", 0, end)
        return ""

    def _refresh_roots(self) -> None:
        """모듈 루트가 바뀌었으면(제자리 수정 포함) 루트에 묶인 기억을 버린다.

        버리는 것: 이름별 해석 결과, 루트 항목 색인, 비어 있지 않은 루트 집합. 목록 비교는 같은 문자열 객체끼리라
        포인터 비교로 끝난다.
        """
        if self._resolved_roots != self.module_roots:
            self._resolved_modules.clear()
            self._root_entries.clear()
            self._resolved_roots = list(self.module_roots)
            self._root_names = frozenset(root for root in self._resolved_roots if root)

    def resolve_module(self, name: str) -> str | None:
        """점으로 이은 모듈 이름을 프로젝트 안의 파일로 푼다. 한 실행 안에서 이름별 결과(None 포함)를 기억한다.

        이름 해석은 소스에 이름이 나올 때마다 불린다. 기억하지 않으면 `manage.py`가 여러 개인 저장소에서 같은 외부
        이름을 수십만 번 다시 찾는다. 분석은 안정된 트리를 전제하고, 경계는 해석이 아니라 읽기(`_read_bytes`·
        `_open_directory`의 열기 시점 재검사)가 지키므로 기억해도 링크를 따라가지 않는다. 처음 찾을 때 만난 링크는 그때
        센다(같은 링크는 한 번만 센다). 모듈 루트가 바뀌면 기억을 버린다.

        Args:
            name: 모듈 이름(`shop.urls`).

        Returns:
            프로젝트 상대 경로, 프로젝트 안에 없으면 None(외부 모듈).
        """
        self._refresh_roots()
        if name not in self._resolved_modules:
            self._resolved_modules[name] = self._find_module(name)
        return self._resolved_modules[name]

    def _find_module(self, name: str) -> str | None:
        """모듈 루트 순서대로 이름의 패키지·모듈 파일을 찾는다(`resolve_module`이 결과를 기억한다).

        루트 바로 아래에 이름의 첫 성분이 없는 루트는 후보를 검사하지 않는다(`_root_may_hold`). 그런 루트의 후보 lstat은
        첫 성분에서 없는 경로로 끝나 링크를 세지 않으므로 건너뛰어도 결과·링크 수가 같다. 고유 이름마다 모든 루트 ×
        후보 2개의 성분별 lstat을 하던 것이, 첫 성분이 있는 루트에서만 일어난다(나머지 루트는 집합 조회 한 번).

        Args:
            name: 모듈 이름(`shop.urls`).

        Returns:
            프로젝트 상대 경로, 프로젝트 안에 없으면 None(외부 모듈).
        """
        if not name or any(not part.isidentifier() for part in name.split(".")):
            return None
        relative_parts = name.replace(".", "/")
        keys = _first_component_keys(name)
        for root in self.module_roots:
            if not self._root_may_hold(root, keys):
                continue
            base = f"{root}/{relative_parts}" if root else relative_parts
            # 같은 검색 루트에서는 일반 패키지가 같은 이름의 .py 모듈보다 우선한다.
            # __init__.py 없는 namespace 디렉터리는 기존 일반 모듈을 가리지 않는다.
            for candidate in (f"{base}/__init__.py", f"{base}.py"):
                if self.is_file(candidate):
                    return candidate
        return None

    def _root_may_hold(self, root: str, keys: frozenset[str]) -> bool:
        """루트 항목 색인으로 그 루트 바로 아래에 이름의 첫 성분이 있을 수 있는지 본다.

        색인은 해석이 그 루트에 처음 닿을 때 만든다. 예전 해석도 그때 처음 그 루트의 후보를 lstat했으므로, 루트 경로의
        링크를 세는 시점·경로가 같다.

        Args:
            root: 모듈 루트.
            keys: 첫 성분의 비교 키(`_first_component_keys`).

        Returns:
            있을 수 있으면 True. 색인을 만들지 못한 루트는 늘 True(후보를 모두 검사한다).
        """
        if root not in self._root_entries:
            self._root_entries[root] = self._root_entry_keys(root)
        entries = self._root_entries[root]
        return entries is None or not entries.isdisjoint(keys)

    def _root_entry_keys(self, root: str) -> frozenset[str] | None:
        """루트 디렉터리의 항목 이름을 한 번 읽어 비교 키 집합으로 만든다.

        링크·건너뛰는 이름의 항목도 넣는다(후보 검사가 그 링크를 예전처럼 센다). 루트 경로 성분의 링크는 목록을 열면서
        세며, 예전 후보 검사가 같은 루트에서 세던 경로와 같다.

        Args:
            root: 모듈 루트.

        Returns:
            키 집합. 루트가 디렉터리가 아니거나 링크 아래이거나 목록을 읽지 못하거나 항목이 순회 상한보다 많으면 None.
        """
        try:
            with self.scan_directory(root) as entries:
                names = [entry.name for entry in islice(entries, MAX_SCANNED_ENTRIES + 1)]
        except OSError:
            # 없는 루트·파일인 루트·링크 아래 루트·읽기 권한이 없는 루트다. 색인 없이 예전처럼 후보를 모두 검사한다.
            return None
        if len(names) > MAX_SCANNED_ENTRIES:
            return None
        return frozenset(_entry_key(name) for name in names)


def skipped_roots_reason(count: int) -> str:
    """모듈 루트로 쓰지 않은 `manage.py` 디렉터리 수를 한계 문장(접두사 뒤)으로 만든다. 명령마다 접두사만 다르다.

    상한 밖 디렉터리의 코드가 그 디렉터리 기준으로 쓰는 import는 풀리지 않아 외부로 보인다. 빈 결과를 완전하다고 읽지
    않게 원인(상한)·영향·방향(`--project`를 좁혀 따로 분석)을 함께 싣는다. 상한은 부르는 시점의 값을 쓴다.

    Args:
        count: 쓰지 않은 디렉터리 수(`Project.skipped_module_roots`).

    Returns:
        한계 문장.
    """
    return (
        f"{count} directories containing manage.py are not used as module roots (only the {MAX_MODULE_ROOTS} "
        "shallowest are), so imports that only they would resolve are treated as external; pass a narrower "
        "--project to analyze them"
    )


def source_budget_reason() -> str:
    """파싱 총량 예산을 넘게 해 파싱하지 않은 모듈의 실패 이유를 만든다(명령마다 "could not be analyzed: " 뒤에 싣는다).

    원인(실행 하나의 파싱 총량 상한)과 방향(`--project`를 좁혀 나눠 분석)을 함께 싣는다. 상한은 부르는 시점의 값을 쓴다.

    Returns:
        실패 이유 문구.
    """
    return (
        f"the run's total parsed source would exceed its {_size_text(MAX_TOTAL_SOURCE_BYTES)} budget; "
        "pass a narrower --project"
    )


def syntax_nodes_reason() -> str:
    """구문 트리 노드가 상한보다 많아 분석하지 않은 모듈의 실패 이유다(명령마다 "could not be analyzed: " 뒤에 싣는다).

    파일 크기 상한의 이유("file larger than 4 MiB")와 같은 꼴로 원인(상한)을 싣는다. 상한은 부르는 시점의 값을 쓴다.

    Returns:
        실패 이유 문구.
    """
    return f"syntax tree larger than {MAX_MODULE_SYNTAX_NODES:,} nodes"


def _size_text(size: int) -> str:
    """바이트 수를 한계 문구에 쓰는 크기로 바꾼다(MiB의 배수면 MiB, 아니면 바이트).

    Args:
        size: 바이트 수.

    Returns:
        `64 MiB`·`1,000 bytes` 같은 문구.
    """
    mebibyte = 1024 * 1024
    return f"{size // mebibyte} MiB" if size % mebibyte == 0 else f"{size:,} bytes"


def _entry_key(name: str) -> str:
    """디렉터리 항목 이름의 비교 키를 만든다(유니코드 정규 대소문자 무시 비교: NFD → casefold → NFD).

    루트 항목 색인은 결과를 바꾸지 않는 사전 거름이어야 한다. macOS 기본 APFS처럼 대소문자·정규화를 무시하는
    파일시스템에서는 lstat이 `Shop`과 `shop`, NFC와 NFD 철자를 같은 항목으로 찾으므로(`ß`↔`ss`, 켈빈 기호↔`k`도
    같다), 정확한 이름으로 거르면 예전에 풀리던 이름을 놓친다. 키가 같아 더 검사하는 쪽은 후보 lstat이 결과를 정하므로
    해롭지 않다.

    Args:
        name: 항목 이름.

    Returns:
        비교 키.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _first_component_keys(name: str) -> frozenset[str]:
    """이름의 후보 경로가 모듈 루트 바로 아래에서 거치는 첫 성분의 비교 키를 만든다.

    `a.b`의 두 후보(`a/b/__init__.py`·`a/b.py`)는 `a`를 거치고, 한 조각 `a`의 후보는 `a`(패키지)와
    `a.py`(모듈)를 거친다.

    Args:
        name: 점으로 이은 모듈 이름.

    Returns:
        키 집합.
    """
    first, separator, _ = name.partition(".")
    if separator:
        return frozenset({_entry_key(first)})
    return frozenset({_entry_key(first), _entry_key(f"{first}.py")})


def _inside(relative: str) -> bool:
    """플랫폼마다 다르게 해석될 경로와 `..`를 거부한다. 링크 검사는 별도로 한다.

    Args:
        relative: 상대 경로.

    Returns:
        안이면 True.
    """
    return (
        "\x00" not in relative
        and "\\" not in relative
        and ".." not in PurePosixPath(relative).parts
        and not PurePosixPath(relative).is_absolute()
        and not PureWindowsPath(relative).drive
    )


def _is_skipped_directory(name: str) -> bool:
    """순회가 들어가지 않는 디렉터리 이름(점 이름, 가상 환경·캐시·빌드 산출물·의존성)인지 판정한다.

    Args:
        name: 디렉터리 항목 이름.

    Returns:
        건너뛰면 True.
    """
    return name.startswith(".") or name in SKIPPED_DIRECTORIES


def _link_may_hide_sources(name: str) -> bool:
    """링크를 따라갔다면 순회가 읽었을 소스를 가릴 수 있는지 링크 이름만 보고 판정한다. 대상에는 접근하지 않는다.

    이름이 `.py`인 링크(끊어진 링크 포함)는 파일 소스를, 확장자가 없고 순회가 건너뛰지 않는 이름의 링크는 들어갔을
    디렉터리를 가릴 수 있다고 본다. `CLAUDE.md`·`README.md`처럼 확장자가 있는 이름은 파일 링크로 보고, `.venv`·
    `node_modules`처럼 건너뛰는 이름은 디렉터리여도 순회가 들어가지 않으므로 세지 않는다. 소스와 무관한 링크까지 세면
    링크 하나가 문서 전체 커버리지 한계가 되어 진단을 묻고 닫힌 세계 `bound` 증명을 막는다.

    대상을 stat해 종류를 고르지 않는 이유: 프로젝트 밖 대상의 존재와 종류가 한계 수로 드러나(`probe -> /usr/bin`은
    1, `-> /nonexistent`는 0) 출력을 보는 사람이 분석 머신의 경로를 알아낼 수 있고, 링크를 따라가지 않는다는 원칙과도
    어긋난다. 그 대가로 확장자 있는 이름의 디렉터리 링크(`conf.d`)는 세지 않고, 확장자 없는 파일 링크(`LICENSE`)는 센다.

    Args:
        name: 링크인 디렉터리 항목의 이름.

    Returns:
        소스를 가릴 수 있으면 True.
    """
    if name.endswith(".py"):
        return True
    return "." not in name and not _is_skipped_directory(name)


def _is_link(info: os.stat_result) -> bool:
    """심볼릭 링크와 Windows 재분석 지점(디렉터리 junction 포함)을 거부한다."""
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse_point)


def _is_encodable(text: str) -> bool:
    """문자열을 UTF-8로 쓸 수 있는지(짝 없는 서로게이트가 없는지) 확인한다.

    Args:
        text: 검사할 문자열.

    Returns:
        쓸 수 있으면 True.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_test_path(relative: str) -> bool:
    """테스트 소스로 보는 경로인지 판정한다.

    규칙(pytest·unittest 관례): 파일 이름이 `test_*.py`·`*_test.py`·`tests.py`·`conftest.py`이거나
    경로에 `tests`·`test` 디렉터리가 있다.

    Args:
        relative: 프로젝트 상대 경로.

    Returns:
        테스트 소스면 True.
    """
    path = PurePosixPath(relative)
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py") or name in ("tests.py", "conftest.py"):
        return True
    return any(part in ("tests", "test") for part in path.parts[:-1])
