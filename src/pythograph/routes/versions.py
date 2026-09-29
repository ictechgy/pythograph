"""프레임워크 버전 선언 확인.

pythograph의 라우팅 규칙은 특정 메이저 버전의 소스로 확인했다(Django 5, DRF 3, Flask 3 — `docs/HTTP-ROUTES.md`).
프로젝트가 그 메이저로 제한된 버전을 선언했는지 잠금·요구 파일에서 정적으로 읽는다. 확인하지 못하면
`route-framework-version-unknown:` 한계를 낸다(규칙이 다른 버전에서 다를 수 있다는 공백 신고).

읽는 파일(프로젝트 루트와 모듈 루트): `uv.lock`·`poetry.lock`·`pdm.lock`(정확한 버전), `Pipfile.lock`,
`requirements*.txt`·`requirements/*.txt`, `pyproject.toml`, `setup.cfg`, `Pipfile`. 파일은 실행하지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from pythograph.source.project import Project

#: 읽을 선언 파일의 최대 크기다.
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

#: 잠금 파일의 패키지 블록에서 (이름, 버전)을 읽는 정규식이다.
_LOCK_PACKAGE = re.compile(r'name\s*=\s*"(?P<name>[^"]+)"\s*\n\s*version\s*=\s*"(?P<version>[^"]+)"')

#: 요구 문자열(`django>=5.2,<6`)을 찾는 정규식이다.
_REQUIREMENT = re.compile(
    r"(?im)(?:^|[\"'\s,\[])(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?P<spec>(?:(?:===|==|~=|!=|<=|>=|<|>)\s*[0-9][0-9A-Za-z.*+!-]*\s*,?\s*)+)"
)

#: poetry 표기(`django = "^5.2"`)를 찾는 정규식이다.
_POETRY = re.compile(
    r"(?im)^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(?:\{[^}]*version\s*=\s*)?"
    r'"(?P<spec>(?:[\^~]?[0-9][0-9A-Za-z.*]*|(?:(?:===|==|~=|!=|<=|>=|<|>)\s*[0-9][0-9A-Za-z.*]*\s*,?\s*)+))"'
)


def normalize_name(name: str) -> str:
    """패키지 이름을 PEP 503 규칙으로 정규화한다.

    Args:
        name: 패키지 이름.

    Returns:
        소문자, `-`·`_`·`.` 연속을 `-` 하나로 바꾼 이름.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_majors(project: Project) -> dict[str, set[int | None]]:
    """선언 파일에서 패키지별로 제한된 메이저 버전을 모은다.

    Args:
        project: 분석 대상 프로젝트.

    Returns:
        정규화한 패키지 이름 → 메이저 집합(범위가 한 메이저로 제한되지 않으면 None 원소).
    """
    majors: dict[str, set[int | None]] = {}
    for path in _manifest_files(project):
        text = project.read_text(path, MAX_MANIFEST_BYTES)
        if text is None:
            continue
        for name, major in _manifest_majors(path, text):
            majors.setdefault(normalize_name(name), set()).add(major)
    return majors


def _manifest_files(project: Project) -> list[str]:
    """읽을 선언 파일 경로를 모은다.

    Args:
        project: 프로젝트.

    Returns:
        프로젝트 상대 경로 목록.
    """
    roots = list(dict.fromkeys(["", *project.module_roots]))
    names = ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock", "pyproject.toml", "setup.cfg", "Pipfile")
    files: list[str] = []
    for root in roots:
        base = f"{root}/" if root else ""
        files.extend(f"{base}{name}" for name in names)
        files.extend(_requirement_files(project, root))
    return [path for path in dict.fromkeys(files) if project.is_file(path)]


def _requirement_files(project: Project, root: str) -> list[str]:
    """`requirements*.txt`와 `requirements/*.txt`를 찾는다.

    Args:
        project: 프로젝트.
        root: 프로젝트 상대 디렉터리.

    Returns:
        경로 목록.
    """
    directory = project.root / root if root else project.root
    found: list[str] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        relative = PurePosixPath(root, entry.name).as_posix() if root else entry.name
        if entry.name.startswith("requirements") and entry.name.endswith(".txt"):
            found.append(relative)
        elif entry.name == "requirements" and project.is_directory(relative):
            try:
                found.extend(
                    f"{relative}/{child.name}" for child in sorted(entry.iterdir()) if child.name.endswith(".txt")
                )
            except OSError:
                continue
    return found


def _manifest_majors(path: str, text: str) -> list[tuple[str, int | None]]:
    """선언 파일 하나에서 (패키지, 메이저)를 읽는다.

    Args:
        path: 파일 경로.
        text: 내용.

    Returns:
        (이름, 메이저 또는 None) 목록.
    """
    name = PurePosixPath(path).name
    if name in ("uv.lock", "poetry.lock", "pdm.lock"):
        return [(match["name"], _major(match["version"])) for match in _LOCK_PACKAGE.finditer(text)]
    if name == "Pipfile.lock":
        return _pipfile_lock(text)
    results = [(match["name"], spec_major(match["spec"])) for match in _REQUIREMENT.finditer(text)]
    if name in ("pyproject.toml", "Pipfile"):
        results.extend((match["name"], spec_major(match["spec"])) for match in _POETRY.finditer(text))
    return results


def _pipfile_lock(text: str) -> list[tuple[str, int | None]]:
    """`Pipfile.lock`(JSON)의 정확한 버전을 읽는다.

    Args:
        text: 내용.

    Returns:
        (이름, 메이저) 목록.
    """
    try:
        document = json.loads(text)
    except ValueError:
        return []
    results: list[tuple[str, int | None]] = []
    for section in ("default", "develop"):
        packages = document.get(section, {}) if isinstance(document, dict) else {}
        for package, info in packages.items() if isinstance(packages, dict) else []:
            version = info.get("version") if isinstance(info, dict) else None
            if isinstance(version, str):
                results.append((package, spec_major(version)))
    return results


def _major(version: str) -> int | None:
    """정확한 버전 문자열의 메이저를 구한다.

    Args:
        version: 버전(`5.2.17`).

    Returns:
        메이저 또는 None.
    """
    head = version.strip().split(".")[0]
    return int(head) if head.isdigit() else None


def spec_major(spec: str) -> int | None:
    """버전 지정자가 한 메이저로 제한되면 그 메이저를 돌려준다.

    `==5.2.1`, `==5.*`, `~=5.2`, `>=5.2,<6`, `>=5.2,<5.3`, poetry `^5.2`·`~5.2`는 5다. 하한이나 상한이
    없거나 두 메이저에 걸치면 None이다.

    Args:
        spec: 지정자 문자열.

    Returns:
        메이저 또는 None.
    """
    text = spec.replace(" ", "")
    if text[:1] in "^~" and text[1:2].isdigit():
        return _major(text[1:])
    lower: int | None = None
    upper: int | None = None
    for clause in filter(None, text.split(",")):
        match = re.match(r"(===|==|~=|!=|<=|>=|<|>)(.+)", clause)
        if match is None:
            continue
        operator, version = match.groups()
        parts = version.split(".")
        major = int(parts[0]) if parts[0].isdigit() else None
        if major is None:
            return None
        if operator in ("==", "==="):
            return major
        if operator == "~=" and len(parts) >= 2:
            return major
        if operator in (">=", ">"):
            lower = major
        elif operator == "<=":
            upper = major
        elif operator == "<":
            upper = major - 1 if all(part in ("0", "*") for part in parts[1:]) else major
    return lower if lower is not None and lower == upper else None


def version_gap(majors: dict[str, set[int | None]], package: str, expected: int, label: str) -> str | None:
    """패키지가 기대한 메이저로 제한되지 않았으면 한계 문장을 돌려준다.

    Args:
        majors: 선언한 메이저.
        package: 정규화한 패키지 이름.
        expected: 확인한 메이저.
        label: 사람이 읽을 이름.

    Returns:
        한계 문장(접두사 제외) 또는 None.
    """
    found = majors.get(package)
    if found and found == {expected}:
        return None
    return (
        f"the project does not declare {label} limited to major version {expected}, the version whose routing "
        "rules pythograph verified"
    )
