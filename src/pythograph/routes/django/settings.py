"""Django 설정 모듈 찾기와 설정값 정적 읽기.

Django는 `DJANGO_SETTINGS_MODULE` 환경 변수로 설정 모듈을 정하고, 프로젝트 템플릿의 `manage.py`·
`wsgi.py`·`asgi.py`는 `os.environ.setdefault("DJANGO_SETTINGS_MODULE", "<모듈>")`로 기본값을 둔다.
pythograph는 그 기본값 리터럴을 읽는다(`--settings`로 바꿀 수 있다). 환경 변수 재정의는 모델링하지 않는다.
설정값은 모듈 수준 대문자 이름의 무조건 대입만 읽고, 프로젝트 안 모듈의 `from x import *`는 먼저 합친다. 정적 값
하나로 증명되지 않는 대문자 이름(두 번 이상 묶임·`+=`·조건·반복·예외·`with` 블록 안 대입·`global` 재대입·
`.append` 같은 제자리 변경, 이름공간 사전을 쓰는 모듈)은 읽지 못한 설정(`UNKNOWN`)이며 star import로 합친 기반
설정 값도 덮는다(`ModuleIndex.rebound`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.symbols import ModuleIndex, SymbolTable, star_import_modules

#: 설정 모듈을 따라가는 star import 최대 깊이다.
MAX_STAR_DEPTH = 8

#: 기본값을 찾는 진입 파일 이름과 우선순위다.
_ENTRY_FILES = ("manage.py", "wsgi.py", "asgi.py")


@dataclass
class DjangoSettings:
    """읽은 Django 설정.

    Attributes:
        module: 설정 모듈 이름(찾지 못하면 None).
        path: 설정 모듈 파일(프로젝트 상대, 찾지 못하면 None).
        values: 대문자 설정 이름 → 평가한 값(평가하지 못한 이름은 `UNKNOWN`).
        candidates: 진입 파일에서 찾은 서로 다른 설정 모듈 이름.
    """

    module: str | None
    path: str | None
    values: dict[str, object] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)

    def get(self, name: str) -> object:
        """설정값을 돌려준다.

        Args:
            name: 설정 이름.

        Returns:
            값, 없거나 모르면 `UNKNOWN`.
        """
        return self.values.get(name, UNKNOWN)


def discover_settings_modules(symbols: SymbolTable) -> list[str]:
    """진입 파일의 `DJANGO_SETTINGS_MODULE` 기본값을 우선순위대로 모은다.

    Args:
        symbols: 이름 해석기.

    Returns:
        서로 다른 설정 모듈 이름(`manage.py` → `wsgi.py` → `asgi.py`, 얕은 경로 우선).
    """
    files = [path for path in symbols.project.python_files() if PurePosixPath(path).name in _ENTRY_FILES]
    files.sort(key=lambda path: (_ENTRY_FILES.index(PurePosixPath(path).name), path.count("/"), path))
    found: list[str] = []
    for path in files:
        index = symbols.index(path)
        if index is None:
            continue
        for node in ast.walk(index.module.tree):
            value = _settings_default(node)
            if value is not None and value not in found:
                found.append(value)
    return found


def _settings_default(node: ast.AST) -> str | None:
    """`os.environ.setdefault("DJANGO_SETTINGS_MODULE", "<모듈>")` 호출이면 모듈 이름을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        모듈 이름 또는 None.
    """
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setdefault"
        and len(node.args) == 2
    ):
        return None
    key, value = node.args
    if not (isinstance(key, ast.Constant) and key.value == "DJANGO_SETTINGS_MODULE"):
        return None
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def load_settings(symbols: SymbolTable, module: str | None) -> DjangoSettings:
    """설정 모듈을 찾아 값을 읽는다.

    Args:
        symbols: 이름 해석기.
        module: 설정 모듈 이름(None이면 진입 파일에서 찾는다).

    Returns:
        읽은 설정(모듈을 찾지 못하면 값이 빈 설정).
    """
    candidates = [module] if module is not None else discover_settings_modules(symbols)
    chosen = candidates[0] if candidates else None
    path = symbols.project.resolve_module(chosen) if chosen else None
    settings = DjangoSettings(module=chosen, path=path, candidates=candidates)
    if path is not None:
        settings.values = _module_settings(symbols, path, 0, set())
    return settings


def _module_settings(symbols: SymbolTable, path: str, depth: int, seen: set[str]) -> dict[str, object]:
    """설정 모듈 하나의 대문자 이름 값을 모은다. star import한 프로젝트 모듈 값을 먼저 합친다.

    Args:
        symbols: 이름 해석기.
        path: 설정 모듈 경로.
        depth: star import 깊이.
        seen: 순환 방지용 경로 집합.

    Returns:
        이름 → 값.
    """
    index = symbols.index(path)
    if index is None or depth > MAX_STAR_DEPTH or path in seen:
        return {}
    values: dict[str, object] = {}
    for name in star_import_modules(index):
        star_path = symbols.project.resolve_module(name)
        if star_path is not None:
            values.update(_module_settings(symbols, star_path, depth + 1, seen | {path}))
    evaluator = Evaluator(symbols)
    for name, node in index.values.items():
        if name.isupper() and not index.rebound(name):
            values[name] = evaluator.value(path, node)
    for name in _unproven_settings(index, values):
        values[name] = UNKNOWN
    return values


def _unproven_settings(index: ModuleIndex, merged: dict[str, object]) -> list[str]:
    """설정 모듈이 정적 값 하나로 증명하지 못하는 대문자 이름을 돌려준다.

    이 모듈이 묶거나 바꾸는 이름과, 모듈이 이름공간 사전을 쓰면 star import로 합친 이름까지 본다. 그런 이름은 기반
    설정 값을 남기면 안 된다. 실행하면 이 모듈이 바꾼 값(`from .base import *` 뒤 `INSTALLED_APPS += [...]`)이나
    조건에 따라 생기는 값(`if …: FORCE_SCRIPT_NAME = "/sub"`)이 남기 때문이다.

    Args:
        index: 설정 모듈 색인.
        merged: 지금까지 모은 이름 → 값(star import로 합친 값과 이 모듈 값).

    Returns:
        이름 목록(정렬).
    """
    summary = index.bindings()
    names = set(merged) | set(summary.counts) | summary.declared_globals | summary.mutated
    return sorted(name for name in names if name.isupper() and index.rebound(name))
