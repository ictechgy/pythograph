"""분석 대상 프로젝트의 파일을 안전하게 찾고 읽고 파싱한다.

불변 조건:
- 분석 대상 코드는 import하거나 실행하지 않는다. 표준 라이브러리 `ast.parse`로만 읽는다.
- 심볼릭 링크는 따라가지 않는다(프로젝트 밖을 읽거나 순환하지 않기 위해서다). 건너뛴 것은 센다.
- 파일 크기·순회 항목 수·깊이에 상한을 둔다. 넘거나 읽지 못한 파일은 실패로 기록해 한계로 남긴다.
- 경로는 모두 프로젝트 상대 POSIX 문자열로 다룬다. 절대 경로를 출력·오류 문구에 싣지 않는다.
"""

from __future__ import annotations

import ast
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: 읽을 파이썬 파일의 최대 크기(바이트)다. 더 크면 생성물로 보고 건너뛴다.
MAX_SOURCE_BYTES = 4 * 1024 * 1024

#: 순회할 최대 디렉터리 항목 수다.
MAX_SCANNED_ENTRIES = 200_000

#: 순회할 최대 디렉터리 깊이다.
MAX_SCAN_DEPTH = 64

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
    """

    root: Path
    module_roots: list[str] = field(default_factory=list)
    _modules: dict[str, SourceModule | ReadFailure] = field(default_factory=dict)
    _files: list[str] | None = None
    skipped_links: int = 0
    scan_capped: bool = False
    unencodable_names: int = 0

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
        pending: list[tuple[Path, int]] = [(self.root, 0)]
        scanned = 0
        while pending:
            directory, depth = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCANNED_ENTRIES:
                    self.scan_capped = True
                    return found
                self._visit(entry, depth, pending, found)
        return found

    def _visit(self, entry: os.DirEntry[str], depth: int, pending: list[tuple[Path, int]], found: list[str]) -> None:
        """순회 항목 하나를 분류한다.

        Args:
            entry: 디렉터리 항목.
            depth: 부모 깊이.
            pending: 더 들어갈 디렉터리 목록(수정한다).
            found: 발견한 파일 목록(수정한다).
        """
        if entry.is_symlink():
            self.skipped_links += 1
            return
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith(".") or entry.name in SKIPPED_DIRECTORIES:
                return
            if depth + 1 > MAX_SCAN_DEPTH:
                self.scan_capped = True
                return
            pending.append((Path(entry.path), depth + 1))
        elif entry.name.endswith(".py") and entry.is_file(follow_symlinks=False):
            relative = Path(entry.path).relative_to(self.root).as_posix()
            if _is_encodable(relative):
                found.append(relative)
            else:
                # UTF-8이 아닌 파일 이름은 교환 형식에 쓸 수 없다. 건너뛰고 센다.
                self.unencodable_names += 1

    def is_directory(self, relative: str) -> bool:
        """프로젝트 상대 경로가 링크가 아닌 디렉터리인지 돌려준다.

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            디렉터리면 True.
        """
        try:
            mode = os.lstat(self.root / relative).st_mode
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
            mode = os.lstat(self.root / relative).st_mode
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
        if not self.is_file(relative) or not _inside(self.root, relative):
            return None
        try:
            data = (self.root / relative).read_bytes()[: limit + 1]
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

        Args:
            relative: 프로젝트 상대 경로.

        Returns:
            파싱한 모듈 또는 실패.
        """
        if not self.is_file(relative) or not _inside(self.root, relative):
            return ReadFailure(relative, "unreadable or not a regular file")
        try:
            data = (self.root / relative).read_bytes()[: MAX_SOURCE_BYTES + 1]
        except OSError:
            return ReadFailure(relative, "unreadable file")
        if len(data) > MAX_SOURCE_BYTES:
            return ReadFailure(relative, "file larger than 4 MiB")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ReadFailure(relative, "file is not valid UTF-8")
        try:
            tree = ast.parse(text, filename=PurePosixPath(relative).name)
        except (SyntaxError, ValueError):
            return ReadFailure(relative, "syntax error or syntax newer than the running Python")
        except (RecursionError, MemoryError):
            return ReadFailure(relative, "source nesting too deep to parse")
        return SourceModule(
            path=relative,
            name=self.module_name(relative),
            tree=tree,
            has_bom=data.startswith(b"\xef\xbb\xbf"),
            is_package=PurePosixPath(relative).name == "__init__.py",
        )

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
            for candidate in (f"{base}.py", f"{base}/__init__.py"):
                if self.is_file(candidate):
                    return candidate
        return None


def _inside(root: Path, relative: str) -> bool:
    """상대 경로가 `..` 없이 프로젝트 안을 가리키는지 확인한다.

    Args:
        root: 프로젝트 루트.
        relative: 상대 경로.

    Returns:
        안이면 True.
    """
    return ".." not in PurePosixPath(relative).parts and not PurePosixPath(relative).is_absolute()


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
