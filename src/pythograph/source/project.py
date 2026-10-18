"""분석 대상 프로젝트의 파일을 안전하게 찾고 읽고 파싱한다.

불변 조건:
- 분석 대상 코드는 import하거나 실행하지 않는다. 표준 라이브러리 `ast.parse`로만 읽는다.
- 심볼릭 링크는 따라가지 않는다(프로젝트 밖을 읽거나 순환하지 않기 위해서다). 순회에서는 따라갔다면 읽었을 소스를
  가릴 수 있는 링크만 세고, 이름 해석·선언 파일 읽기에서 만난 링크는 모두 센다.
- 파일 크기·순회 항목 수·깊이에 상한을 둔다. 넘거나 읽지 못한 파일은 실패로 기록해 한계로 남긴다.
- 경로는 모두 프로젝트 상대 POSIX 문자열로 다룬다. 절대 경로를 출력·오류 문구에 싣지 않는다.
- 경로는 사실의 위치와 심볼 id가 되므로, 교환 형식이 쓸 수 없는 이름(UTF-8이 아님, 계약 금지 문자)의 파일은
  건너뛰고 센다.
"""

from __future__ import annotations

import ast
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath

from pythograph.source.syntax import chain_too_long, exceeds_depth
from pythograph.source.text import is_contract_safe

#: 읽을 파이썬 파일의 최대 크기(바이트)다. 더 크면 생성물로 보고 건너뛴다.
MAX_SOURCE_BYTES = 4 * 1024 * 1024

#: 순회할 최대 디렉터리 항목 수다.
MAX_SCANNED_ENTRIES = 200_000

#: 순회할 최대 디렉터리 깊이다.
MAX_SCAN_DEPTH = 64

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


@dataclass
class Project:
    """분석 대상 프로젝트.

    Attributes:
        root: 프로젝트 realpath.
        module_roots: 모듈 이름을 파일로 풀 때 쓰는 프로젝트 상대 루트 목록(`""`는 프로젝트 루트).
        forbidden_names: 경로(디렉터리 성분 포함)에 계약 금지 문자(제어 문자·U+2028/2029)가 있어 건너뛴 파이썬 파일 수.
    """

    root: Path
    module_roots: list[str] = field(default_factory=list)
    _modules: dict[str, SourceModule | ReadFailure] = field(default_factory=dict)
    _files: list[str] | None = None
    skipped_links: int = 0
    scan_capped: bool = False
    unencodable_names: int = 0
    forbidden_names: int = 0
    scan_errors: int = 0
    _skipped_link_paths: set[str] = field(default_factory=set)

    @classmethod
    def open(cls, root: Path) -> Project:
        """프로젝트를 열고 모듈 루트를 정한다.

        모듈 루트는 `manage.py`가 있는 디렉터리들(Django가 `sys.path`에 넣는 곳), 프로젝트 루트,
        `src/` 순서다.

        Args:
            root: 프로젝트 realpath(디렉터리).

        Returns:
            프로젝트.
        """
        project = cls(root=root)
        manage_directories = sorted(
            {
                str(PurePosixPath(path).parent)
                for path in project.python_files()
                if PurePosixPath(path).name == "manage.py"
            },
            key=lambda item: (item.count("/"), item),
        )
        roots = [("" if directory == "." else directory) for directory in manage_directories]
        roots.append("")
        if project.is_directory("src"):
            roots.append("src")
        project.module_roots = list(dict.fromkeys(roots))
        return project

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
            if _link_may_hide_sources(entry):
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

        구문 트리가 `MAX_AST_DEPTH`보다 깊은 모듈도 실패다. 재귀 분석기가 파이썬 재귀 한도를 넘겨 명령 전체를 끝내지
        않도록 파서가 거부한 경우와 같은 이유로 남긴다(실행 중인 파이썬 버전과 무관하게 같은 문구). 3.10은 긴 사슬을
        파싱하다 프로세스가 죽으므로 파싱 전에 사슬 길이를 먼저 본다(`chain_too_long`).

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
        if chain_too_long(text):
            return ReadFailure(relative, "source nesting too deep to parse")
        try:
            tree = ast.parse(text, filename=PurePosixPath(relative).name)
        except (SyntaxError, ValueError):
            return ReadFailure(relative, "syntax error or syntax newer than the running Python")
        except (RecursionError, MemoryError):
            return ReadFailure(relative, "source nesting too deep to parse")
        if exceeds_depth(tree):
            return ReadFailure(relative, "source nesting too deep to parse")
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
        best = ""
        for root in self.module_roots:
            if root and (relative.startswith(root + "/")) and len(root) > len(best):
                best = root
        inner = path.relative_to(best) if best else path
        parts = list(inner.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def resolve_module(self, name: str) -> str | None:
        """점으로 이은 모듈 이름을 프로젝트 안의 파일로 푼다.

        Args:
            name: 모듈 이름(`shop.urls`).

        Returns:
            프로젝트 상대 경로, 프로젝트 안에 없으면 None(외부 모듈).
        """
        if not name or any(not part.isidentifier() for part in name.split(".")):
            return None
        relative_parts = name.replace(".", "/")
        for root in self.module_roots:
            base = f"{root}/{relative_parts}" if root else relative_parts
            # 같은 검색 루트에서는 일반 패키지가 같은 이름의 .py 모듈보다 우선한다.
            # __init__.py 없는 namespace 디렉터리는 기존 일반 모듈을 가리지 않는다.
            for candidate in (f"{base}/__init__.py", f"{base}.py"):
                if self.is_file(candidate):
                    return candidate
        return None


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


def _link_may_hide_sources(entry: os.DirEntry[str]) -> bool:
    """링크를 따라갔다면 순회가 읽었을 소스를 가리는지 판정한다. 대상 종류만 보고 들어가지는 않는다.

    순회가 들어갔을 이름의 디렉터리를 가리키거나, 이름이 `.py`인(끊어진 링크 포함) 링크만 소스를 가린다. `CLAUDE.md`·
    `README.md`·`.venv`처럼 소스와 무관한 링크까지 세면 링크 하나가 문서 전체 커버리지 한계가 되어 진단을 묻고 닫힌 세계
    `bound` 증명을 막는다. 대상 종류를 확인하지 못하면(권한·순환 링크) 가린다고 본다(과소 신고보다 안전하다).

    Args:
        entry: 링크인 디렉터리 항목.

    Returns:
        소스를 가릴 수 있으면 True.
    """
    try:
        points_to_directory = entry.is_dir(follow_symlinks=True)
    except OSError:
        return True
    if points_to_directory:
        return not _is_skipped_directory(entry.name)
    return entry.name.endswith(".py")


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
