"""Django 설정에서 persistence 이름 규칙에 필요한 값(`INSTALLED_APPS`·`DATABASES`)을 정적으로 읽는다.

routes의 설정 모듈 찾기(`DJANGO_SETTINGS_MODULE` 기본값, `--settings`)를 그대로 쓴다. `INSTALLED_APPS`는 무조건
대입뿐 아니라 `+=`·`.append`·`.extend`·`.insert`와 조건문 안 변경까지 순서대로 따라간다. 조건문 안에서 더한
앱도 "설치될 수 있는 앱"으로 넣는다(그 앱의 모델이 존재한다면 라벨은 그 앱의 것이다). 평가하지 못한 원소가
있으면 목록이 불완전하다고 표시한다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.persistence.names import ALL_BACKENDS, MYSQL, ORACLE, POSTGRESQL, SQLITE, DjangoBackend
from pythograph.routes.django.settings import load_settings
from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.symbols import SymbolTable, star_import_modules

#: 설정 모듈을 따라가는 star import 최대 깊이다.
MAX_STAR_DEPTH = 8

#: 확인한 백엔드 모듈이다(내장 백엔드와 GeoDjango 백엔드는 같은 `DatabaseOperations`를 상속한다).
_ENGINES: dict[str, DjangoBackend] = {
    "django.db.backends.sqlite3": SQLITE,
    "django.db.backends.postgresql": POSTGRESQL,
    "django.db.backends.mysql": MYSQL,
    "django.db.backends.oracle": ORACLE,
    "django.contrib.gis.db.backends.spatialite": SQLITE,
    "django.contrib.gis.db.backends.postgis": POSTGRESQL,
    "django.contrib.gis.db.backends.mysql": MYSQL,
    "django.contrib.gis.db.backends.oracle": ORACLE,
}


@dataclass
class DjangoProjectSettings:
    """persistence가 쓰는 Django 설정.

    Attributes:
        found: 설정 모듈을 찾았는지.
        installed_apps: `INSTALLED_APPS` 원소(평가한 문자열만, 순서 유지).
        apps_complete: 모든 원소를 평가했는지.
        backends: 이름 규칙 후보 백엔드.
        backends_known: 모든 데이터베이스의 ENGINE을 확인했는지.
        auth_user_model: `AUTH_USER_MODEL`(없으면 기본 `auth.User`, 모르면 None).
    """

    found: bool
    installed_apps: list[str] = field(default_factory=list)
    apps_complete: bool = True
    backends: tuple[DjangoBackend, ...] = ALL_BACKENDS
    backends_known: bool = False
    auth_user_model: str | None = "auth.User"


def read_project_settings(symbols: SymbolTable, module: str | None) -> DjangoProjectSettings:
    """설정 모듈을 찾아 persistence용 값을 읽는다.

    Args:
        symbols: 이름 해석기.
        module: `--settings`로 준 설정 모듈(없으면 진입 파일에서 찾는다).

    Returns:
        읽은 설정. 모듈을 찾지 못하면 `found=False`.
    """
    loaded = load_settings(symbols, module)
    if loaded.path is None:
        return DjangoProjectSettings(found=False, apps_complete=False)
    settings = DjangoProjectSettings(found=True)
    reader = _AppsReader(symbols, settings)
    reader.read(loaded.path, 0, set())
    settings.backends, settings.backends_known = _backends(reader.databases_value())
    user_model = loaded.get("AUTH_USER_MODEL")
    if user_model is not UNKNOWN or "AUTH_USER_MODEL" in loaded.values:
        settings.auth_user_model = user_model if isinstance(user_model, str) else None
    return settings


def _backends(databases: object) -> tuple[tuple[DjangoBackend, ...], bool]:
    """`DATABASES` 값에서 후보 백엔드를 구한다. 하나라도 모르면 모든 후보다.

    Args:
        databases: 평가한 `DATABASES` 값(사전을 평가하지 못하면 `UNKNOWN`).

    Returns:
        (후보 백엔드, 모두 확인했는지).
    """
    if not isinstance(databases, dict) or not databases:
        return ALL_BACKENDS, False
    found: list[DjangoBackend] = []
    for entry in databases.values():
        engine = entry.get("ENGINE") if isinstance(entry, dict) else None
        backend = _ENGINES.get(engine) if isinstance(engine, str) else None
        if backend is None:
            return ALL_BACKENDS, False
        if backend not in found:
            found.append(backend)
    return tuple(found), True


class _AppsReader:
    """설정 모듈 문장을 순서대로 읽어 `INSTALLED_APPS`를 만든다."""

    def __init__(self, symbols: SymbolTable, settings: DjangoProjectSettings) -> None:
        """읽기 도구를 만든다.

        Args:
            symbols: 이름 해석기.
            settings: 채울 설정.
        """
        self.symbols = symbols
        self.settings = settings
        self.evaluator = Evaluator(symbols)
        self.databases: tuple[str, ast.expr] | None = None
        self.databases_changed = False

    def databases_value(self) -> object:
        """마지막 무조건 `DATABASES` 대입을 평가한다. 조건부·부분 변경이 있으면 모르는 값이다.

        Returns:
            평가한 사전 또는 `UNKNOWN`.
        """
        if self.databases is None or self.databases_changed:
            return UNKNOWN
        path, node = self.databases
        return _dict_value(self.evaluator, path, node)

    def read(self, path: str, depth: int, seen: set[str]) -> None:
        """설정 모듈 하나를 읽는다. star import한 프로젝트 모듈을 먼저 읽는다.

        Args:
            path: 모듈 경로.
            depth: star import 깊이.
            seen: 순환 방지 경로 집합.
        """
        index = self.symbols.index(path)
        if index is None or depth > MAX_STAR_DEPTH or path in seen:
            return
        for name in star_import_modules(index):
            star_path = self.symbols.project.resolve_module(name)
            if star_path is not None:
                self.read(star_path, depth + 1, seen | {path})
        self._statements(path, index.module.tree.body, nested=False)

    def _statements(self, path: str, statements: list[ast.stmt], nested: bool) -> None:
        """문장 목록에서 `INSTALLED_APPS`·`DATABASES` 변경을 순서대로 적용한다.

        조건문 안의 `INSTALLED_APPS = [...]`는 목록을 비우지 않고 더한다(어느 분기든 설치될 수 있다).

        Args:
            path: 모듈 경로.
            statements: 문장 목록.
            nested: 조건·반복 블록 안인지.
        """
        for statement in statements:
            self._track_databases(path, statement, nested)
            targets = _assigned_targets(statement)
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and _targets_apps(targets):
                if not nested:
                    self.settings.installed_apps = []
                self._extend(path, statement.value)
            elif isinstance(statement, ast.AugAssign) and _targets_apps(targets):
                self._extend(path, statement.value)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                self._method_call(path, statement.value)
            else:
                for block in _nested_blocks(statement):
                    self._statements(path, block, nested=True)

    def _track_databases(self, path: str, statement: ast.stmt, nested: bool) -> None:
        """`DATABASES` 대입을 기록한다. 조건부 대입과 부분 변경(`DATABASES[...] = ...`)은 모르는 값으로 만든다.

        Args:
            path: 모듈 경로.
            statement: 문장.
            nested: 조건·반복 블록 안인지.
        """
        targets = _assigned_targets(statement)
        if isinstance(statement, ast.Assign) and _targets_name(targets, "DATABASES") and not nested:
            self.databases = (path, statement.value)
            self.databases_changed = False
        elif _targets_name(targets, "DATABASES") or any(_subscripts_name(target, "DATABASES") for target in targets):
            self.databases_changed = True

    def _method_call(self, path: str, call: ast.Call) -> None:
        """`INSTALLED_APPS.append/extend/insert(...)`를 적용한다.

        Args:
            path: 모듈 경로.
            call: 호출 식.
        """
        function = call.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "INSTALLED_APPS"
        ):
            return
        if function.attr == "append" and len(call.args) == 1:
            self._extend(path, ast.List(elts=[call.args[0]], ctx=ast.Load()))
        elif function.attr == "extend" and len(call.args) == 1:
            self._extend(path, call.args[0])
        elif function.attr == "insert" and len(call.args) == 2:
            self._extend(path, ast.List(elts=[call.args[1]], ctx=ast.Load()))
        else:
            self.settings.apps_complete = False

    def _extend(self, path: str, node: ast.expr | None) -> None:
        """평가한 원소를 더한다. 평가하지 못하면 목록을 불완전하다고 표시한다.

        Args:
            path: 모듈 경로.
            node: 원소 목록 식.
        """
        if isinstance(node, (ast.List, ast.Tuple)):
            for element in node.elts:
                self._extend_value(self.evaluator.value(path, element), single=True)
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            self._extend(path, node.left)
            self._extend(path, node.right)
            return
        self._extend_value(self.evaluator.value(path, node), single=False)

    def _extend_value(self, value: object, single: bool) -> None:
        """평가한 값을 원소로 더한다.

        Args:
            value: 평가한 값.
            single: 원소 하나인지(아니면 목록).
        """
        items = [value] if single else value if isinstance(value, list) else [UNKNOWN]
        for item in items:
            if isinstance(item, str):
                self.settings.installed_apps.append(item)
            else:
                self.settings.apps_complete = False


def _targets_apps(targets: list[ast.expr]) -> bool:
    """대입 대상에 `INSTALLED_APPS` 이름이 있는지 본다.

    Args:
        targets: 대입 대상.

    Returns:
        있으면 True.
    """
    return _targets_name(targets, "INSTALLED_APPS")


def _targets_name(targets: list[ast.expr], name: str) -> bool:
    """대입 대상에 이름이 있는지 본다.

    Args:
        targets: 대입 대상.
        name: 이름.

    Returns:
        있으면 True.
    """
    return any(isinstance(target, ast.Name) and target.id == name for target in targets)


def _subscripts_name(target: ast.expr, name: str) -> bool:
    """대입 대상이 `name[...]`(중첩 포함)인지 본다.

    Args:
        target: 대입 대상.
        name: 이름.

    Returns:
        그러면 True.
    """
    while isinstance(target, ast.Subscript):
        target = target.value
        if isinstance(target, ast.Name) and target.id == name:
            return True
    return False


def _assigned_targets(statement: ast.stmt) -> list[ast.expr]:
    """대입류 문장의 대상 목록을 돌려준다.

    Args:
        statement: 문장.

    Returns:
        대상 목록(대입이 아니면 빈 목록).
    """
    if isinstance(statement, ast.Assign):
        return list(statement.targets)
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        return [statement.target]
    return []


def _dict_value(evaluator: Evaluator, path: str, node: ast.expr, depth: int = 0) -> object:
    """리터럴 사전(중첩 포함)을 평가한다. 키는 문자열 상수만, 값은 평가기로 구한다.

    Args:
        evaluator: 상수 평가기.
        path: 모듈 경로.
        node: 식.
        depth: 중첩 깊이.

    Returns:
        사전·값 또는 `UNKNOWN`.
    """
    if depth > MAX_STAR_DEPTH:
        return UNKNOWN
    if not isinstance(node, ast.Dict):
        return evaluator.value(path, node)
    result: dict[str, object] = {}
    for key, value in zip(node.keys, node.values, strict=False):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return UNKNOWN
        result[key.value] = _dict_value(evaluator, path, value, depth + 1)
    return result


def _nested_blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    """조건·예외·반복·with 문의 블록을 돌려준다.

    Args:
        statement: 문장.

    Returns:
        블록 목록.
    """
    if isinstance(statement, ast.If):
        return [statement.body, statement.orelse]
    if isinstance(statement, ast.Try):
        handlers = [handler.body for handler in statement.handlers]
        return [statement.body, *handlers, statement.orelse, statement.finalbody]
    if isinstance(statement, (ast.With, ast.For, ast.While)):
        return [statement.body]
    return []
