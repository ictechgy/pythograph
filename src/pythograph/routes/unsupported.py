"""pythograph가 라우트를 추출하지 않는 서버 프레임워크를 import로 감지한다.

`routes --role server`는 Django·DRF·Flask/Werkzeug만 읽는다. FastAPI·Starlette 같은 다른 프레임워크로 선언한 라우트는
사실이 되지 않으므로, 그 프레임워크를 import한 모듈 수를 `route-coverage:` 한계로 남긴다. 없다는 것은 그 프레임워크가
없다는 증거가 아니라 import하지 않았다는 관찰일 뿐이다(동적 import는 보지 않는다).
"""

from __future__ import annotations

import ast
from collections import Counter

from pythograph.source.project import Project, SourceModule, is_test_path

#: 최상위 모듈 이름(`aiohttp.web`은 두 성분) → 한계 문구의 프레임워크 이름이다.
UNSUPPORTED_SERVER_FRAMEWORKS = {
    "fastapi": "FastAPI",
    "starlette": "Starlette",
    "litestar": "Litestar",
    "sanic": "Sanic",
    "falcon": "Falcon",
    "tornado.web": "Tornado",
    "aiohttp.web": "aiohttp.web",
    "bottle": "Bottle",
    "pyramid": "Pyramid",
    "quart": "Quart",
}


def unsupported_framework_gap(project: Project, include_tests: bool) -> str | None:
    """지원하지 않는 서버 프레임워크를 import한 모듈 수의 한계 문구를 만든다.

    Args:
        project: 분석 대상 프로젝트.
        include_tests: 테스트 소스도 볼지.

    Returns:
        문구(접두사 제외), 그런 모듈이 없으면 None.
    """
    modules = 0
    names: Counter[str] = Counter()
    for path in project.python_files():
        if not include_tests and is_test_path(path):
            continue
        module = project.module(path)
        found = _frameworks(module.tree) if isinstance(module, SourceModule) else set()
        if found:
            modules += 1
            names.update(found)
    if not modules:
        return None
    listed = ", ".join(sorted(names))
    return (
        f"{modules} modules import {listed}; pythograph extracts only Django, Django REST framework, and Flask routes, "
        "so routes declared with those frameworks were not extracted"
    )


def _frameworks(tree: ast.Module) -> set[str]:
    """모듈이 import한 지원하지 않는 서버 프레임워크 이름을 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        프레임워크 이름 집합.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for name in modules:
            framework = _framework_of(name)
            if framework is not None:
                found.add(framework)
    return found


def _framework_of(module: str) -> str | None:
    """점 경로가 지원하지 않는 서버 프레임워크(또는 그 하위 모듈)인지 본다.

    Args:
        module: import한 모듈 점 경로.

    Returns:
        프레임워크 이름 또는 None.
    """
    parts = module.split(".")
    for length in (2, 1):
        key = ".".join(parts[:length])
        if len(parts) >= length and key in UNSUPPORTED_SERVER_FRAMEWORKS:
            return UNSUPPORTED_SERVER_FRAMEWORKS[key]
    return None
