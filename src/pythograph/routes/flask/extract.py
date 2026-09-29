"""Flask 프로젝트에서 route-decl을 추출한다.

규칙(Flask 3.1.3·Werkzeug 3.1.9 소스로 확인, `docs/HTTP-ROUTES.md`):

- `Flask(...)`·`Blueprint(...)` 객체를 모듈 수준과 함수(앱 팩토리) 안 대입에서 찾는다.
- 등록: `@X.route(rule, methods=…)`, `@X.get/post/put/delete/patch(rule)`(Flask 2.0+), `X.add_url_rule(rule,
  endpoint, view_func, methods=…)`, `X.register_blueprint(bp, url_prefix=…)`(중첩 블루프린트 포함).
- method: `methods`가 없으면 `view_func.methods` 또는 `("GET",)`(`App.add_url_rule`). GET이 있으면 HEAD를,
  `PROVIDE_AUTOMATIC_OPTIONS`면 OPTIONS를 Werkzeug·Flask가 더하므로 decl로 내지 않는다.
  `MethodView`는 정의한 핸들러 이름(대문자)과 기반 클래스 `methods`의 합집합이다(`__init_subclass__`).
- 접두사: 블루프린트 규칙은 `url_prefix.rstrip("/") + "/" + rule.lstrip("/")`(규칙이 비면 접두사 그대로),
  중첩은 부모 접두사와 같은 방식으로 잇는다(`BlueprintSetupState`, `Blueprint.register`).
- 끝 슬래시: `strict_slashes`(기본 True)면 한쪽만 핸들러에 닿고(끝 `/` 규칙은 다른 쪽을 308로 넘긴다) `strict`,
  False면 양쪽이 닿아 `optional`이다(`StateMachineMatcher.match`).
- Werkzeug는 정적 부분을 먼저, 동적 부분은 가중치 순으로 맞추고 method가 맞지 않으면 다른 규칙을 계속 찾는다.
  그래서 문서는 `dispatch: "specificity"`다.
- 앱 정적 파일 경로(`static_url_path + "/<path:filename>"`, GET)는 framework 제공 경로로 스코프를 둔다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.routes.classes import analyze_class, attribute_literal
from pythograph.routes.django.urlconf import converter_regex, node_location
from pythograph.routes.django.views import emitted_methods
from pythograph.routes.flask.rules import rule_alternatives
from pythograph.routes.model import Extraction, Location, RouteDecl, RouteShape, ScopeRange
from pythograph.routes.pattern import Unconvertible, dynamic_shape, skeleton_shapes
from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.project import is_test_path
from pythograph.source.symbols import ProjectSymbol, SymbolTable, ValueSymbol, is_external

#: `@X.<method>` 단축 장식자(Flask 2.0+)다.
_SHORTCUTS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}

#: `MethodView`가 핸들러로 보는 이름이다(`flask/views.py` `http_method_funcs`).
_HTTP_METHOD_FUNCS = ("get", "post", "head", "options", "delete", "put", "trace", "patch")

#: 라우트를 따로 등록하는 Flask 확장(모델링하지 않음)이다.
_ROUTING_EXTENSIONS = ("flask_restful", "flask_restx", "flask_smorest", "connexion", "flask_appbuilder", "flask_admin")


@dataclass
class _Object:
    """Flask 앱 또는 블루프린트 객체.

    Attributes:
        kind: `app` 또는 `blueprint`.
        key: (모듈 경로, 함수 점 경로, 변수 이름).
        url_prefix: 블루프린트 기본 접두사(없으면 None, 모르면 `UNKNOWN`).
        static: 정적 파일 URL 경로(없으면 None, 모르면 `UNKNOWN`).
        strict_slashes: 앱 `url_map.strict_slashes`(모르면 `UNKNOWN`).
        merge_slashes: 앱 `url_map.merge_slashes`.
        converters: 앱 사용자 변환기.
    """

    kind: str
    key: tuple[str, str, str]
    url_prefix: object = None
    static: object = None
    strict_slashes: object = True
    merge_slashes: object = True
    converters: dict[str, str | None] = field(default_factory=dict)
    rules: list[_Rule] = field(default_factory=list)
    children: list[tuple[_Object, object, Location]] = field(default_factory=list)


@dataclass(frozen=True)
class _Rule:
    """등록한 규칙 하나."""

    rule: str | None
    methods: tuple[str, ...] | None
    handlers: tuple[tuple[str, str | None, str], ...]
    strict_slashes: object
    location: Location
    uncertain: bool


@dataclass(frozen=True)
class FlaskOptions:
    """Flask 추출 옵션."""

    include_tests: bool


def extract_flask(symbols: SymbolTable, options: FlaskOptions) -> Extraction:
    """Flask 프로젝트의 route-decl을 추출한다.

    Args:
        symbols: 이름 해석기.
        options: 추출 옵션.

    Returns:
        추출 결과.
    """
    extraction = Extraction(dispatch="specificity", framework="flask")
    collector = _Collector(symbols, Evaluator(symbols), options.include_tests)
    collector.collect()
    registered: set[tuple[str, str, str]] = set()
    for app in collector.apps():
        _emit_object(app, None, extraction, app, registered, "root")
        _static_gap(app, None, extraction)
    for blueprint in collector.blueprints():
        if blueprint.key not in registered and blueprint.rules:
            extraction.add_gap(
                "unresolved-route-prefix:",
                "{count} Flask blueprints are registered in a way that "
                "is not modeled, so their routes are emitted with a base "
                "anchor",
            )
            _emit_object(blueprint, None, extraction, None, registered, "base")
    for gap in collector.gaps:
        extraction.add_gap(*gap)
    return extraction


def flask_detected(symbols: SymbolTable, include_tests: bool) -> bool:
    """프로젝트가 Flask 앱이나 블루프린트를 만드는지 확인한다.

    Args:
        symbols: 이름 해석기.
        include_tests: 테스트 파일도 볼지.

    Returns:
        찾으면 True.
    """
    collector = _Collector(symbols, Evaluator(symbols), include_tests)
    collector.collect_objects()
    return bool(collector.objects)


def _emit_object(
    obj: _Object,
    prefix: object,
    extraction: Extraction,
    app: _Object | None,
    registered: set[tuple[str, str, str]],
    anchor: str,
) -> None:
    """객체의 규칙과 등록한 하위 블루프린트를 선언으로 낸다.

    Args:
        obj: 앱 또는 블루프린트.
        prefix: 적용할 접두사(None이면 없음, `UNKNOWN`이면 모름).
        extraction: 채울 결과.
        app: 규칙을 담는 앱(변환기·슬래시 설정용, 등록되지 않은 블루프린트면 None).
        registered: 앱에 닿은 블루프린트 키(수정한다).
        anchor: 경로 앵커.
    """
    if obj.kind == "blueprint" and app is not None:
        registered.add(obj.key)
    for rule in obj.rules:
        _emit_rule(rule, prefix, extraction, app, anchor, is_test_path(rule.location.path))
    for child, option_prefix, location in obj.children:
        if app is None:
            continue
        child_prefix = _child_prefix(prefix, option_prefix, child.url_prefix)
        if child_prefix is UNKNOWN:
            extraction.add_gap(
                "unresolved-route-prefix:",
                "{count} Flask blueprint registrations use a url_prefix that is not a literal",
            )
        _emit_object(child, child_prefix, extraction, app, registered, "base" if child_prefix is UNKNOWN else anchor)
        _static_gap(child, child_prefix, extraction)
        del location


def _child_prefix(parent: object, option: object, own: object) -> object:
    """중첩 등록의 접두사를 정한다(`Blueprint.register`의 `bp_url_prefix` 규칙).

    Args:
        parent: 부모 등록의 접두사.
        option: `register_blueprint(url_prefix=…)` 값(없으면 None).
        own: 블루프린트 자신의 `url_prefix`.

    Returns:
        접두사(None, 문자열, 또는 `UNKNOWN`).
    """
    child = option if option is not None else own
    if child is UNKNOWN or parent is UNKNOWN:
        return UNKNOWN
    if parent is not None and child is not None:
        return str(parent).rstrip("/") + "/" + str(child).lstrip("/")
    return child if child is not None else parent


def _joined_rule(prefix: object, rule: str) -> str | None:
    """블루프린트 접두사와 규칙을 잇는다(`BlueprintSetupState.add_url_rule`).

    Args:
        prefix: 접두사(None이면 규칙 그대로, `UNKNOWN`이면 규칙만).
        rule: 규칙.

    Returns:
        전체 규칙.
    """
    if prefix is None or prefix is UNKNOWN:
        return rule if prefix is None else "/" + rule.lstrip("/")
    if not isinstance(prefix, str):
        return None
    return "/".join((prefix.rstrip("/"), rule.lstrip("/"))) if rule else prefix


def _emit_rule(
    rule: _Rule, prefix: object, extraction: Extraction, app: _Object | None, anchor: str, test_source: bool
) -> None:
    """규칙 하나를 선언으로 낸다.

    Args:
        rule: 규칙.
        prefix: 블루프린트 접두사.
        extraction: 채울 결과.
        app: 담는 앱(없으면 기본 설정).
        anchor: 경로 앵커.
        test_source: 테스트 소스인지.
    """
    shapes = _rule_shapes(rule, prefix, app)
    templates = tuple(shape.channel for shape in shapes if not shape.dynamic and shape.channel)
    for shape in shapes:
        if shape.dynamic:
            extraction.add_gap(
                "route-coverage:", "{count} Flask rules are emitted as dynamic: " + str(shape.coverage_reason)
            )
    if rule.uncertain:
        scope = ScopeRange(templates=templates) if anchor == "root" and len(templates) == len(shapes) else None
        extraction.add_gap(
            "route-coverage:",
            "{count} Flask views accept methods that could not be determined statically and were emitted as ANY",
            scope,
        )
    for shape in shapes:
        for method, usr, qualified_name in rule.handlers:
            if usr is None:
                extraction.add_gap("missing-route-usrs:", "{count} Flask route handlers have no pythograph symbol id")
            extraction.decls.append(
                RouteDecl(
                    method=method,
                    shape=shape,
                    path_anchor=anchor,
                    location=rule.location,
                    qualified_name=qualified_name,
                    usr=usr,
                    test_source=test_source,
                )
            )


def _rule_shapes(rule: _Rule, prefix: object, app: _Object | None) -> list[RouteShape]:
    """규칙을 정규 템플릿 판정으로 바꾼다.

    Args:
        rule: 규칙.
        prefix: 블루프린트 접두사.
        app: 담는 앱.

    Returns:
        판정 목록.
    """
    if rule.rule is None:
        return [dynamic_shape(None, "a Flask rule string is not a literal")]
    full = _joined_rule(prefix, rule.rule)
    if full is None:
        return [dynamic_shape(rule.rule, "a Flask blueprint url_prefix is not a string")]
    converters = app.converters if app is not None else {}
    merge = app.merge_slashes if app is not None else True
    strict = rule.strict_slashes if rule.strict_slashes is not UNKNOWN else UNKNOWN
    if strict is None:
        strict = app.strict_slashes if app is not None else True
    policy = None if not isinstance(strict, bool) else ("strict" if strict else "optional")
    try:
        alternatives = rule_alternatives(full, merge is not False, converters)
    except Unconvertible as error:
        return [dynamic_shape(full, str(error))]
    return skeleton_shapes(alternatives, raw=full, trailing_policy=policy)


def _static_gap(obj: _Object, prefix: object, extraction: Extraction) -> None:
    """앱·블루프린트 정적 파일 경로를 framework 제공 경로 공백으로 더한다.

    Args:
        obj: 앱 또는 블루프린트.
        prefix: 블루프린트 등록 접두사.
        extraction: 채울 결과.
    """
    if obj.static is None:
        return
    message = "Flask serves static files under the static URL path"
    if obj.static is UNKNOWN or prefix is UNKNOWN:
        extraction.add_gap("framework-provided-routes:", message)
        return
    path = _joined_rule(prefix, str(obj.static)) if obj.kind == "blueprint" else str(obj.static)
    if path is None:
        extraction.add_gap("framework-provided-routes:", message)
        return
    boundary = "/" + path.strip("/")
    extraction.add_gap(
        "framework-provided-routes:", message, ScopeRange(template_prefixes=(boundary,), methods=("GET", "HEAD"))
    )


class _Collector:
    """프로젝트 전체의 Flask 객체와 등록을 모은다."""

    def __init__(self, symbols: SymbolTable, evaluator: Evaluator, include_tests: bool) -> None:
        """수집기를 만든다.

        Args:
            symbols: 이름 해석기.
            evaluator: 상수 평가기.
            include_tests: 테스트 파일도 볼지.
        """
        self.symbols = symbols
        self.evaluator = evaluator
        self.include_tests = include_tests
        self.objects: dict[tuple[str, str, str], _Object] = {}
        self.gaps: list[tuple[str, str]] = []

    def _files(self) -> list[str]:
        """볼 파이썬 파일을 돌려준다.

        Returns:
            파일 경로 목록.
        """
        return [path for path in self.symbols.project.python_files() if self.include_tests or not is_test_path(path)]

    def apps(self) -> list[_Object]:
        """앱 객체를 돌려준다.

        Returns:
            앱 목록(키 순서).
        """
        return [obj for key, obj in sorted(self.objects.items()) if obj.kind == "app"]

    def blueprints(self) -> list[_Object]:
        """블루프린트 객체를 돌려준다.

        Returns:
            블루프린트 목록(키 순서).
        """
        return [obj for key, obj in sorted(self.objects.items()) if obj.kind == "blueprint"]

    def collect(self) -> None:
        """객체를 찾고 등록을 모은다."""
        self.collect_objects()
        for path in self._files():
            index = self.symbols.index(path)
            if index is None:
                continue
            self._scan_extensions(index.module.tree)
            _Registrar(self, path, index.module.has_bom).run(index.module.tree.body, "", {})

    def collect_objects(self) -> None:
        """모든 파일에서 `Flask(...)`·`Blueprint(...)` 대입을 찾는다."""
        for path in self._files():
            index = self.symbols.index(path)
            if index is None:
                continue
            self._objects_in(path, index.module.tree.body, "")

    def _objects_in(self, path: str, statements: list[ast.stmt], scope: str) -> None:
        """문장 목록(과 중첩 함수)에서 객체 대입을 찾는다.

        Args:
            path: 모듈 경로.
            statements: 문장 목록.
            scope: 함수 점 경로(모듈 수준은 빈 문자열).
        """
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = f"{scope}.{statement.name}" if scope else statement.name
                self._objects_in(path, statement.body, inner)
            elif isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
                kind = self._constructor(path, statement.value)
                for target in statement.targets:
                    if kind is not None and isinstance(target, ast.Name):
                        self._add_object(kind, (path, scope, target.id), statement.value)
            elif isinstance(statement, (ast.If, ast.Try, ast.With)):
                for block in _blocks(statement):
                    self._objects_in(path, block, scope)

    def _constructor(self, path: str, call: ast.Call) -> str | None:
        """`Flask(...)`·`Blueprint(...)` 호출이면 종류를 돌려준다.

        Args:
            path: 모듈 경로.
            call: 호출 식.

        Returns:
            `app`·`blueprint` 또는 None.
        """
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return None
        resolved = self.symbols.resolve_expr(path, call.func)
        if is_external(resolved, "flask.Flask", "flask.app.Flask"):
            return "app"
        if is_external(resolved, "flask.Blueprint", "flask.blueprints.Blueprint"):
            return "blueprint"
        return None

    def _add_object(self, kind: str, key: tuple[str, str, str], call: ast.Call) -> None:
        """객체를 만든다. 생성자 인자에서 `url_prefix`·정적 파일 경로를 읽는다.

        Args:
            kind: `app`·`blueprint`.
            key: 객체 키.
            call: 생성자 호출.
        """
        path = key[0]
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        obj = _Object(kind=kind, key=key)
        if kind == "blueprint":
            obj.url_prefix = self._optional_string(path, keywords.get("url_prefix"), 5, call)
            folder = self._optional_string(path, keywords.get("static_folder"), 2, call)
        else:
            folder = (
                self._optional_string(path, keywords.get("static_folder"), 2, call)
                if "static_folder" in keywords or len(call.args) > 2
                else "static"
            )
        obj.static = self._static_path(path, keywords, folder, call, 3 if kind == "blueprint" else 1)
        self.objects[key] = obj

    def _static_path(
        self, path: str, keywords: dict[str, ast.expr], folder: object, call: ast.Call, position: int
    ) -> object:
        """정적 파일 URL 경로를 정한다(`static_url_path`가 없으면 `/` + 폴더 이름).

        위치 인자 순번은 `Flask(import_name, static_url_path, static_folder, …)`면 1,
        `Blueprint(name, import_name, static_folder, static_url_path, …)`면 3이다.

        Args:
            path: 모듈 경로.
            keywords: 생성자 키워드 인자.
            folder: 정적 폴더 값.
            call: 생성자 호출.
            position: `static_url_path`의 위치 인자 순번.

        Returns:
            경로, 없으면 None, 모르면 `UNKNOWN`.
        """
        if folder is None:
            return None
        explicit = self._optional_string(
            path, keywords.get("static_url_path"), position if "static_url_path" not in keywords else -1, call
        )
        if explicit is not None:
            return explicit if explicit is UNKNOWN else str(explicit).rstrip("/")
        if folder is UNKNOWN:
            return UNKNOWN
        return "/" + str(folder).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    def _optional_string(self, path: str, node: ast.expr | None, position: int, call: ast.Call) -> object:
        """선택 문자열 인자를 평가한다.

        Args:
            path: 모듈 경로.
            node: 키워드 인자 식.
            position: 위치 인자 순번(키워드가 없을 때, -1이면 보지 않음).
            call: 생성자 호출.

        Returns:
            문자열, None, 또는 `UNKNOWN`.
        """
        if node is None and 0 <= position < len(call.args):
            node = call.args[position]
        if node is None:
            return None
        value = self.evaluator.value(path, node)
        if value is None or isinstance(value, str):
            return value
        return UNKNOWN

    def _scan_extensions(self, tree: ast.Module) -> None:
        """라우트를 따로 등록하는 Flask 확장 import를 찾는다.

        Args:
            tree: 모듈 구문 트리.
        """
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            if any(name.split(".")[0] in _ROUTING_EXTENSIONS for name in names):
                self.gaps.append(("route-coverage:", "Flask extensions that register their own routes are not modeled"))
                return


class _Registrar:
    """한 모듈의 등록 문장을 읽는 걷개."""

    def __init__(self, collector: _Collector, path: str, has_bom: bool) -> None:
        """걷개를 만든다.

        Args:
            collector: 상위 수집기.
            path: 모듈 경로.
            has_bom: BOM 여부.
        """
        self.collector = collector
        self.path = path
        self.has_bom = has_bom

    def run(self, statements: list[ast.stmt], scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """문장 목록을 걷는다.

        Args:
            statements: 문장 목록.
            scope: 함수 점 경로.
            loops: 반복 변수 → 펼친 원소 식.
        """
        for statement in statements:
            self._statement(statement, scope, loops)

    def _statement(self, statement: ast.stmt, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """문장 하나를 처리한다.

        Args:
            statement: 문장.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._decorated(statement, scope, loops)
            inner = f"{scope}.{statement.name}" if scope else statement.name
            self.run(statement.body, inner, loops)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            self._call(statement.value, scope, loops)
        elif isinstance(statement, ast.Assign):
            self._assignment(statement, scope, loops)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            self._loop(statement, scope, loops)
        elif isinstance(statement, (ast.If, ast.Try, ast.With)):
            for block in _blocks(statement):
                self.run(block, scope, loops)

    def _loop(self, statement: ast.For | ast.AsyncFor, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """리터럴 목록을 도는 반복문은 원소마다 몸체를 펼친다.

        Args:
            statement: 반복문.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        if isinstance(statement.target, ast.Name) and isinstance(statement.iter, (ast.List, ast.Tuple)):
            for element in statement.iter.elts:
                self.run(statement.body, scope, {**loops, statement.target.id: [element]})
            return
        self.run(statement.body, scope, loops)

    def _object(self, node: ast.expr, scope: str, loops: dict[str, list[ast.expr]]) -> _Object | None:
        """식이 가리키는 앱·블루프린트를 찾는다(반복 변수 → 함수 지역 → 바깥 함수 → 모듈 → import).

        Args:
            node: 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.

        Returns:
            객체 또는 None.
        """
        if isinstance(node, ast.Name) and node.id in loops and loops[node.id]:
            return self._object(loops[node.id][0], scope, {})
        if isinstance(node, ast.Name):
            parts = scope.split(".") if scope else []
            for depth in range(len(parts), -1, -1):
                found = self.collector.objects.get((self.path, ".".join(parts[:depth]), node.id))
                if found is not None:
                    return found
        if isinstance(node, (ast.Name, ast.Attribute)):
            symbol = self.collector.symbols.resolve_expr(self.path, node)
            if isinstance(symbol, ValueSymbol):
                return self.collector.objects.get((symbol.path, "", symbol.name))
        return None

    def _decorated(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, scope: str, loops: dict[str, list[ast.expr]]
    ) -> None:
        """`@X.route(...)`·`@X.get(...)` 장식자를 등록으로 기록한다.

        Args:
            function: 함수 정의.
            scope: 함수가 정의된 점 경로.
            loops: 반복 변수 바인딩.
        """
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
                continue
            name = decorator.func.attr
            if name != "route" and name not in _SHORTCUTS:
                continue
            owner = self._object(decorator.func.value, scope, loops)
            if owner is None:
                continue
            qualname = f"{scope}.{function.name}" if scope else function.name
            usr = f"{self.path}#{qualname}"
            self._add_rule(owner, decorator, _SHORTCUTS.get(name), [("*", usr, usr)], None, False, loops)

    def _call(self, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """`X.add_url_rule`·`X.register_blueprint`·`X.url_map.converters.update` 호출을 처리한다.

        Args:
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        func = call.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr == "update" and _is_map_attribute(func.value, "converters"):
            owner = self._object(func.value.value.value, scope, loops)  # type: ignore[attr-defined]
            if owner is not None and call.args and isinstance(call.args[0], ast.Dict):
                for key, value in zip(call.args[0].keys, call.args[0].values, strict=False):
                    self._converter(owner, key, value)
            return
        owner = self._object(func.value, scope, loops)
        if owner is None:
            return
        if func.attr == "add_url_rule":
            self._add_url_rule(owner, call, scope, loops)
        elif func.attr == "register_blueprint" and call.args:
            child = self._object(call.args[0], scope, loops)
            keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
            prefix = self.collector._optional_string(self.path, keywords.get("url_prefix"), -1, call)
            if child is not None:
                owner.children.append((child, prefix, node_location(self.path, call, self.has_bom)))

    def _assignment(self, statement: ast.Assign, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """`X.url_map.strict_slashes = …`·`X.url_map.merge_slashes = …`·`X.url_map.converters[n] = C`를 처리한다.

        Args:
            statement: 대입문.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        for target in statement.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr in ("strict_slashes", "merge_slashes")
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "url_map"
            ):
                owner = self._object(target.value.value, scope, loops)
                if owner is not None:
                    value = self.collector.evaluator.value(self.path, statement.value)
                    setattr(owner, target.attr, value if isinstance(value, bool) else UNKNOWN)
            elif isinstance(target, ast.Subscript) and _is_map_attribute(target.value, "converters"):
                owner = self._object(target.value.value.value, scope, loops)  # type: ignore[attr-defined]
                if owner is not None:
                    self._converter(owner, target.slice, statement.value)

    def _converter(self, owner: _Object, key: ast.expr | None, value: ast.expr) -> None:
        """사용자 변환기를 등록한다.

        Args:
            owner: 앱.
            key: 변환기 이름 식.
            value: 변환기 클래스 식.
        """
        name = self.collector.evaluator.string(self.path, key)
        if name is not None:
            owner.converters[name] = converter_regex(self.collector.symbols, self.collector.evaluator, self.path, value)

    def _add_url_rule(self, owner: _Object, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """`X.add_url_rule(rule, endpoint=None, view_func=None, **options)`를 기록한다.

        Args:
            owner: 앱·블루프린트.
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        view = (
            call.args[2]
            if len(call.args) > 2
            else next((keyword.value for keyword in call.keywords if keyword.arg == "view_func"), None)
        )
        handlers, view_methods, uncertain = self._view_handlers(view, scope)
        self._add_rule(owner, call, None, handlers, view_methods, uncertain, loops)

    def _view_handlers(
        self, view: ast.expr | None, scope: str
    ) -> tuple[list[tuple[str, str | None, str]], tuple[str, ...] | None, bool]:
        """`view_func`의 핸들러와 뷰가 선언한 method를 구한다.

        Args:
            view: `view_func` 식.
            scope: 함수 점 경로.

        Returns:
            (method별 핸들러 틀, 뷰의 methods 또는 None, 확정 불가 여부).
        """
        if isinstance(view, ast.Call) and isinstance(view.func, ast.Attribute) and view.func.attr == "as_view":
            return self._class_handlers(view.func.value)
        if isinstance(view, ast.Name):
            local = self._local_function(view.id, scope)
            if local is not None:
                return [("*", local, local)], None, False
        symbol = (
            self.collector.symbols.resolve_expr(self.path, view)
            if isinstance(view, (ast.Name, ast.Attribute))
            else None
        )
        if isinstance(symbol, ProjectSymbol) and isinstance(symbol.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [("*", symbol.id, symbol.id)], None, False
        return [("*", None, "unresolved-view")], None, view is not None

    def _local_function(self, name: str, scope: str) -> str | None:
        """같은 함수 안에 정의한 함수면 그 id를 돌려준다.

        Args:
            name: 함수 이름.
            scope: 함수 점 경로.

        Returns:
            심볼 id 또는 None.
        """
        if not scope:
            return None
        index = self.collector.symbols.index(self.path)
        if index is None:
            return None
        for node in ast.walk(index.module.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                qualname = index.qualname(node)
                if qualname == f"{scope}.{name}":
                    return f"{self.path}#{qualname}"
        return None

    def _class_handlers(
        self, owner_node: ast.expr
    ) -> tuple[list[tuple[str, str | None, str]], tuple[str, ...] | None, bool]:
        """`Cls.as_view(name)`의 method와 핸들러를 구한다.

        Args:
            owner_node: 클래스 식.

        Returns:
            (method별 핸들러 틀, methods, 확정 불가 여부).
        """
        symbol = (
            self.collector.symbols.resolve_expr(self.path, owner_node)
            if isinstance(owner_node, (ast.Name, ast.Attribute))
            else None
        )
        info = analyze_class(self.collector.symbols, symbol)
        if info is None or not isinstance(info.symbol, ProjectSymbol):
            return [("*", None, "unresolved-view")], (), True
        base_id = info.symbol.id
        dispatch = f"{base_id}.dispatch_request"
        declared = attribute_literal(info, "methods")
        if isinstance(declared, (list, tuple, set)) and all(isinstance(item, str) for item in declared):
            methods = tuple(str(item).upper() for item in declared)
            return self._method_view_handlers(info.names, base_id, info.kinds), methods, info.unknown_bases
        if declared is not None:
            return [("*", None, "unresolved-view")], (), True
        if "flask-methodview" in info.kinds:
            names = [name for name in _HTTP_METHOD_FUNCS if name in info.names]
            methods = tuple(name.upper() for name in names)
            return self._method_view_handlers(info.names, base_id, info.kinds), (methods or None), info.unknown_bases
        return [("*", dispatch, dispatch)], None, info.unknown_bases

    @staticmethod
    def _method_view_handlers(names: set[str], base_id: str, kinds: set[str]) -> list[tuple[str, str | None, str]]:
        """클래스 뷰의 method별 핸들러 틀을 만든다.

        `View`는 `dispatch_request`가 모든 method를 처리한다(`*`). `MethodView`는 정의한 `get`·`post`… 핸들러로
        보내고, 핸들러가 없는 method는 `dispatch_request`의 단정문에서 실패하므로 usr 없는 `dispatch_request`로
        둔다(빈 키). HEAD는 `get`으로 넘어간다(`MethodView.dispatch_request`).

        Args:
            names: 클래스 사슬에서 정의한 이름.
            base_id: 등록한 클래스 id.
            kinds: 프레임워크 뷰 종류.

        Returns:
            (method 또는 `*`·빈 키, usr, qualifiedName) 목록.
        """
        dispatch = f"{base_id}.dispatch_request"
        if "flask-methodview" not in kinds:
            return [("*", dispatch, dispatch)]
        handlers: list[tuple[str, str | None, str]] = [
            (name.upper(), f"{base_id}.{name}", f"{base_id}.{name}") for name in _HTTP_METHOD_FUNCS if name in names
        ]
        handlers.append(("", None, dispatch))
        return handlers

    def _add_rule(
        self,
        owner: _Object,
        call: ast.Call,
        shortcut: str | None,
        handlers: list[tuple[str, str | None, str]],
        view_methods: tuple[str, ...] | None,
        uncertain: bool,
        loops: dict[str, list[ast.expr]],
    ) -> None:
        """규칙을 기록한다. method는 `methods` 인자 → 단축 장식자 → 뷰의 methods → GET 순서다.

        Args:
            owner: 앱·블루프린트.
            call: 장식자·`add_url_rule` 호출.
            shortcut: 단축 장식자 동사.
            handlers: 핸들러 틀(`*`는 모든 method, 빈 키는 핸들러 없는 method).
            view_methods: 뷰의 methods.
            uncertain: 확정 불가 여부.
            loops: 반복 변수 바인딩(리터럴 목록을 도는 반복의 규칙 문자열).
        """
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        rule_node = call.args[0] if call.args else keywords.get("rule")
        if isinstance(rule_node, ast.Name) and loops.get(rule_node.id):
            rule_node = loops[rule_node.id][0]
        rule = self.collector.evaluator.string(self.path, rule_node)
        strict = self._strict(keywords)
        methods, uncertain = self._methods(keywords, shortcut, view_methods, uncertain)
        resolved = _bind_handlers(handlers, methods)
        location = node_location(self.path, rule_node if rule_node is not None else call, self.has_bom)
        owner.rules.append(_Rule(rule, methods, tuple(resolved), strict, location, uncertain))

    def _strict(self, keywords: dict[str, ast.expr]) -> object:
        """규칙의 `strict_slashes` 옵션을 읽는다.

        Args:
            keywords: 키워드 인자.

        Returns:
            불리언, 없으면 None, 모르면 `UNKNOWN`.
        """
        if "strict_slashes" not in keywords:
            return None
        value = self.collector.evaluator.value(self.path, keywords["strict_slashes"])
        return value if isinstance(value, bool) or value is None else UNKNOWN

    def _methods(
        self, keywords: dict[str, ast.expr], shortcut: str | None, view_methods: tuple[str, ...] | None, uncertain: bool
    ) -> tuple[tuple[str, ...] | None, bool]:
        """규칙이 받는 method를 정한다.

        Args:
            keywords: 키워드 인자.
            shortcut: 단축 장식자 동사.
            view_methods: 뷰의 methods.
            uncertain: 지금까지의 확정 불가 여부.

        Returns:
            (method 튜플 또는 None(모름), 확정 불가 여부).
        """
        if shortcut is not None:
            return (shortcut,), uncertain
        if "methods" in keywords:
            listed = self.collector.evaluator.string_list(self.path, keywords["methods"])
            if listed is None:
                return None, True
            return tuple(item.upper() for item in listed), uncertain
        if view_methods == ():
            # 뷰 클래스의 methods를 확정하지 못했다(빈 튜플 표식). ANY로 낸다.
            return None, True
        return (view_methods or ("GET",)), uncertain


def _bind_handlers(
    handlers: list[tuple[str, str | None, str]], methods: tuple[str, ...] | None
) -> list[tuple[str, str | None, str]]:
    """method 목록에 핸들러를 붙인다.

    `*` 핸들러(함수 뷰, `View.dispatch_request`)는 모든 method를 처리한다. 그 밖(`MethodView`)은 method별
    핸들러, HEAD는 GET 핸들러, 핸들러가 없는 method는 빈 키 항목(usr 없음)이다.

    Args:
        handlers: 핸들러 틀.
        methods: method 튜플(None이면 `ANY`).

    Returns:
        (method, usr, qualifiedName) 목록.
    """
    by_method = {method: (usr, name) for method, usr, name in handlers}
    missing = by_method.get("*", by_method.get("", (None, "unresolved-view")))
    if methods is None:
        return [("ANY", *missing)]
    result: list[tuple[str, str | None, str]] = []
    for method in emitted_methods(list(methods)):
        usr, name = by_method.get(method, by_method.get("GET", missing) if method == "HEAD" else missing)
        result.append((method, usr, name))
    return result


def _is_map_attribute(node: ast.expr, name: str) -> bool:
    """식이 `X.url_map.<name>` 모양인지 확인한다.

    Args:
        node: 식.
        name: 마지막 속성 이름.

    Returns:
        맞으면 True.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "url_map"
    )


def _blocks(statement: ast.If | ast.Try | ast.With) -> list[list[ast.stmt]]:
    """조건·try·with 문의 블록을 돌려준다.

    Args:
        statement: 문장.

    Returns:
        블록 목록.
    """
    if isinstance(statement, ast.If):
        return [statement.body, statement.orelse]
    if isinstance(statement, ast.With):
        return [statement.body]
    return [statement.body, *[handler.body for handler in statement.handlers], statement.orelse, statement.finalbody]
