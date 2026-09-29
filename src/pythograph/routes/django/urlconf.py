"""Django URLconf 모듈을 실행 없이 해석해 URL 패턴 트리를 만든다.

모듈 수준 문장을 순서대로 읽는 작은 추상 해석기다. `urlpatterns = [...]`, `+=`, `.append`·`.extend`·
`.insert`, 목록 변수, `include()`(모듈 문자열·목록·`(목록, app_name)`·모듈 객체), DRF `router.urls`,
`admin.site.urls`, `static()`, `i18n_patterns()`, `format_suffix_patterns()`, `register_converter()`를
다룬다. Django의 패턴 해석 순서(`URLResolver.resolve`는 목록 순서대로 시도하고 중첩 include 안에서
맞는 것이 없으면 다음 패턴으로 넘어간다)대로 트리를 보존해, 깊이 우선 순서가 곧 등록 순서가 된다.

조건문(`if`·`try`) 안에서 더한 패턴은 조건부로 표시한다. 확정할 수 없는 식은 그 자리에 불투명 항목을
남겨, 현재 접두사로 상한을 증명할 수 있으면 스코프 있는 한계가 되게 한다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace

from pythograph.routes.classes import analyze_class
from pythograph.routes.model import Location
from pythograph.source.evaluate import Evaluator
from pythograph.source.symbols import (
    ExternalSymbol,
    ModuleSymbol,
    Symbol,
    SymbolTable,
    ValueSymbol,
    is_external,
)

#: include를 따라가는 최대 중첩 깊이다.
MAX_INCLUDE_DEPTH = 32


@dataclass(frozen=True)
class Piece:
    """URL 패턴 한 단계의 경로 조각.

    Attributes:
        kind: `route`(`path()`), `regex`(`re_path()`), `locale`(`i18n_patterns` 언어 접두사).
        text: 조각 문자열(리터럴이 아니면 None).
        endpoint: 뷰에 닿는 패턴인지(include 접두사는 False).
    """

    kind: str
    text: str | None
    endpoint: bool


@dataclass(frozen=True)
class DrfBinding:
    """DRF 라우터가 연결한 viewset action.

    Attributes:
        viewset_path: viewset 식이 있는 모듈 경로.
        viewset: viewset 식.
        mapping: 소문자 HTTP 동사 → action 이름.
        framework_view: 프레임워크 뷰(API 루트)면 그 점 경로.
    """

    viewset_path: str
    viewset: ast.expr | None
    mapping: tuple[tuple[str, str], ...]
    framework_view: str | None = None


@dataclass(frozen=True)
class Endpoint:
    """뷰에 닿는 패턴."""

    piece: Piece
    module: str
    view: ast.expr | None
    location: Location
    conditional: bool = False
    drf: DrfBinding | None = None


@dataclass(frozen=True)
class Opaque:
    """확정할 수 없는 패턴 자리. 현재 접두사 아래의 알 수 없는 경로를 뜻한다.

    Attributes:
        reason: limitation 문장 틀.
        prefix: `route-coverage:` 또는 `framework-provided-routes:`.
        location: 원인 위치(없을 수 있음).
        conditional: 조건부 등록인지.
        methods: 받을 수 있는 동사 상한(모르면 빈 튜플).
    """

    reason: str
    prefix: str
    location: Location | None
    conditional: bool = False
    methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class Include:
    """접두사와 하위 패턴 목록."""

    piece: Piece
    children: tuple[Entry, ...]
    conditional: bool = False


#: URL 패턴 트리 항목이다.
Entry = Endpoint | Opaque | Include


@dataclass
class RouterState:
    """DRF 라우터 객체의 정적 상태.

    Attributes:
        kind: `default`·`simple` 또는 None(모르는 라우터).
        trailing_slash: `/` 또는 빈 문자열, 모르면 None.
        use_regex: `use_regex_path` 값, 모르면 None.
        registrations: (prefix, viewset 모듈, viewset 식, 위치) 목록.
        location: 라우터를 만든 위치.
    """

    kind: str | None
    trailing_slash: str | None
    use_regex: bool | None
    location: Location
    registrations: list[tuple[str | None, str, ast.expr | None, Location]] = field(default_factory=list)


@dataclass
class ConverterRegistry:
    """`register_converter`로 등록한 변환기 이름 → 정규식(모르면 None)."""

    regexes: dict[str, str | None] = field(default_factory=dict)


class UrlconfReader:
    """URLconf 모듈 해석기. 모듈별 결과를 캐시하고 include 순환을 막는다."""

    def __init__(self, symbols: SymbolTable, evaluator: Evaluator, router_builder: RouterBuilder) -> None:
        """해석기를 만든다.

        Args:
            symbols: 이름 해석기.
            evaluator: 상수 평가기(설정값 포함).
            router_builder: DRF 라우터 패턴 생성기.
        """
        self.symbols = symbols
        self.evaluator = evaluator
        self.router_builder = router_builder
        self.converters = ConverterRegistry()
        self._namespaces: dict[str, dict[str, object]] = {}
        self._active: list[str] = []

    def module_patterns(self, path: str) -> tuple[Entry, ...] | None:
        """모듈의 `urlpatterns`를 해석한다.

        Args:
            path: URLconf 모듈 경로.

        Returns:
            패턴 목록, 모듈을 읽지 못하거나 `urlpatterns`가 없으면 None.
        """
        namespace = self.namespace(path)
        if namespace is None:
            return None
        value = namespace.get("urlpatterns")
        return tuple(value) if isinstance(value, list) else None

    def namespace(self, path: str) -> dict[str, object] | None:
        """모듈 수준 문장을 해석해 이름 → 값 사전을 만든다(캐시, 순환이면 None).

        Args:
            path: 모듈 경로.

        Returns:
            이름 공간 또는 None.
        """
        if path in self._namespaces:
            return self._namespaces[path]
        if path in self._active or len(self._active) > MAX_INCLUDE_DEPTH:
            return None
        index = self.symbols.index(path)
        if index is None:
            return None
        self._active.append(path)
        try:
            namespace: dict[str, object] = {}
            _ModuleInterpreter(self, path, namespace, index.module.has_bom).run(index.module.tree.body, False)
        finally:
            self._active.pop()
        self._namespaces[path] = namespace
        return namespace


class RouterBuilder:
    """DRF 라우터 상태에서 URL 패턴을 만드는 인터페이스(구현은 `drf.py`)."""

    def expand(self, router: RouterState, conditional: bool) -> list[Entry]:  # pragma: no cover - 인터페이스
        """라우터의 현재 등록으로 패턴 목록을 만든다.

        Args:
            router: 라우터 상태.
            conditional: 조건부 등록인지.

        Returns:
            패턴 목록.
        """
        raise NotImplementedError

    def suffix(self, entries: list[Entry], call: ast.Call, path: str) -> list[Entry] | None:  # pragma: no cover
        """`format_suffix_patterns`를 적용한다.

        Args:
            entries: 패턴 목록.
            call: 호출 식(인자 확인용).
            path: 모듈 경로.

        Returns:
            변환한 목록, 인자를 확정하지 못하면 None.
        """
        raise NotImplementedError


def node_location(path: str, node: ast.AST, has_bom: bool) -> Location:
    """구문 노드의 위치를 계약 형식(1부터 줄, UTF-8 바이트 1부터 열)으로 만든다.

    `ast`의 `col_offset`은 UTF-8 바이트 오프셋이다. 파일이 BOM으로 시작하면 첫 줄 열에 3바이트를 더한다.

    Args:
        path: 프로젝트 상대 경로.
        node: 구문 노드.
        has_bom: BOM 여부.

    Returns:
        위치.
    """
    line = int(getattr(node, "lineno", 1))
    column = int(getattr(node, "col_offset", 0)) + 1
    if has_bom and line == 1:
        column += 3
    return Location(path, line, column)


class _ModuleInterpreter:
    """한 모듈의 모듈 수준 문장 해석기."""

    def __init__(self, reader: UrlconfReader, path: str, namespace: dict[str, object], has_bom: bool) -> None:
        """해석기를 만든다.

        Args:
            reader: 상위 해석기.
            path: 모듈 경로.
            namespace: 채울 이름 공간.
            has_bom: 파일 BOM 여부(위치 보정).
        """
        self.reader = reader
        self.path = path
        self.namespace = namespace
        self.has_bom = has_bom

    def run(self, statements: list[ast.stmt], conditional: bool) -> None:
        """문장 목록을 순서대로 해석한다.

        Args:
            statements: 문장 목록.
            conditional: 조건문 안인지.
        """
        for statement in statements:
            self._statement(statement, conditional)

    def _statement(self, statement: ast.stmt, conditional: bool) -> None:
        """문장 하나를 해석한다.

        Args:
            statement: 문장.
            conditional: 조건문 안인지.
        """
        if isinstance(statement, ast.Assign):
            value = self._value(statement.value, conditional)
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    self._assign(target.id, self._transformed(target.id, statement.value, value, conditional),
                                 conditional)
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            if isinstance(statement.target, ast.Name):
                self._assign(statement.target.id, self._value(statement.value, conditional), conditional)
        elif isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name):
            self._augment(statement.target.id, statement.value, conditional)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            self._call_statement(statement.value, conditional)
        elif isinstance(statement, ast.If):
            self.run(statement.body, True)
            self.run(statement.orelse, True)
        elif isinstance(statement, ast.Try):
            for block in (
                statement.body,
                *[handler.body for handler in statement.handlers],
                statement.orelse,
                statement.finalbody,
            ):
                self.run(block, True)
        elif isinstance(statement, ast.With):
            self.run(statement.body, conditional)
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            self._loop(statement, conditional)

    def _transformed(self, name: str, node: ast.expr, value: object, conditional: bool) -> object:
        """알 수 없는 호출로 목록을 바꾸면(`urlpatterns = helper(urlpatterns)`) 기존 항목과 불투명 항목을 남긴다.

        호출이 패턴을 더하거나 뺄 수 있으므로 기존 항목은 유지하고(빠졌을 수 있는 선언은 거짓 match 쪽이라
        안전하다) 더했을 수 있는 경로는 불투명 항목으로 공백에 싣는다.

        Args:
            name: 대입 대상 이름.
            node: 오른쪽 식.
            value: 해석한 값.
            conditional: 조건문 안인지.

        Returns:
            대입할 값.
        """
        if value is not None or not isinstance(node, ast.Call):
            return value
        existing = self.namespace.get(name)
        referenced = any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))
        kept: list[object] = list(existing) if isinstance(existing, list) and referenced else []
        reason = "{count} URL pattern lists are transformed by a call that is not modeled"
        kept.append(self._opaque(reason, node, conditional))
        return kept

    def _assign(self, name: str, value: object, conditional: bool) -> None:
        """이름에 값을 묶는다. 조건부 대입은 기존 목록을 버리지 않고 조건부 항목을 덧붙인다.

        Args:
            name: 이름.
            value: 값.
            conditional: 조건문 안인지.
        """
        if conditional and isinstance(self.namespace.get(name), list) and isinstance(value, list):
            existing = self.namespace[name]
            assert isinstance(existing, list)
            existing.extend(_mark_conditional(value))
            return
        self.namespace[name] = value

    def _augment(self, name: str, node: ast.expr, conditional: bool) -> None:
        """`name += expr`를 해석한다.

        Args:
            name: 대상 이름.
            node: 더할 식.
            conditional: 조건문 안인지.
        """
        target = self.namespace.get(name)
        if not isinstance(target, list):
            return
        entries = self._entries(node, conditional)
        target.extend(_mark_conditional(entries) if conditional else entries)

    def _call_statement(self, call: ast.Call, conditional: bool) -> None:
        """식 문장 호출(`.append`·`.extend`·`.insert`·`router.register`·`register_converter`)을 해석한다.

        Args:
            call: 호출 식.
            conditional: 조건문 안인지.
        """
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = self.namespace.get(func.value.id)
            if isinstance(target, list):
                self._list_method(target, func.attr, call, conditional)
                return
            if isinstance(target, RouterState) and func.attr == "register":
                self._register(target, call)
                return
        resolved = (
            self.reader.symbols.resolve_expr(self.path, func) if isinstance(func, (ast.Name, ast.Attribute)) else None
        )
        if is_external(resolved, "django.urls.register_converter", "django.urls.converters.register_converter"):
            self._register_converter(call)

    def _list_method(self, target: list[object], method: str, call: ast.Call, conditional: bool) -> None:
        """목록 메서드 호출을 해석한다.

        Args:
            target: 대상 목록.
            method: 메서드 이름.
            call: 호출 식.
            conditional: 조건문 안인지.
        """
        if method == "append" and len(call.args) == 1:
            added: list[Entry] = [self._entry(call.args[0], conditional)]
        elif method == "extend" and len(call.args) == 1:
            added = self._entries(call.args[0], conditional)
        elif method == "insert" and len(call.args) == 2:
            position = self.reader.evaluator.value(self.path, call.args[0])
            entry = self._entry(call.args[1], conditional)
            if isinstance(position, int) and not isinstance(position, bool) and not conditional:
                target.insert(position, entry)
                return
            added = [entry]
        else:
            added = [
                self._opaque("{count} URL pattern lists are changed by a call that is not modeled", call, conditional)
            ]
        target.extend(_mark_conditional(added) if conditional else added)

    def _loop(self, statement: ast.For | ast.AsyncFor | ast.While, conditional: bool) -> None:
        """반복문 안에서 목록을 바꾸면 그 목록에 불투명 항목을 남긴다.

        Args:
            statement: 반복문.
            conditional: 조건문 안인지.
        """
        del conditional
        touched = {
            node.value.id
            for node in ast.walk(statement)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        touched |= {
            node.target.id
            for node in ast.walk(statement)
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
        }
        for name in sorted(touched):
            target = self.namespace.get(name)
            if isinstance(target, list):
                target.append(self._opaque("{count} URL pattern lists are built in a loop", statement, True))

    def _register(self, router: RouterState, call: ast.Call) -> None:
        """`router.register(prefix, viewset, basename=None)`를 기록한다.

        Args:
            router: 라우터 상태.
            call: 호출 식.
        """
        prefix_node = call.args[0] if call.args else _keyword(call, "prefix")
        viewset_node = call.args[1] if len(call.args) > 1 else _keyword(call, "viewset")
        prefix = self.reader.evaluator.string(self.path, prefix_node)
        location = self._location(prefix_node if prefix_node is not None else call)
        router.registrations.append((prefix, self.path, viewset_node, location))

    def _register_converter(self, call: ast.Call) -> None:
        """`register_converter(Class, "name")`를 기록한다. 클래스의 `regex`를 정적으로 읽는다.

        Args:
            call: 호출 식.
        """
        if len(call.args) != 2:
            return
        name = self.reader.evaluator.string(self.path, call.args[1])
        if name is None:
            return
        self.reader.converters.regexes[name] = converter_regex(
            self.reader.symbols, self.reader.evaluator, self.path, call.args[0]
        )

    def _value(self, node: ast.expr, conditional: bool) -> object:
        """대입 오른쪽 식의 값을 구한다(패턴 목록, 패턴, 라우터, 그 밖은 None).

        Args:
            node: 식.
            conditional: 조건문 안인지.

        Returns:
            값.
        """
        router = self._router(node)
        if router is not None:
            return router
        if isinstance(node, (ast.List, ast.Tuple, ast.BinOp)) or self._is_list_call(node):
            return self._entries(node, conditional)
        if isinstance(node, ast.Call) and self._pattern_kind(node) is not None:
            return self._entry(node, conditional)
        if isinstance(node, ast.Name):
            existing = self.namespace.get(node.id)
            return list(existing) if isinstance(existing, list) else existing
        if isinstance(node, ast.Attribute) and node.attr == "urls":
            return self._entries(node, conditional)
        return None

    def _is_list_call(self, node: ast.expr) -> bool:
        """패턴 목록을 돌려주는 알려진 호출인지 확인한다.

        Args:
            node: 식.

        Returns:
            `static`·`i18n_patterns`·`format_suffix_patterns`·`staticfiles_urlpatterns`면 True.
        """
        if not isinstance(node, ast.Call) or not isinstance(node.func, (ast.Name, ast.Attribute)):
            return False
        resolved = self.reader.symbols.resolve_expr(self.path, node.func)
        return _list_function(resolved) is not None

    def _router(self, node: ast.expr) -> RouterState | None:
        """DRF 라우터 생성식이면 상태를 만든다.

        Args:
            node: 식.

        Returns:
            라우터 상태 또는 None.
        """
        if not isinstance(node, ast.Call) or not isinstance(node.func, (ast.Name, ast.Attribute)):
            return None
        resolved = self.reader.symbols.resolve_expr(self.path, node.func)
        if is_external(resolved, "rest_framework.routers.DefaultRouter"):
            kind: str | None = "default"
        elif is_external(resolved, "rest_framework.routers.SimpleRouter"):
            kind = "simple"
        else:
            return None
        trailing = _keyword(node, "trailing_slash")
        use_regex = _keyword(node, "use_regex_path")
        trailing_value = self.reader.evaluator.value(self.path, trailing) if trailing is not None else True
        regex_value = self.reader.evaluator.value(self.path, use_regex) if use_regex is not None else True
        return RouterState(
            kind=kind if not node.args else None,
            trailing_slash=None if not isinstance(trailing_value, (bool, str)) else ("/" if trailing_value else ""),
            use_regex=regex_value if isinstance(regex_value, bool) else None,
            location=self._location(node),
        )

    def _entries(self, node: ast.expr, conditional: bool) -> list[Entry]:
        """패턴 목록 식을 해석한다.

        Args:
            node: 식.
            conditional: 조건문 안인지.

        Returns:
            패턴 목록(확정할 수 없으면 불투명 항목 하나).
        """
        if isinstance(node, (ast.List, ast.Tuple)):
            result: list[Entry] = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    result.extend(self._entries(element.value, conditional))
                else:
                    result.append(self._entry(element, conditional))
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._entries(node.left, conditional) + self._entries(node.right, conditional)
        if isinstance(node, ast.Name) and isinstance(self.namespace.get(node.id), list):
            value = self.namespace[node.id]
            assert isinstance(value, list)
            return list(value)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            router = self.namespace.get(node.value.id)
            if isinstance(router, RouterState) and node.attr == "urls":
                return self._router_entries(router, node, conditional)
        if isinstance(node, ast.Call):
            listed = self._list_call(node, conditional)
            if listed is not None:
                return listed
        imported = self._imported_list(node)
        if imported is not None:
            return imported
        return [self._opaque("{count} URL pattern lists could not be resolved statically", node, conditional)]

    def _router_entries(self, router: RouterState, node: ast.expr, conditional: bool) -> list[Entry]:
        """라우터의 현재 등록으로 패턴을 만든다(`router.urls`는 접근 시점의 등록을 담는다).

        Args:
            router: 라우터 상태.
            node: `router.urls` 식.
            conditional: 조건문 안인지.

        Returns:
            패턴 목록.
        """
        if router.kind is None or router.trailing_slash is None or router.use_regex is None:
            return [
                self._opaque(
                    "{count} Django REST framework routers use options that are not modeled", node, conditional
                )
            ]
        return self.reader.router_builder.expand(router, conditional)

    def _imported_list(self, node: ast.expr) -> list[Entry] | None:
        """다른 모듈에서 import한 패턴 목록(`from x.urls import urlpatterns as p`, `module.urlpatterns`)을 푼다.

        Args:
            node: 이름·속성 식.

        Returns:
            패턴 목록 또는 None.
        """
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return None
        symbol = self.reader.symbols.resolve_expr(self.path, node)
        if isinstance(symbol, ValueSymbol):
            namespace = self.reader.namespace(symbol.path)
            value = namespace.get(symbol.name) if namespace is not None else None
            return list(value) if isinstance(value, list) else None
        return None

    def _list_call(self, call: ast.Call, conditional: bool) -> list[Entry] | None:
        """패턴 목록을 돌려주는 알려진 함수 호출을 해석한다.

        Args:
            call: 호출 식.
            conditional: 조건문 안인지.

        Returns:
            패턴 목록, 알려진 함수가 아니면 None.
        """
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return None
        kind = _list_function(self.reader.symbols.resolve_expr(self.path, call.func))
        if kind == "static":
            return [self._static_entry(call, conditional)]
        if kind == "staticfiles":
            return [
                self._framework("Django staticfiles serves STATIC_URL in DEBUG", call, conditional, ("GET", "HEAD"))
            ]
        if kind == "i18n":
            children = tuple(
                self._entry(argument, conditional) for argument in call.args if not isinstance(argument, ast.Starred)
            )
            return [Include(Piece("locale", None, False), children, conditional)]
        if kind == "suffix" and call.args:
            converted = self.reader.router_builder.suffix(self._entries(call.args[0], conditional), call, self.path)
            if converted is not None:
                return converted
            return [
                self._opaque(
                    "{count} format_suffix_patterns calls use arguments that are not modeled", call, conditional
                )
            ]
        return None

    def _static_entry(self, call: ast.Call, conditional: bool) -> Entry:
        """`django.conf.urls.static.static(prefix, ...)`를 framework 제공 경로로 만든다.

        Django는 `DEBUG`이고 prefix에 host가 없을 때만 `re_path(r"^<prefix>(?P<path>.*)$", serve)`를 더한다
        (`django/conf/urls/static.py`). 설정에 따라 달라지므로 decl을 내지 않고 접두사로 상한을 둔다.

        Args:
            call: 호출 식.
            conditional: 조건문 안인지.

        Returns:
            접두사 조각과 불투명 항목을 담은 include.
        """
        prefix = self.reader.evaluator.string(self.path, call.args[0]) if call.args else None
        opaque = self._framework(
            "django.conf.urls.static serves files under a URL prefix in DEBUG", call, conditional, ()
        )
        if prefix is None or "://" in prefix or prefix.startswith("//"):
            return replace(opaque, reason="django.conf.urls.static serves files under an unresolved URL prefix")
        return Include(Piece("route", prefix.lstrip("/"), False), (opaque,), conditional)

    def _entry(self, node: ast.expr, conditional: bool) -> Entry:
        """패턴 하나를 해석한다.

        Args:
            node: 식.
            conditional: 조건문 안인지.

        Returns:
            패턴 항목.
        """
        if isinstance(node, ast.Name):
            value = self.namespace.get(node.id)
            if isinstance(value, (Endpoint, Include, Opaque)):
                return _with_conditional(value, conditional) if conditional else value
        if isinstance(node, ast.Call):
            kind = self._pattern_kind(node)
            if kind is not None:
                return self._pattern(kind, node, conditional)
        return self._opaque("{count} URL patterns could not be resolved statically", node, conditional)

    def _pattern_kind(self, call: ast.Call) -> str | None:
        """`path`·`re_path` 호출이면 종류를 돌려준다.

        Args:
            call: 호출 식.

        Returns:
            `route`·`regex` 또는 None.
        """
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return None
        resolved = self.reader.symbols.resolve_expr(self.path, call.func)
        if is_external(resolved, "django.urls.path", "django.urls.conf.path"):
            return "route"
        if is_external(resolved, "django.urls.re_path", "django.urls.conf.re_path", "django.conf.urls.url"):
            return "regex"
        return None

    def _pattern(self, kind: str, call: ast.Call, conditional: bool) -> Entry:
        """`path(route, view)`·`re_path(regex, view)`를 해석한다.

        Args:
            kind: `route` 또는 `regex`.
            call: 호출 식.
            conditional: 조건문 안인지.

        Returns:
            endpoint 또는 include.
        """
        route_node = call.args[0] if call.args else _keyword(call, "route")
        view_node = call.args[1] if len(call.args) > 1 else _keyword(call, "view")
        text = self.reader.evaluator.string(self.path, route_node)
        location = self._location(route_node if route_node is not None else call)
        children = self._include_children(view_node, conditional) if view_node is not None else None
        if children is not None:
            return Include(Piece(kind, text, False), tuple(children), conditional)
        return Endpoint(Piece(kind, text, True), self.path, view_node, location, conditional)

    def _include_children(self, node: ast.expr, conditional: bool) -> list[Entry] | None:
        """뷰 인자가 include 대상이면 하위 패턴을 돌려준다.

        Args:
            node: 뷰 인자 식.
            conditional: 조건문 안인지.

        Returns:
            하위 패턴 목록, 뷰면 None.
        """
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            resolved = self.reader.symbols.resolve_expr(self.path, node.func)
            if is_external(resolved, "django.urls.include", "django.urls.conf.include", "django.conf.urls.include"):
                target = node.args[0] if node.args else _keyword(node, "arg")
                return self._include_target(target, node, conditional)
        if isinstance(node, ast.Tuple) and len(node.elts) == 3:
            return self._entries(node.elts[0], conditional)
        if isinstance(node, ast.Attribute) and node.attr == "urls":
            return self._urls_attribute(node, conditional)
        return None

    def _urls_attribute(self, node: ast.Attribute, conditional: bool) -> list[Entry]:
        """`X.urls` 뷰 인자(`admin.site.urls`, DRF 라우터, 제3자 API 객체)를 해석한다.

        Args:
            node: 속성 식.
            conditional: 조건문 안인지.

        Returns:
            하위 패턴 목록.
        """
        if isinstance(node.value, ast.Name) and isinstance(self.namespace.get(node.value.id), RouterState):
            return self._entries(node, conditional)
        resolved = self.reader.symbols.resolve_expr(self.path, node)
        if is_external(resolved, "django.contrib.admin.site.urls", "django.contrib.admin.sites.site.urls"):
            return [self._framework("the Django admin site serves routes under its URL prefix", node, conditional, ())]
        return [self._opaque("{count} included URL modules outside the project are not scanned", node, conditional)]

    def _include_target(self, node: ast.expr | None, call: ast.Call, conditional: bool) -> list[Entry]:
        """`include()`의 대상을 해석한다.

        Args:
            node: 첫 인자.
            call: include 호출(위치용).
            conditional: 조건문 안인지.

        Returns:
            하위 패턴 목록.
        """
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            node = node.elts[0]
        if node is None:
            return [self._opaque("{count} include() calls could not be resolved statically", call, conditional)]
        module_name = self.reader.evaluator.string(self.path, node)
        if module_name is not None:
            return self._include_module(module_name, call, conditional)
        symbol = (
            self.reader.symbols.resolve_expr(self.path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        )
        if isinstance(symbol, ModuleSymbol):
            return self._module_entries(symbol.path, call, conditional)
        if isinstance(symbol, ExternalSymbol):
            return [self._external_include(symbol.dotted, call, conditional)]
        return self._entries(node, conditional)

    def _include_module(self, module_name: str, call: ast.Call, conditional: bool) -> list[Entry]:
        """모듈 이름 문자열 include를 해석한다.

        Args:
            module_name: 모듈 이름.
            call: include 호출.
            conditional: 조건문 안인지.

        Returns:
            하위 패턴 목록.
        """
        path = self.reader.symbols.project.resolve_module(module_name)
        if path is None:
            return [self._external_include(module_name, call, conditional)]
        return self._module_entries(path, call, conditional)

    def _module_entries(self, path: str, call: ast.Call, conditional: bool) -> list[Entry]:
        """프로젝트 모듈의 `urlpatterns`를 하위 패턴으로 가져온다.

        Args:
            path: 모듈 경로.
            call: include 호출.
            conditional: 조건문 안인지.

        Returns:
            하위 패턴 목록.
        """
        patterns = self.reader.module_patterns(path)
        if patterns is None:
            return [
                self._opaque(
                    "{count} included URL modules could not be read or define no urlpatterns", call, conditional
                )
            ]
        return _mark_conditional(list(patterns)) if conditional else list(patterns)

    def _external_include(self, module_name: str, call: ast.AST, conditional: bool) -> Entry:
        """프로젝트 밖 모듈 include를 불투명 항목으로 만든다. 알려진 프레임워크 모듈은 framework 제공 경로다.

        Args:
            module_name: 모듈 이름.
            call: 위치 노드.
            conditional: 조건문 안인지.

        Returns:
            불투명 항목.
        """
        if module_name.startswith(("django.", "rest_framework.")):
            return self._framework(
                "framework URL modules included from Django or Django REST framework serve routes under their prefix",
                call,
                conditional,
                (),
            )
        return self._opaque("{count} included URL modules outside the project are not scanned", call, conditional)

    def _opaque(self, reason: str, node: ast.AST, conditional: bool) -> Opaque:
        """`route-coverage:` 불투명 항목을 만든다.

        Args:
            reason: 문장 틀.
            node: 위치 노드.
            conditional: 조건문 안인지.

        Returns:
            불투명 항목.
        """
        return Opaque(reason, "route-coverage:", self._location(node), conditional)

    def _framework(self, reason: str, node: ast.AST, conditional: bool, methods: tuple[str, ...]) -> Opaque:
        """`framework-provided-routes:` 불투명 항목을 만든다.

        Args:
            reason: 문장.
            node: 위치 노드.
            conditional: 조건문 안인지.
            methods: 받을 수 있는 동사 상한(모르면 빈 튜플).

        Returns:
            불투명 항목.
        """
        return Opaque(reason, "framework-provided-routes:", self._location(node), conditional, methods)

    def _location(self, node: ast.AST) -> Location:
        """노드 위치를 만든다.

        Args:
            node: 구문 노드.

        Returns:
            위치.
        """
        return node_location(self.path, node, self.has_bom)


def converter_regex(symbols: SymbolTable, evaluator: Evaluator, path: str, node: ast.expr) -> str | None:
    """변환기 클래스의 `regex` 클래스 속성을 정적으로 읽는다(프로젝트 기반 클래스 포함).

    Args:
        symbols: 이름 해석기.
        evaluator: 상수 평가기.
        path: 모듈 경로.
        node: 변환기 클래스 식.

    Returns:
        정규식 문자열, 읽지 못하면 None.
    """
    info = analyze_class(
        symbols, symbols.resolve_expr(path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
    )
    if info is None or "regex" not in info.attributes:
        return None
    module_path, value = info.attributes["regex"]
    return evaluator.string(module_path, value)


def _list_function(symbol: Symbol | None) -> str | None:
    """패턴 목록을 돌려주는 알려진 함수면 종류를 돌려준다.

    Args:
        symbol: 호출 대상 해석 결과.

    Returns:
        `static`·`staticfiles`·`i18n`·`suffix` 또는 None.
    """
    if is_external(symbol, "django.conf.urls.static.static"):
        return "static"
    if is_external(symbol, "django.contrib.staticfiles.urls.staticfiles_urlpatterns"):
        return "staticfiles"
    if is_external(symbol, "django.conf.urls.i18n.i18n_patterns"):
        return "i18n"
    if is_external(symbol, "rest_framework.urlpatterns.format_suffix_patterns"):
        return "suffix"
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    """호출의 키워드 인자 값을 찾는다.

    Args:
        call: 호출 식.
        name: 키워드 이름.

    Returns:
        값 식 또는 None.
    """
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _mark_conditional(entries: list[Entry] | list[object]) -> list[Entry]:
    """항목들을 조건부로 표시한다.

    Args:
        entries: 항목 목록.

    Returns:
        조건부로 표시한 목록.
    """
    return [_with_conditional(entry, True) for entry in entries if isinstance(entry, (Endpoint, Include, Opaque))]


def _with_conditional(entry: Entry, conditional: bool) -> Entry:
    """항목의 조건부 표시를 바꾼 사본을 만든다.

    Args:
        entry: 항목.
        conditional: 조건부 여부.

    Returns:
        사본.
    """
    return replace(entry, conditional=entry.conditional or conditional)
