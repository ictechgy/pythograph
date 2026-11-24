"""Flask 프로젝트에서 route-decl을 추출한다.

규칙(Flask 3.1.3·Werkzeug 3.1.9 소스로 확인, `docs/HTTP-ROUTES.md`):

- `Flask(...)`·`Blueprint(...)` 객체를 모듈 수준과 함수(앱 팩토리) 안 대입에서 찾는다.
- 등록: `@X.route(rule, methods=…)`, `@X.get/post/put/delete/patch(rule)`(Flask 2.0+), 장식자를 직접 부른
  `X.route(rule)(view)`, `X.add_url_rule(rule, endpoint, view_func, methods=…)`,
  `X.register_blueprint(bp, url_prefix=…)`(중첩 블루프린트 포함). 함수·클래스 본문과 모든 블록 문장 안을 걷는다.
- method: `methods`가 없으면 `view_func.methods` 또는 `("GET",)`이고 `view_func.required_methods`를 더한다
  (`App.add_url_rule`). 그래서 증명하지 못한 뷰 식과 그 속성을 다는 함수(`view_options`)는 GET이 아니라 `ANY`다. GET이
  있으면 HEAD를, `PROVIDE_AUTOMATIC_OPTIONS`면 OPTIONS를 Werkzeug·Flask가 더하므로 decl로 내지 않는다. `MethodView`는
  정의한 핸들러 이름(대문자)과 기반 클래스 `methods`의 합집합이다(`__init_subclass__`).
- 앱·블루프린트로 찾지 못한 수신자의 등록은 사실 대신 `route-coverage:`로 센다(`_UNMODELED_REGISTRATIONS`).
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
import builtins
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field

from pythograph.exchange.document import MAX_FACTS
from pythograph.exchange.methods import ROUTE_DECL_METHODS
from pythograph.routes.classes import analyze_class, attribute_literal
from pythograph.routes.django.urlconf import converter_regex, node_location
from pythograph.routes.django.views import emitted_methods
from pythograph.routes.flask.rules import rule_alternatives
from pythograph.routes.flask.view_options import ProjectOptionWrites, scan_option_writes, stores_options
from pythograph.routes.model import Extraction, Location, RouteDecl, RouteShape, ScopeRange
from pythograph.routes.pattern import (
    Unconvertible,
    dynamic_gap_prefix,
    dynamic_shape,
    skeleton_shapes,
    unconvertible_shape,
)
from pythograph.routes.unsupported import unsupported_framework
from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.project import is_test_path
from pythograph.source.symbols import (
    ExternalSymbol,
    ImportBinding,
    MemberSymbol,
    ModuleSymbol,
    ProjectSymbol,
    Symbol,
    SymbolTable,
    ValueSymbol,
    absolute_module,
    is_external,
)

#: `@X.<method>` 단축 장식자(Flask 2.0+)다.
_SHORTCUTS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}

#: `MethodView`가 핸들러로 보는 이름이다(`flask/views.py` `http_method_funcs`).
_HTTP_METHOD_FUNCS = ("get", "post", "head", "options", "delete", "put", "trace", "patch")

if sys.version_info >= (3, 11):  # pragma: no cover - 실행 중인 파이썬 버전에 따라 한쪽만 탄다.
    _TRY_STATEMENTS: tuple[type[ast.stmt], ...] = (ast.Try, ast.TryStar)
else:  # pragma: no cover
    _TRY_STATEMENTS = (ast.Try,)

#: 안쪽 블록의 등록·객체를 그대로 걷는 문장이다(`try … except*`·`match`·`while`·`async with` 포함). 조건부 등록도
#: 평범한 등록으로 낸다(Django와 다른 기존 동작, HANDOFF "Flask 조건부 등록").
_BLOCK_STATEMENTS: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.While,
    ast.Match,
    *_TRY_STATEMENTS,
)

#: 본문을 따로 걷는 정의 문장이다.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: 테스트 대역 모듈이다. 그 이름으로 풀리는 수신자의 단축 장식자(`mock.patch`)는 라우트 등록이 아니다.
_MOCK_MODULES = ("mock", "unittest.mock")

#: 내장 이름이다. 프로젝트가 다시 묶지 않은 `staticmethod`·`property` 같은 장식자는 프로젝트 밖 이름이다.
_BUILTIN_NAMES = frozenset(dir(builtins))

#: 규칙을 등록하는 앱·블루프린트 메서드 이름이다(`_sweep`이 처리하지 못한 사용을 센다).
_REGISTRATION_METHODS = frozenset({"route", "add_url_rule", *_SHORTCUTS})

#: 프로젝트 함수·클래스나 장식자에 묶지 못한 뷰 함수 옵션 속성 쓰기의 limitation 문장 틀이다(`view_options`). 그 쓰기가
#: 어느 뷰에 닿는지 모르므로 스코프를 싣지 않는다. 모두 `ANY`로 내리면 정밀도를 지나치게 잃어 세기만 한다.
_UNTIED_OPTION_WRITES = (
    "{count} writes of the Flask view options methods or required_methods go through a name that is not a project "
    "function or class (an alias, loop variable, parameter, __dict__ or vars, functools.wraps or update_wrapper), so "
    "views registered without methods may accept other methods than the emitted GET"
)

#: 사실이 담지 못하는 호스트·리다이렉트 등록 옵션이다(규칙·블루프린트·`register_blueprint`).
_HOST_KEYWORDS = frozenset({"subdomain", "host", "redirect_to"})

#: 앱 생성자의 호스트 매칭 옵션이다.
_APP_HOST_KEYWORDS = frozenset({"host_matching", "subdomain_matching", "static_host"})

#: 호스트·리다이렉트 옵션 limitation 문장 틀이다. 계약에서 host는 조인 키가 아니므로 경로만 같은 다른 호스트의 호출과
#: 잇거나, `redirect_to` 규칙은 핸들러 대신 리다이렉트로 끝날 수 있다.
_HOST_OPTIONS = (
    "{count} Flask registrations use subdomain, host, redirect_to, host_matching, subdomain_matching, or static_host, "
    "which route facts do not model; "
    "those routes are emitted as plain paths, so a call to another host or a redirected rule may be matched"
)


def _uses_host_option(keywords: dict[str, ast.expr], names: frozenset[str]) -> bool:
    """호스트·리다이렉트 옵션 중 끈 값(`False`·`None`·빈 문자열 리터럴)이 아닌 것을 주었는지 본다.

    `host_matching=False`·`subdomain=None`은 Flask 기본값과 같아 경로 사실이 그대로 맞다. 값이 리터럴이 아니면 켜졌을 수
    있으므로 센다.

    Args:
        keywords: 호출의 키워드 인자 이름 → 값 식.
        names: 볼 옵션 이름.

    Returns:
        켜졌거나 켜졌을 수 있는 옵션이 있으면 True.
    """
    return any(name in keywords and not _disabled_literal(keywords[name]) for name in names)


def _disabled_literal(node: ast.expr) -> bool:
    """옵션 값이 끈 리터럴(`False`·`None`·빈 문자열)인지 본다.

    Args:
        node: 값 식.

    Returns:
        끈 리터럴이면 True.
    """
    return isinstance(node, ast.Constant) and (node.value is False or node.value is None or node.value == "")


#: 라우트를 따로 등록하는 Flask 확장(모델링하지 않음)이다.
_ROUTING_EXTENSIONS = ("flask_restful", "flask_restx", "flask_smorest", "connexion", "flask_appbuilder", "flask_admin")

#: 한 번의 추출에서 펼치는 하위 블루프린트 등록(등록 경로마다 하나)의 상한이다. Flask는 같은 블루프린트를 여러 경로로
#: 등록하는 격자를 허용하고 경로마다 규칙을 다시 등록하므로 깊이 d 격자는 등록이 2^d개다. 등록마다 계약 동사 선언이
#: 하나 이상이면 사실 상한(MAX_FACTS)이 먼저 걸리도록 그 두 배로 둔다. 규칙이 없거나 계약 밖 동사뿐인 등록도 이 상한에서
#: 멈춘다.
MAX_BLUEPRINT_REGISTRATIONS = 2 * MAX_FACTS

#: 순환을 닫는 하위 블루프린트 등록의 limitation 문장 틀이다.
_REGISTRATION_CYCLE = (
    "{count} Flask blueprint registrations close a registration cycle that Flask cannot complete, "
    "so they were not followed"
)

#: 하위 블루프린트 등록 펼침 상한에 걸렸을 때의 limitation 문장 틀이다(`{limit}`은 상한).
_REGISTRATIONS_CAPPED = (
    "Flask blueprint registrations expand to more than {limit} registrations "
    "(the same blueprints registered along many paths), so the remaining registrations were not followed"
)

#: 다시 묶이는 모듈 이름(`SymbolTable.proven`이 증명하지 못함)을 거친 블루프린트 등록의 limitation 문장 틀이다. 그
#: 이름이 실행 경로에 따라 다른 앱·블루프린트를 가리킬 수 있어 등록 접두사를 정적으로 정하지 않는다.
_REBOUND_REGISTRATIONS = (
    "{count} Flask blueprint registrations go through a module name that may refer to another app or blueprint "
    "(bound more than once or conditionally), so their routes are emitted with a base anchor"
)

#: 다시 묶이는 모듈 이름에 단 규칙이 root 앵커였을 자리의 limitation 문장 틀이다. 규칙이 어느 객체에 붙는지
#: 정적으로 정하지 못한다(먼저 묶인 객체에 단 규칙은 앱에 닿지 않을 수 있다).
_REBOUND_RULES = (
    "{count} Flask rules are registered through a module name that may refer to another app or blueprint "
    "(bound more than once or conditionally), so they are emitted with a base anchor"
)

#: 앱·블루프린트로 찾지 못한 수신자(함수 매개변수, `self.app` 같은 속성, `create_app()` 결과를 담은 이름, Flask 하위
#: 클래스 인스턴스, 다른 패키지에서 import한 앱)나 모델링하지 않은 모양(앱·블루프린트를 모르는 `X.route(rule)(view)`)의
#: 등록 limitation 문장 틀이다. 전에는 한계 없이 사라져 빈 문서가 완전한 결과처럼 읽혔다. 그 객체가 블루프린트일 수
#: 있어 접두사를 모르므로 스코프를 싣지 않는다. 지원하지 않는 서버 프레임워크의 객체는 그 import 한계가 이미 센다.
_UNMODELED_REGISTRATIONS = (
    "{count} route registrations (route, add_url_rule or a method shortcut) go through an object that is not a known "
    "Flask app or blueprint (a parameter, attribute, call result, Flask subclass instance or app imported from another "
    "package) or use a form that is not modeled, so their routes were not emitted"
)


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
        rules: 이 객체에 단 규칙.
        children: 등록한 하위 블루프린트(블루프린트, 등록 옵션 `url_prefix`, 위치, 등록이 거친 이름이 모두 정적 객체
            하나로 증명되는지).
    """

    kind: str
    key: tuple[str, str, str]
    url_prefix: object = None
    static: object = None
    strict_slashes: object = True
    merge_slashes: object = True
    converters: dict[str, str | None] = field(default_factory=dict)
    rules: list[_Rule] = field(default_factory=list)
    children: list[tuple[_Object, object, Location, bool]] = field(default_factory=list)


@dataclass(frozen=True)
class _Rule:
    """등록한 규칙 하나.

    Attributes:
        rebound: 규칙을 단 앱·블루프린트 이름이 정적 객체 하나로 증명되지 않는다(다시 묶이는 모듈 이름).
    """

    rule: str | None
    methods: tuple[str, ...] | None
    handlers: tuple[tuple[str, str | None, str], ...]
    strict_slashes: object
    location: Location
    uncertain: bool
    rebound: bool = False


@dataclass(frozen=True)
class FlaskOptions:
    """Flask 추출 옵션."""

    include_tests: bool


@dataclass
class _Emission:
    """규칙을 선언으로 내는 동안의 상태.

    Attributes:
        extraction: 채울 결과.
        registered: 앱에 닿은 블루프린트 키.
        registrations: 지금까지 펼친 하위 블루프린트 등록 수.
        capped: 등록 펼침 상한에 걸렸는지.
        counted: 계약 동사 선언을 센 `extraction.decls` 앞부분 길이.
        facts: 그 앞부분의 계약 동사 선언 수.
    """

    extraction: Extraction
    registered: set[tuple[str, str, str]] = field(default_factory=set)
    registrations: int = 0
    capped: bool = False
    counted: int = 0
    facts: int = 0

    def may_register(self) -> bool:
        """하위 블루프린트 등록을 하나 더 펼쳐도 되는지 정한다.

        계약 동사 선언이 이미 사실 상한을 넘었으면 어차피 문서를 낼 수 없어(조립에서 사실 상한 오류, 종료 2) 더
        펼치지 않는다. 계약 밖 동사 선언은 조립 전에 빠지므로 세지 않는다. 펼친 등록이 상한에 닿으면 한계를 한 번
        남기고 멈춘다.

        Returns:
            펼쳐도 되면 True.
        """
        if self._facts_over_limit():
            return False
        if self.registrations >= MAX_BLUEPRINT_REGISTRATIONS:
            if not self.capped:
                self.capped = True
                self.extraction.add_gap(
                    "route-coverage:", _REGISTRATIONS_CAPPED.format(limit=MAX_BLUEPRINT_REGISTRATIONS)
                )
            return False
        self.registrations += 1
        return True

    def _facts_over_limit(self) -> bool:
        """지금까지 낸 계약 동사 선언이 사실 상한을 넘었는지 본다(지난번 뒤에 더한 선언만 센다).

        Returns:
            넘었으면 True.
        """
        decls = self.extraction.decls
        self.facts += sum(1 for decl in decls[self.counted :] if decl.method in ROUTE_DECL_METHODS)
        self.counted = len(decls)
        return self.facts > MAX_FACTS


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
    emission = _Emission(extraction)
    for app in collector.apps():
        _emit_object(app, None, emission, app, "root")
        _static_gap(app, None, extraction)
    for blueprint in collector.blueprints():
        if blueprint.key not in emission.registered and blueprint.rules:
            extraction.add_gap(
                "unresolved-route-prefix:",
                "{count} Flask blueprints are registered in a way that "
                "is not modeled, so their routes are emitted with a base "
                "anchor",
            )
            _emit_object(blueprint, None, emission, None, "base")
    for gap in collector.gaps:
        extraction.add_gap(*gap)
    for _ in range(collector.option_writes().untied):
        extraction.add_gap("route-coverage:", _UNTIED_OPTION_WRITES)
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
    emission: _Emission,
    app: _Object | None,
    anchor: str,
    path: tuple[tuple[str, str, str], ...] = (),
) -> None:
    """객체의 규칙과 등록한 하위 블루프린트를 선언으로 낸다.

    등록 경로(앱부터 이 객체까지)에 이미 있는 객체를 다시 등록하면 순환이다. Flask는 그런 앱을 등록하다 실패하므로
    (자기 등록은 ValueError, 서로 등록은 끝없는 재귀) 따라가지 않고 한계로 센다. 같은 블루프린트를 다른 경로로 다시
    등록하는 격자는 Flask가 허용해 그대로 펼치되 `_Emission.may_register`의 상한에서 멈춘다. 다시 묶이는 모듈 이름을
    거친 등록은 그 이름이 다른 객체를 가리킬 수 있으므로 접두사를 모르는 등록처럼 base 앵커로 낸다.

    Args:
        obj: 앱 또는 블루프린트.
        prefix: 적용할 접두사(None이면 없음, `UNKNOWN`이면 모름).
        emission: 선언을 내는 상태(수정한다).
        app: 규칙을 담는 앱(변환기·슬래시 설정용, 등록되지 않은 블루프린트면 None).
        anchor: 경로 앵커.
        path: 이 객체를 등록한 경로의 객체 키(앱부터).
    """
    extraction = emission.extraction
    if obj.kind == "blueprint" and app is not None:
        emission.registered.add(obj.key)
    for rule in obj.rules:
        _emit_rule(rule, prefix, extraction, app, anchor, is_test_path(rule.location.path))
    path = (*path, obj.key)
    for child, option_prefix, _location, proven in obj.children:
        if app is None:
            return
        if child.key in path:
            extraction.add_gap("route-coverage:", _REGISTRATION_CYCLE)
            continue
        if not emission.may_register():
            return
        child_prefix = _child_prefix(prefix, option_prefix, child.url_prefix) if proven else UNKNOWN
        if not proven:
            extraction.add_gap("unresolved-route-prefix:", _REBOUND_REGISTRATIONS)
        elif child_prefix is UNKNOWN:
            extraction.add_gap(
                "unresolved-route-prefix:",
                "{count} Flask blueprint registrations use a url_prefix that is not a literal",
            )
        _emit_object(child, child_prefix, emission, app, "base" if child_prefix is UNKNOWN else anchor, path)
        _static_gap(child, child_prefix, extraction)


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
    """규칙 하나를 선언으로 낸다. 다시 묶이는 모듈 이름에 단 규칙(`_Rule.rebound`)은 root 대신 base 앵커로 낸다.

    Args:
        rule: 규칙.
        prefix: 블루프린트 접두사.
        extraction: 채울 결과.
        app: 담는 앱(없으면 기본 설정).
        anchor: 경로 앵커.
        test_source: 테스트 소스인지.
    """
    if rule.rebound and anchor == "root":
        # 규칙을 단 이름이 다른 객체를 가리킬 수 있다. 등록 쪽이 이미 base면 그 한계가 같은 사실을 덮는다.
        anchor = "base"
        extraction.add_gap("unresolved-route-prefix:", _REBOUND_RULES)
    shapes = _rule_shapes(rule, prefix, app)
    templates = tuple(shape.channel for shape in shapes if not shape.dynamic and shape.channel)
    for shape in shapes:
        if shape.dynamic:
            extraction.add_gap(
                dynamic_gap_prefix(shape), "{count} Flask rules are emitted as dynamic: " + str(shape.coverage_reason)
            )
    if rule.uncertain:
        scope = ScopeRange(templates=templates) if anchor == "root" and len(templates) == len(shapes) else None
        extraction.add_gap(
            "route-coverage:",
            "{count} Flask views accept methods that could not be determined statically and were emitted as ANY "
            "or as their known methods",
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
        return [unconvertible_shape(full, error)]
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
        # 알려진 객체 이름이다(객체를 모두 모은 뒤 처음 쓸 때 만든다).
        self._object_names: frozenset[str] | None = None
        # 프로젝트 전체의 뷰 함수 옵션 속성 쓰기 요약이다(처음 쓸 때 한 번 만든다).
        self._option_writes: ProjectOptionWrites | None = None
        # 장식자 정의 → 옵션 속성을 쓰는지다. 같은 장식자를 쓰는 등록마다 본문을 다시 걷지 않는다(등록 수 × 본문 크기).
        self._stores: dict[ast.AST, bool] = {}

    def object_names(self) -> frozenset[str]:
        """알려진 앱·블루프린트를 묶은 이름들이다(`_Registrar._sweep`이 수신자 후보를 거른다).

        Returns:
            이름 집합.
        """
        if self._object_names is None:
            self._object_names = frozenset(key[2] for key in self.objects)
        return self._object_names

    def option_writes(self) -> ProjectOptionWrites:
        """프로젝트(볼 파일) 전체의 뷰 함수 옵션 속성 쓰기를 돌려준다(`view_options.scan_option_writes`).

        Returns:
            쓰기 요약.
        """
        if self._option_writes is None:
            indexes = [self.symbols.index(path) for path in self._files()]
            self._option_writes = scan_option_writes(index.module.tree for index in indexes if index is not None)
        return self._option_writes

    def stores(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda) -> bool:
        """장식자 정의가 옵션 속성을 쓰는지 기억해 둔 결과로 돌려준다(`view_options.stores_options`).

        Args:
            node: 장식자 함수·클래스·람다.

        Returns:
            쓰면 True.
        """
        if node not in self._stores:
            self._stores[node] = stores_options(node)
        return self._stores[node]

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
            registrar = _Registrar(self, path, index.module.has_bom)
            registrar.run(index.module.tree.body, "", {})
            registrar.sweep(index.module.tree)

    def collect_objects(self) -> None:
        """모든 파일에서 `Flask(...)`·`Blueprint(...)` 대입을 찾는다."""
        for path in self._files():
            index = self.symbols.index(path)
            if index is None:
                continue
            self._objects_in(path, index.module.tree.body, "")

    def _objects_in(self, path: str, statements: list[ast.stmt], scope: str) -> None:
        """문장 목록(과 중첩 함수, 클래스의 메서드)에서 객체 대입을 찾는다.

        클래스 본문의 대입(`class C: app = Flask(...)`)은 메서드에서 이름으로 보이지 않으므로(`self.app`으로 읽는다)
        객체로 두지 않고 메서드·중첩 클래스만 걷는다.

        Args:
            path: 모듈 경로.
            statements: 문장 목록.
            scope: 함수 점 경로(모듈 수준은 빈 문자열).
        """
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{statement.name}" if scope else statement.name
                body = statement.body
                if isinstance(statement, ast.ClassDef):
                    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    body = [member for member in statement.body if isinstance(member, nested)]
                self._objects_in(path, body, inner)
            elif isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
                kind = self._constructor(path, statement.value)
                for target in statement.targets:
                    if kind is not None and isinstance(target, ast.Name):
                        self._add_object(kind, (path, scope, target.id), statement.value)
            elif isinstance(statement, _BLOCK_STATEMENTS):
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

        같은 키에 다시 대입하면 마지막 객체가 남는다. 모듈 수준 이름이 다시 묶이면(`ModuleIndex.rebound`) 그 이름은
        실행 경로에 따라 다른 객체(다른 묶음은 Flask 객체가 아닐 수도 있다)를 가리킬 수 있으므로 정적 파일 경로를 이
        호출의 값과 관계없이 모르는 값으로 둔다. 규칙과 등록은 그 이름을 거칠 때마다 `_Registrar._lookup`이 같은
        판정으로 base 앵커를 고른다.

        Args:
            kind: `app`·`blueprint`.
            key: 객체 키.
            call: 생성자 호출.
        """
        path = key[0]
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        obj = _Object(kind=kind, key=key)
        if _uses_host_option(keywords, _HOST_KEYWORDS if kind == "blueprint" else _APP_HOST_KEYWORDS):
            self.gaps.append(("route-coverage:", _HOST_OPTIONS))
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
        if not key[1] and self.module_name_rebound(path, key[2]):
            obj.static = UNKNOWN
        self.objects[key] = obj

    def module_name_rebound(self, path: str, name: str) -> bool:
        """모듈 수준 이름이 정적 객체 하나를 증명하지 못하는지 본다(상수 평가와 같은 묶음 판정).

        Args:
            path: 모듈 경로.
            name: 모듈 수준 이름.

        Returns:
            다시 묶이면(또는 모듈을 읽지 못했으면) True.
        """
        index = self.symbols.index(path)
        return index is None or index.rebound(name)

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
        # 함수 안 import 묶음((함수 점 경로, 이름) → 묶음)이다. 앱 팩토리가 블루프린트를 함수 안에서 import하는 흔한
        # 구조(`from app.auth import bp as auth_bp`)를 따라가려고 모은다. 모듈 수준 import는 이름 해석기가 다룬다.
        # 값이 None이면 그 함수에서 import가 아닌 방식(대입·반복 변수 등)으로 다시 묶였다는 뜻이다(모듈 이름도 가린다).
        self.local_imports: dict[tuple[str, str], ImportBinding | None] = {}
        # 함수 안에서 지원하지 않는 서버 프레임워크 생성자로 묶은 지역 이름((함수 점 경로, 이름))이다. 그 객체의 등록은
        # 프레임워크 import 한계가 이미 센다.
        self.foreign_locals: set[tuple[str, str]] = set()
        # 메서드 안 `self.X = FastAPI()`처럼 지원하지 않는 서버 프레임워크 객체를 담은 `self`·`cls` 속성 이름이다.
        self.foreign_attributes: set[str] = set()
        # 클래스 본문 범위(어휘적 점 경로)다. 클래스 본문의 이름은 그 본문에서만 보이고 메서드에서는 보이지 않는다.
        self.class_scopes: set[str] = set()
        # 함수·클래스 본문의 단순 이름 대입 값((범위, 이름) → 값 식, 두 번 이상 묶이면 None)이다. 지역 장식자
        # (`limiter = Limiter(app)` 뒤 `@limiter.limit(…)`)가 프로젝트 밖 객체인지 본다.
        self.local_values: dict[tuple[str, str], ast.expr | None] = {}
        # 안쪽 함수의 `nonlocal`이 다시 묶는 바깥 함수 지역 이름((범위, 이름))이다. 그 객체는 실행 경로에 따라 바뀐다.
        self.rebound_locals: set[tuple[str, str]] = set()
        # 처리한(사실로 냈거나 센) 등록 메서드 속성 식의 id다. `_sweep`이 그 밖의 등록 사용을 센다.
        self.handled: set[int] = set()
        # 모듈 안 함수 정의(어휘적 점 경로 → 정의들)다. `view_func=이름` 등록마다 구문 트리 전체를 걷지 않도록 처음
        # 쓸 때 만든다.
        self._function_nodes: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] | None = None

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
        # 바다코끼리 대입(`if (bp := other):`)도 그 함수의 지역 묶음이다.
        self._shadow(_walrus_targets(statement), scope)
        if isinstance(statement, ast.Nonlocal):
            self._nonlocal(statement.names, scope)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 클래스 본문과 메서드도 걷는다(확장의 `init_app(self, app)`, 클래스 본문의 `@app.route`). 매개변수는 바깥
            # 이름을 가리는 지역 묶음이다.
            inner = f"{scope}.{statement.name}" if scope else statement.name
            if isinstance(statement, ast.ClassDef):
                self.class_scopes.add(inner)
            else:
                self._decorated(statement, scope, loops)
                for name in _parameter_names(statement.args):
                    self.local_imports[(inner, name)] = None
                    self.local_values[(inner, name)] = None
            self.run(statement.body, inner, loops)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            self._call(statement.value, scope, loops)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if isinstance(statement.value, ast.Call):
                self._call_form(statement.value, scope, loops)
                self._foreign_local(targets, statement.value, scope)
            self._shadow(targets, scope)
            self._remember_values(targets, statement, scope)
            if isinstance(statement, ast.Assign):
                self._assignment(statement, scope, loops)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            self._shadow([statement.target], scope)
            self._loop(statement, scope, loops)
        elif isinstance(statement, _BLOCK_STATEMENTS):
            self._shadow(_block_targets(statement), scope)
            for block in _blocks(statement):
                self.run(block, scope, loops)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)) and scope:
            self._local_import(statement, scope)

    def _nonlocal(self, names: list[str], scope: str) -> None:
        """`nonlocal` 이름이 가리키는 바깥 함수 지역 이름을 다시 묶이는 이름으로 기록한다(그 객체로 풀지 않는다).

        Args:
            names: `nonlocal` 이름.
            scope: `nonlocal`을 쓴 함수 점 경로.
        """
        for name in names:
            for prefix in self._visible(scope)[1:]:
                if (self.path, prefix, name) in self.collector.objects or (prefix, name) in self.local_imports:
                    self.rebound_locals.add((prefix, name))
                    break

    def _remember_values(self, targets: list[ast.expr], statement: ast.stmt, scope: str) -> None:
        """함수·클래스 본문의 단순 이름 대입 값을 기록한다(누적 대입이나 두 번째 묶음이면 모르는 값).

        Args:
            targets: 대입 대상 식.
            statement: 대입문.
            scope: 함수 점 경로.
        """
        value = getattr(statement, "value", None)
        if not scope:
            return
        for target in targets:
            if isinstance(target, ast.Name):
                key = (scope, target.id)
                once = isinstance(statement, (ast.Assign, ast.AnnAssign)) and key not in self.local_values
                self.local_values[key] = value if once else None

    def sweep(self, tree: ast.Module) -> None:
        """모듈을 다 걸은 뒤 처리하지 못한 등록 메서드 사용을 모델링하지 않은 등록으로 센다.

        `return app.route(rule)(view)`, 목록 원소·인자 안 등록, 바다코끼리, `x = app.get` 뒤 `x(…)(view)`, 클래스에 단
        `@app.route`처럼 걷개의 모양 밖에서 알려진 앱·블루프린트의 `route`·`add_url_rule`·단축 장식자를 쓰면 사실도
        한계도 없이 사라졌다. 수신자 끝 이름이 알려진 객체 이름인 속성만 찾아본다(사전 `get` 같은 흔한 이름을 모두 풀지
        않는다). 리터럴 목록을 도는 반복 변수 수신자는 풀지 않는다.

        Args:
            tree: 걸은 모듈 구문 트리.
        """
        for node, scope in _registration_uses(tree, "", self.collector.object_names()):
            if id(node) not in self.handled and self._lookup(node.value, scope, {})[0] is not None:
                self.collector.gaps.append(("route-coverage:", _UNMODELED_REGISTRATIONS))

    def _local_import(self, statement: ast.Import | ast.ImportFrom, scope: str) -> None:
        """함수 안 import 문을 지역 묶음으로 기록한다(`*`와 풀 수 없는 상대 import는 건너뛴다).

        Args:
            statement: import 문.
            scope: 함수 점 경로.
        """
        index = self.collector.symbols.index(self.path)
        if index is None:
            return
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                module = alias.name if alias.asname else alias.name.split(".")[0]
                self.local_imports[(scope, alias.asname or module)] = ImportBinding(module, None)
            return
        base = absolute_module(index.module, statement.module, statement.level)
        for alias in statement.names:
            if base is not None and alias.name != "*":
                self.local_imports[(scope, alias.asname or alias.name)] = ImportBinding(base, alias.name)

    def _shadow(self, targets: list[ast.expr], scope: str) -> None:
        """함수 안에서 import가 아닌 방식으로 묶은 이름을 기록한다(그 뒤로는 import 묶음·모듈 이름을 가린다).

        Args:
            targets: 대입·반복·with 대상 식.
            scope: 함수 점 경로.
        """
        if not scope:
            return
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    self.local_imports[(scope, node.id)] = None

    def _local_binding(self, name: str, scope: str) -> ImportBinding | None | bool:
        """함수 안 묶음을 찾는다. 안쪽 함수부터 바깥 함수로 가며 처음 묶은 범위의 것을 쓴다.

        Args:
            name: 이름.
            scope: 함수 점 경로.

        Returns:
            import 묶음, 지역에서 import가 아닌 방식으로 묶였으면 True, 지역 묶음이 없으면 False.
        """
        for prefix in self._visible(scope):
            key = (prefix, name)
            if key in self.local_imports:
                binding = self.local_imports[key]
                return True if binding is None else binding
        return False

    def _visible(self, scope: str) -> list[str]:
        """범위에서 이름을 찾는 함수·클래스 범위를 안쪽부터 돌려준다(모듈 제외).

        파이썬처럼 바깥 클래스 본문은 건너뛴다: 메서드는 클래스 본문의 이름을 이름으로 보지 못한다. 현재 범위가 클래스
        본문이면 그 본문은 본다.

        Args:
            scope: 함수 점 경로.

        Returns:
            범위 점 경로 목록.
        """
        parts = scope.split(".") if scope else []
        prefixes: list[str] = []
        for depth in range(len(parts), 0, -1):
            prefix = ".".join(parts[:depth])
            if depth == len(parts) or prefix not in self.class_scopes:
                prefixes.append(prefix)
        return prefixes

    def _resolve(self, node: ast.expr, scope: str) -> Symbol | None:
        """이름·속성 식을 해석한다. 맨 앞 이름이 함수 안 import로 묶였으면 그 묶음으로만 푼다(모듈 이름을 가린다).

        Args:
            node: 이름 또는 속성 식.
            scope: 함수 점 경로.

        Returns:
            해석 결과 또는 None.
        """
        if isinstance(node, ast.Attribute):
            root: ast.expr = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and self._local_binding(root.id, scope) is not False:
                return self.collector.symbols.member(self._resolve(node.value, scope), node.attr)
        if isinstance(node, ast.Name):
            binding = self._local_binding(node.id, scope)
            if isinstance(binding, ImportBinding):
                return self.collector.symbols.resolve_binding(binding)
            if binding is True:
                return None
        return self.collector.symbols.resolve_expr(self.path, node)

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
        else:
            self.run(statement.body, scope, loops)
        self.run(statement.orelse, scope, loops)

    def _object(self, node: ast.expr, scope: str, loops: dict[str, list[ast.expr]]) -> _Object | None:
        """식이 가리키는 앱·블루프린트를 찾는다(`_lookup`의 객체만).

        Args:
            node: 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.

        Returns:
            객체 또는 None.
        """
        return self._lookup(node, scope, loops)[0]

    def _lookup(self, node: ast.expr, scope: str, loops: dict[str, list[ast.expr]]) -> tuple[_Object | None, bool]:
        """식이 가리키는 앱·블루프린트와, 거친 이름이 정적 객체 하나로 증명되는지를 함께 찾는다.

        찾는 순서는 반복 변수(리터럴 목록을 도는 반복의 원소) → 함수 지역 → 바깥 함수 → 모듈 → import다. 모듈 수준
        이름은 상수 평가와 같은 묶음 판정(`ModuleIndex.rebound`, import·모듈 별칭으로 거쳐 간 묶음까지는
        `SymbolTable.proven`)을 지나야 증명된다. 두 번 이상·조건부로 묶인 이름은 실행 경로에 따라 다른 객체를 가리킬
        수 있기 때문이다. 함수 지역 이름(앱 팩토리)은 판정하지 않는다.

        Args:
            node: 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.

        Returns:
            (객체 또는 None, 증명되는지). 객체가 없으면 증명 여부는 True다.
        """
        if isinstance(node, ast.Name) and node.id in loops and loops[node.id]:
            return self._lookup(loops[node.id][0], scope, {})
        if isinstance(node, ast.Name):
            for prefix in self._visible(scope):
                if (prefix, node.id) in self.rebound_locals:
                    return None, True
                found = self.collector.objects.get((self.path, prefix, node.id))
                if found is not None:
                    return found, True
                if (prefix, node.id) in self.local_imports:
                    if self.local_imports[(prefix, node.id)] is None:
                        # 매개변수·지역 대입·클래스 본문 대입이 바깥 객체를 가린다(그 값은 모른다).
                        return None, True
                    break
            else:
                found = self.collector.objects.get((self.path, "", node.id))
                if found is not None:
                    return found, not self.collector.module_name_rebound(self.path, node.id)
        if isinstance(node, (ast.Name, ast.Attribute)):
            symbol = self._resolve(node, scope)
            if isinstance(symbol, ValueSymbol):
                found = self.collector.objects.get((symbol.path, "", symbol.name))
                return found, found is None or self.collector.symbols.proven(symbol)
        return None, True

    def _unmodeled(self, receiver: ast.expr, scope: str) -> None:
        """앱·블루프린트로 찾지 못한 수신자의 등록(`route`·`add_url_rule`·단축 장식자)을 한계로 센다.

        테스트 대역(`mock.patch` 장식자)과 지원하지 않는 서버 프레임워크의 이름·인스턴스(`bottle.route`, `FastAPI()`·
        `APIRouter()`·`Quart()`로 만든 객체)는 세지 않는다(`_foreign`). 그 밖은 모두 센다: 함수 매개변수, `self.app`,
        호출 결과, Flask 하위 클래스 인스턴스, 다른 패키지에서 import한 앱, `flask.current_app`.

        Args:
            receiver: 등록 메서드의 수신자 식.
            scope: 함수 점 경로.
        """
        if not self._foreign(receiver, scope):
            self.collector.gaps.append(("route-coverage:", _UNMODELED_REGISTRATIONS))

    def _foreign(self, receiver: ast.expr, scope: str) -> bool:
        """수신자가 테스트 대역이거나 지원하지 않는 서버 프레임워크의 이름·인스턴스인지 본다.

        그 프레임워크의 라우트는 `unsupported_framework_gap`이 import한 모듈 수로 이미 센다. 모듈 수준 인스턴스는 마지막
        무조건 대입의 생성자로, 함수 지역 인스턴스는 그 함수의 대입(`_foreign_local`)으로 본다.

        Args:
            receiver: 등록 메서드의 수신자 식.
            scope: 함수 점 경로.

        Returns:
            세지 않을 수신자면 True.
        """
        if isinstance(receiver, ast.Name) and any(
            (p, receiver.id) in self.foreign_locals for p in self._visible(scope)
        ):
            return True
        if (
            isinstance(receiver, ast.Attribute)
            and _is_self_attribute(receiver)
            and receiver.attr in self.foreign_attributes
        ):
            return True
        symbol = self._resolve(receiver, scope) if isinstance(receiver, (ast.Name, ast.Attribute)) else None
        if isinstance(symbol, ExternalSymbol):
            return _foreign_name(symbol.dotted)
        if isinstance(symbol, ValueSymbol) and isinstance(symbol.node, ast.Call):
            built = symbol.node.func
            constructor = (
                self.collector.symbols.resolve_expr(symbol.path, built)
                if isinstance(built, (ast.Name, ast.Attribute))
                else None
            )
            return isinstance(constructor, ExternalSymbol) and unsupported_framework(constructor.dotted) is not None
        return False

    def _foreign_local(self, targets: list[ast.expr], call: ast.Call, scope: str) -> None:
        """지원하지 않는 서버 프레임워크 생성자로 묶은 지역 이름·`self` 속성을 기록한다(`r: APIRouter = APIRouter()`,
        메서드 안 `self.router = APIRouter()`).

        Args:
            targets: 대입 대상 식.
            call: 대입 값 호출.
            scope: 함수 점 경로.
        """
        if not scope or not isinstance(call.func, (ast.Name, ast.Attribute)):
            return
        constructor = self._resolve(call.func, scope)
        if not (isinstance(constructor, ExternalSymbol) and unsupported_framework(constructor.dotted) is not None):
            return
        for target in targets:
            if isinstance(target, ast.Name):
                self.foreign_locals.add((scope, target.id))
            elif _is_self_attribute(target):
                assert isinstance(target, ast.Attribute)
                self.foreign_attributes.add(target.attr)

    def _decorated(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, scope: str, loops: dict[str, list[ast.expr]]
    ) -> None:
        """`@X.route(...)`·`@X.get(...)` 장식자를 등록으로 기록한다.

        Args:
            function: 함수 정의.
            scope: 함수가 정의된 점 경로.
            loops: 반복 변수 바인딩.
        """
        for position, decorator in enumerate(function.decorator_list):
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
                continue
            name = decorator.func.attr
            if name != "route" and name not in _SHORTCUTS:
                continue
            self.handled.add(id(decorator.func))
            owner, proven = self._lookup(decorator.func.value, scope, loops)
            if owner is None:
                self._unmodeled(decorator.func.value, scope)
                continue
            qualname = f"{scope}.{function.name}" if scope else function.name
            usr = f"{self.path}#{qualname}"
            # 장식자는 아래부터 적용되므로 등록하는 함수는 이 장식자 아래 장식자들이 감싼 것이다(위 장식자는 등록 뒤다).
            below = function.decorator_list[position + 1 :]
            uncertain = self._decorators_store_options(self.path, below, scope)
            self._add_rule(
                owner, decorator, _SHORTCUTS.get(name), [("*", usr, usr)], None, uncertain, loops, rebound=not proven
            )

    def _decorators_store_options(self, path: str, decorators: list[ast.expr], scope: str) -> bool:
        """장식자 중 뷰 함수 옵션 속성을 달 수 있는 것이 있는지 본다(`_decorator_may_set_options`).

        Args:
            path: 장식자 식이 있는 모듈 경로.
            decorators: 장식자 식 목록.
            scope: 장식자 식을 평가하는 범위(다른 모듈이면 모듈 수준).

        Returns:
            있으면 True.
        """
        return any(self._decorator_may_set_options(decorator, path, scope) for decorator in decorators)

    def _decorator_may_set_options(self, decorator: ast.expr, path: str, scope: str) -> bool:
        """장식자(또는 장식자 팩토리)가 옵션 속성을 다는지, 프로젝트 장식자인데 풀지 못하는지 본다.

        프로젝트 함수·클래스(본문의 `methods = …`와 `__call__` 포함)·람다 장식자는 본문을 보고, 범위 안 지역 함수(앱
        팩토리 안 정의)·클래스 본문 정의·`Decos.post_only` 같은 클래스 멤버도 같다. 프로젝트 밖 이름과 그 생성자로 만든
        객체의 메서드(`login_required`, `cache.cached`, 지역 `limiter = Limiter(app)`의 `limiter.limit`)는 옵션을 달지
        않는다고 본다. 매개변수처럼 무엇인지 모르는 장식자는 단다고 본다.

        Args:
            decorator: 장식자 식.
            path: 장식자 식이 있는 모듈 경로.
            scope: 장식자 식을 평가하는 범위.

        Returns:
            달 수 있으면 True.
        """
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Lambda):
            return self.collector.stores(target)
        if isinstance(target, ast.Attribute) and target.attr in _REGISTRATION_METHODS:
            # 겹친 등록 장식자(`@app.route` 아래 `@bp.get`)는 받은 함수를 그대로 돌려준다.
            return False
        if not isinstance(target, (ast.Name, ast.Attribute)):
            return True
        if path == self.path and isinstance(target, ast.Name):
            local = self._visible_function(target.id, scope)
            if local is not None:
                return any(self.collector.stores(node) for node in local[1])
            if self._local_binding(target.id, scope) is True:
                return self._value_may_set_options(self._local_value(target.id, scope), path, scope)
        symbol = (
            self._resolve(target, scope) if path == self.path else self.collector.symbols.resolve_expr(path, target)
        )
        if symbol is None and isinstance(target, ast.Attribute):
            return self._owner_may_set_options(target.value, path, scope)
        if symbol is None and isinstance(target, ast.Name) and target.id in _BUILTIN_NAMES:
            return False
        return self._symbol_may_set_options(symbol, path)

    def _owner_may_set_options(self, owner: ast.expr, path: str, scope: str) -> bool:
        """`X.attr` 장식자의 `X`가 무엇인지로 판정한다(프로젝트 클래스면 그 본문, 프로젝트 밖 객체면 달지 않는다).

        프로젝트 함수가 돌려준 객체(`guard = make_guard()`의 `guard.allow`)는 클래스를 모르므로 달 수 있다고 본다.

        Args:
            owner: 속성 앞 식.
            path: 모듈 경로.
            scope: 범위.

        Returns:
            달 수 있으면 True.
        """
        if path == self.path and isinstance(owner, ast.Name) and self._local_binding(owner.id, scope) is True:
            return self._value_may_set_options(self._local_value(owner.id, scope), path, scope, owner=True)
        if not isinstance(owner, (ast.Name, ast.Attribute)):
            return True
        symbol = self._resolve(owner, scope) if path == self.path else self.collector.symbols.resolve_expr(path, owner)
        if isinstance(symbol, ModuleSymbol):
            return True
        return self._symbol_may_set_options(symbol, path, owner=True)

    def _symbol_may_set_options(self, symbol: Symbol | None, path: str, owner: bool = False) -> bool:
        """해석한 장식자(또는 그 주인)가 옵션 속성을 달 수 있는지 본다.

        Args:
            symbol: 해석 결과.
            path: 모듈 경로.
            owner: `X.attr` 장식자의 주인 `X`를 보는지.

        Returns:
            달 수 있으면 True.
        """
        definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if isinstance(symbol, (ExternalSymbol, ModuleSymbol)):
            return False
        if isinstance(symbol, ProjectSymbol) and isinstance(symbol.node, definitions):
            return self.collector.stores(symbol.node)
        if isinstance(symbol, MemberSymbol) and isinstance(symbol.owner.node, ast.ClassDef):
            # `Decos.post_only` 같은 프로젝트 클래스 멤버는 그 클래스 본문 전체로 판정한다.
            return self.collector.stores(symbol.owner.node)
        if isinstance(symbol, ValueSymbol):
            return self._value_may_set_options(symbol.node, symbol.path, "", owner=owner)
        return True

    def _value_may_set_options(self, value: ast.expr | None, path: str, scope: str, owner: bool = False) -> bool:
        """변수에 담은 장식자 값(람다, 생성자·팩토리 호출 결과)이 옵션 속성을 달 수 있는지 본다.

        값을 장식자로 쓰면(`deco = make_deco()` 뒤 `@deco`) 팩토리 본문(안쪽 함수 포함)을 보고, 값의 속성을 장식자로
        쓰면(`owner`, `guard = make_guard()` 뒤 `@guard.allow(…)`) 프로젝트 함수가 돌려준 객체의 클래스를 모르므로 달 수
        있다고 본다. 프로젝트 클래스의 인스턴스면 그 클래스 본문, 프로젝트 밖 생성자의 결과면 달지 않는다.

        Args:
            value: 대입 값 식(모르면 None).
            path: 값이 있는 모듈 경로.
            scope: 값을 평가한 범위.
            owner: 값의 속성을 장식자로 쓰는지.

        Returns:
            달 수 있으면 True.
        """
        if isinstance(value, ast.Lambda):
            return self.collector.stores(value)
        if isinstance(value, ast.Call) and isinstance(value.func, (ast.Name, ast.Attribute)):
            func = value.func
            built = self._resolve(func, scope) if path == self.path else self.collector.symbols.resolve_expr(path, func)
            functions = (ast.FunctionDef, ast.AsyncFunctionDef)
            if owner and isinstance(built, ProjectSymbol) and isinstance(built.node, functions):
                return True
            return self._symbol_may_set_options(built, path) if built is not None else True
        return True

    def _local_value(self, name: str, scope: str) -> ast.expr | None:
        """범위 안 지역 이름의 대입 값을 찾는다(매개변수·여러 번 묶인 이름·없으면 None).

        Args:
            name: 이름.
            scope: 범위.

        Returns:
            값 식 또는 None.
        """
        for prefix in self._visible(scope):
            if (prefix, name) in self.local_imports:
                return self.local_values.get((prefix, name))
        return None

    def _visible_function(
        self, name: str, scope: str
    ) -> tuple[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] | None:
        """범위에서 보이는 지역 함수 정의(앱 팩토리 안·클래스 본문의 정의)를 찾는다. 지역 묶음이 가리면 없다.

        Args:
            name: 이름.
            scope: 범위.

        Returns:
            (점 경로, 정의들) 또는 None.
        """
        for prefix in self._visible(scope):
            qualname = f"{prefix}.{name}"
            found = self._functions().get(qualname)
            if found:
                return qualname, found
            if (prefix, name) in self.local_imports:
                return None
        return None

    def _function_options_unknown(
        self, path: str, functions: list[ast.FunctionDef | ast.AsyncFunctionDef], used_name: str | None, scope: str
    ) -> bool:
        """`view_func`로 넘긴 프로젝트 함수의 옵션 속성(`methods`·`required_methods`)을 모르는지 본다.

        프로젝트 어디서든 그 함수 이름이나 등록에서 넘긴 이름의 옵션 속성을 쓰거나(`view_options`), 그 함수의 장식자가
        옵션을 달 수 있으면 모른다. 쓰는 순서(등록 앞인지 뒤인지)는 따지지 않는다.

        Args:
            path: 함수를 정의한 모듈 경로.
            functions: 같은 점 경로의 정의들(재정의 포함).
            used_name: 등록에서 넘긴 이름의 끝 이름(`views.index`면 `index`).
            scope: 함수를 정의한 범위(장식자를 평가하는 범위).

        Returns:
            모르면 True.
        """
        targets = self.collector.option_writes().targets
        if any(function.name in targets for function in functions) or used_name in targets:
            return True
        return any(self._decorators_store_options(path, function.decorator_list, scope) for function in functions)

    def _call(self, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]]) -> None:
        """`X.add_url_rule`·`X.register_blueprint`·`X.url_map.converters.update` 호출을 처리한다.

        `X`를 앱·블루프린트로 찾지 못한 `add_url_rule`은 모델링하지 않은 등록으로 센다. `X.route(rule)(view)` 모양은
        `_call_form`이 맡는다.

        Args:
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
        """
        func = call.func
        if self._call_form(call, scope, loops) or not isinstance(func, ast.Attribute):
            return
        if func.attr == "update" and _is_map_attribute(func.value, "converters"):
            owner = self._object(func.value.value.value, scope, loops)  # type: ignore[attr-defined]
            if owner is not None and call.args and isinstance(call.args[0], ast.Dict):
                for key, value in zip(call.args[0].keys, call.args[0].values, strict=False):
                    self._converter(owner, key, value)
            return
        if func.attr == "add_url_rule":
            self.handled.add(id(func))
        owner, owner_proven = self._lookup(func.value, scope, loops)
        if owner is None:
            if func.attr == "add_url_rule":
                self._unmodeled(func.value, scope)
            return
        if func.attr == "add_url_rule":
            self._add_url_rule(owner, call, scope, loops, rebound=not owner_proven)
        elif func.attr == "register_blueprint" and call.args:
            child, child_proven = self._lookup(call.args[0], scope, loops)
            keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
            if _uses_host_option(keywords, _HOST_KEYWORDS):
                self.collector.gaps.append(("route-coverage:", _HOST_OPTIONS))
            prefix = self.collector._optional_string(self.path, keywords.get("url_prefix"), -1, call)
            if child is not None:
                location = node_location(self.path, call, self.has_bom)
                owner.children.append((child, prefix, location, owner_proven and child_proven))

    def _call_form(self, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]]) -> bool:
        """장식자를 직접 부른 등록 `X.route(rule)(view)`·`X.get(rule)(view)`를 처리한다(문장·대입·주석 대입의 값).

        Flask의 `route`·단축 장식자는 `add_url_rule(rule, endpoint, view, **options)`를 부르는 함수를 돌려주므로 `X`가
        알려진 앱·블루프린트면 `view_func=view`인 `add_url_rule`과 같게 낸다. `X`를 모르면 `route`만 모델링하지 않은
        등록으로 센다. 모르는 수신자의 `get(…)(…)`은 사전 조회 결과 호출(`handlers.get(name)(request)`)과 구별할 수 없어
        보지 않는다.

        Args:
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.

        Returns:
            그런 모양이면 True.
        """
        return self._call_form_view(call, scope, loops)[0]

    def _call_form_view(
        self, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]]
    ) -> tuple[bool, ast.expr | None]:
        """`_call_form`의 본체다. 등록한 뷰 식을 돌려줘 겹친 호출(`app.route(a)(app.route(b)(view))`)이 안쪽 뷰를 쓴다.

        Flask의 장식자는 받은 함수를 그대로 돌려주므로 바깥 등록의 뷰는 안쪽 등록의 뷰다.

        Args:
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.

        Returns:
            (그런 모양인지, 알려진 앱·블루프린트에 등록한 뷰 식 — 센 등록이면 None).
        """
        inner = call.func
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
            return False, None
        name = inner.func.attr
        if name != "route" and name not in _SHORTCUTS:
            return False, None
        owner, proven = self._lookup(inner.func.value, scope, loops)
        if owner is None:
            if name != "route":
                return False, None
            self.handled.add(id(inner.func))
            self._unmodeled(inner.func.value, scope)
            return True, None
        self.handled.add(id(inner.func))
        plain = len(call.args) == 1 and not call.keywords and not isinstance(call.args[0], ast.Starred)
        if not plain:
            self.collector.gaps.append(("route-coverage:", _UNMODELED_REGISTRATIONS))
            return True, None
        view: ast.expr | None = call.args[0]
        if isinstance(view, ast.Call):
            nested, inner_view = self._call_form_view(view, scope, loops)
            view = inner_view if nested else view
        handlers, view_methods, uncertain = self._view_handlers(view, scope)
        if view is None:
            # 안쪽 등록의 뷰를 모른다(모르는 수신자): 받는 method도 모른다.
            uncertain = True
        self._add_rule(owner, inner, _SHORTCUTS.get(name), handlers, view_methods, uncertain, loops, rebound=not proven)
        return True, view

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

    def _add_url_rule(
        self, owner: _Object, call: ast.Call, scope: str, loops: dict[str, list[ast.expr]], rebound: bool
    ) -> None:
        """`X.add_url_rule(rule, endpoint=None, view_func=None, **options)`를 기록한다.

        Args:
            owner: 앱·블루프린트.
            call: 호출 식.
            scope: 함수 점 경로.
            loops: 반복 변수 바인딩.
            rebound: `X`가 다시 묶이는 모듈 이름이라 객체를 증명하지 못하는지.
        """
        view = (
            call.args[2]
            if len(call.args) > 2
            else next((keyword.value for keyword in call.keywords if keyword.arg == "view_func"), None)
        )
        handlers, view_methods, uncertain = self._view_handlers(view, scope)
        self._add_rule(owner, call, None, handlers, view_methods, uncertain, loops, rebound=rebound)

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
        if isinstance(view, ast.Call) and _is_as_view(view):
            assert isinstance(view.func, ast.Attribute)
            return self._class_handlers(view.func.value)
        if isinstance(view, ast.Lambda) or (isinstance(view, ast.Constant) and view.value is None):
            # 그 자리에서 만든 람다와 None은 `methods` 속성이 없어 `add_url_rule`이 GET을 쓴다(핸들러 id만 없다).
            return [("*", None, "unresolved-view")], None, False
        if isinstance(view, ast.Name) and scope:
            local = self._visible_function(view.id, scope)
            if local is not None:
                qualname, nodes = local
                usr = f"{self.path}#{qualname}"
                defined_in = qualname.rsplit(".", 1)[0]
                return [("*", usr, usr)], None, self._function_options_unknown(self.path, nodes, view.id, defined_in)
        # 매개변수·지역 대입이 가린 이름은 모듈 정의로 풀지 않는다(`_resolve`가 지역 묶음을 본다).
        symbol = self._resolve(view, scope) if isinstance(view, (ast.Name, ast.Attribute)) else None
        used = view.id if isinstance(view, ast.Name) else view.attr if isinstance(view, ast.Attribute) else None
        if isinstance(symbol, ProjectSymbol) and isinstance(symbol.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            unknown = self._function_options_unknown(symbol.path, [symbol.node], used, "")
            return [("*", symbol.id, symbol.id)], None, unknown
        if isinstance(symbol, ValueSymbol) and _is_as_view(symbol.node) and self.collector.symbols.proven(symbol):
            return self._class_view_variable(symbol, used)
        return [("*", None, "unresolved-view")], None, view is not None

    def _class_view_variable(
        self, symbol: ValueSymbol, used: str | None
    ) -> tuple[list[tuple[str, str | None, str]], tuple[str, ...] | None, bool]:
        """한 번만 묶인 모듈 변수에 담은 `Cls.as_view(...)` 뷰의 method와 핸들러를 구한다(`v = Hook.as_view(…)`).

        그 변수(또는 등록에서 넘긴 이름)의 옵션 속성을 쓰는 모듈이 있으면 methods를 모른다.

        Args:
            symbol: 값이 `as_view` 호출인 모듈 변수(`SymbolTable.proven`을 지났다).
            used: 등록에서 넘긴 이름의 끝 이름.

        Returns:
            (method별 핸들러 틀, methods, 확정 불가 여부).
        """
        call = symbol.node
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        handlers, methods, uncertain = self._class_handlers(call.func.value, symbol.path)
        targets = self.collector.option_writes().targets
        if symbol.name in targets or used in targets:
            return handlers, (), True
        return handlers, methods, uncertain

    def _functions(self) -> dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
        """모듈 안 모든 함수 정의를 어휘적 점 경로별로 돌려준다(걷개마다 한 번 만든다).

        등록마다 모듈 구문 트리 전체를 걸으면 등록 수 × 노드 수다(앱 팩토리 하나에 등록이 수천 개면 수 초~수십 초).
        같은 점 경로의 재정의는 모두 담는다(옵션 속성 판정이 모두 본다).

        Returns:
            점 경로 → 정의 목록(모듈을 읽지 못하면 빈 사전).
        """
        if self._function_nodes is None:
            index = self.collector.symbols.index(self.path)
            found: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
            if index is not None:
                for node in ast.walk(index.module.tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found.setdefault(index.qualname(node), []).append(node)
            self._function_nodes = found
        return self._function_nodes

    def _class_handlers(
        self, owner_node: ast.expr, path: str | None = None
    ) -> tuple[list[tuple[str, str | None, str]], tuple[str, ...] | None, bool]:
        """`Cls.as_view(name)`의 method와 핸들러를 구한다.

        Args:
            owner_node: 클래스 식.
            path: 클래스 식이 있는 모듈 경로(없으면 이 모듈).

        Returns:
            (method별 핸들러 틀, methods, 확정 불가 여부).
        """
        symbol = (
            self.collector.symbols.resolve_expr(path or self.path, owner_node)
            if isinstance(owner_node, (ast.Name, ast.Attribute))
            else None
        )
        info = analyze_class(self.collector.symbols, symbol)
        if info is None or not isinstance(info.symbol, ProjectSymbol):
            return [("*", None, "unresolved-view")], (), True
        base_id = info.symbol.id
        node = info.symbol.node
        if isinstance(node, ast.ClassDef) and node.name in self.collector.option_writes().targets:
            # 클래스 본문 밖에서 `Hook.methods = …`로 바꾸면 `as_view`가 그 값을 읽는다(`view.methods = cls.methods`).
            dispatch = f"{base_id}.dispatch_request"
            return [("*", None, dispatch)], (), True
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
        rebound: bool,
    ) -> None:
        """규칙을 기록한다. method는 `methods` 인자 → 단축 장식자 → 뷰의 methods → GET 순서다(모르는 뷰는 GET 대신
        `ANY`, `_methods`).

        Args:
            owner: 앱·블루프린트.
            call: 장식자·`add_url_rule` 호출.
            shortcut: 단축 장식자 동사.
            handlers: 핸들러 틀(`*`는 모든 method, 빈 키는 핸들러 없는 method).
            view_methods: 뷰의 methods.
            uncertain: 확정 불가 여부.
            loops: 반복 변수 바인딩(리터럴 목록을 도는 반복의 규칙 문자열).
            rebound: 규칙을 단 이름이 다시 묶이는 모듈 이름이라 객체를 증명하지 못하는지(`_Rule.rebound`).
        """
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        if _uses_host_option(keywords, _HOST_KEYWORDS):
            self.collector.gaps.append(("route-coverage:", _HOST_OPTIONS))
        rule_node = call.args[0] if call.args else keywords.get("rule")
        if isinstance(rule_node, ast.Name) and loops.get(rule_node.id):
            rule_node = loops[rule_node.id][0]
        rule = self.collector.evaluator.string(self.path, rule_node)
        strict = self._strict(keywords)
        methods, uncertain = self._methods(keywords, shortcut, view_methods, uncertain)
        resolved = _bind_handlers(handlers, methods)
        location = node_location(self.path, rule_node if rule_node is not None else call, self.has_bom)
        owner.rules.append(_Rule(rule, methods, tuple(resolved), strict, location, uncertain, rebound))

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
        if view_methods == () or (view_methods is None and uncertain):
            # 뷰의 methods를 확정하지 못했다(빈 튜플 표식, 또는 `methods` 속성을 실을 수 있는 모르는 뷰 식·기반
            # 클래스). `add_url_rule`은 `view_func.methods`가 있으면 GET 대신 그것을 쓰므로 GET은 증명한 사실이 아니다.
            # ANY로 낸다.
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


def _is_as_view(node: ast.expr) -> bool:
    """식이 `X.as_view(...)` 호출인지 본다.

    Args:
        node: 식.

    Returns:
        맞으면 True.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "as_view"


def _foreign_name(dotted: str) -> bool:
    """외부 점 경로가 테스트 대역 모듈이거나 지원하지 않는 서버 프레임워크의 이름인지 본다.

    Args:
        dotted: 외부 점 경로.

    Returns:
        맞으면 True.
    """
    if any(dotted == module or dotted.startswith(module + ".") for module in _MOCK_MODULES):
        return True
    return unsupported_framework(dotted) is not None


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


def _blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    """블록 문장(`_BLOCK_STATEMENTS`)의 안쪽 문장 목록들을 돌려준다.

    Args:
        statement: 문장.

    Returns:
        블록 목록(본문, `else`, `finally`, 예외 처리기, `match` 사례 본문).
    """
    blocks: list[list[ast.stmt]] = [getattr(statement, name, []) for name in ("body", "orelse", "finalbody")]
    blocks.extend(handler.body for handler in getattr(statement, "handlers", []))
    blocks.extend(case.body for case in getattr(statement, "cases", []))
    return blocks


def _walrus_targets(statement: ast.stmt) -> list[ast.expr]:
    """문장 자신의 식(안쪽 블록·본문 제외)에 있는 바다코끼리 대입 대상이다.

    Args:
        statement: 문장.

    Returns:
        대상 이름 식 목록.
    """
    nested = [statement.body] if isinstance(statement, _DEFINITIONS) else _blocks(statement)
    skipped = {id(child) for block in nested for child in block}
    found: list[ast.expr] = []
    stack: list[ast.AST] = [statement]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.NamedExpr):
            found.append(node.target)
        stack.extend(child for child in ast.iter_child_nodes(node) if id(child) not in skipped)
    return found


def _is_self_attribute(node: ast.expr) -> bool:
    """`self.X`·`cls.X` 식인지 본다.

    Args:
        node: 식.

    Returns:
        맞으면 True.
    """
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")


def _parameter_names(arguments: ast.arguments) -> list[str]:
    """함수 매개변수 이름을 모두 돌려준다(위치 전용·키워드 전용·`*args`·`**kwargs` 포함).

    Args:
        arguments: 함수 인자 정의.

    Returns:
        이름 목록.
    """
    names = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    names.extend(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    return names


def _registration_uses(node: ast.AST, scope: str, names: frozenset[str]) -> Iterator[tuple[ast.Attribute, str]]:
    """부르거나 장식자로 쓰거나 별칭으로 묶는 등록 메서드 속성(`X.route(…)`, `@X.route`, `x = X.get`) 중 `X` 끝 이름이
    알려진 객체 이름인 것을 범위와 함께 찾는다. `if app.get:`·`print(app.route)`·`app.route = …` 같은 단순 참조는 등록이
    아니다.

    Args:
        node: 구문 노드.
        scope: 노드가 있는 함수 점 경로.
        names: 알려진 객체 이름.

    Returns:
        (속성 식, 범위) 반복자.
    """
    used = node.func if isinstance(node, ast.Call) else getattr(node, "value", None)
    if isinstance(node, (ast.Call, ast.Assign, ast.AnnAssign, ast.NamedExpr)) and _registration_attribute(used, names):
        assert isinstance(used, ast.Attribute)
        yield used, scope
    if isinstance(node, _DEFINITIONS):
        for decorator in node.decorator_list:
            if _registration_attribute(decorator, names):
                assert isinstance(decorator, ast.Attribute)
                yield decorator, scope
            yield from _registration_uses(decorator, scope, names)
        inner = f"{scope}.{node.name}" if scope else node.name
        for statement in node.body:
            yield from _registration_uses(statement, inner, names)
        return
    for child in ast.iter_child_nodes(node):
        yield from _registration_uses(child, scope, names)


def _registration_attribute(node: object, names: frozenset[str]) -> bool:
    """식이 알려진 객체 이름을 수신자로 둔 등록 메서드 속성(`app.route`)인지 본다.

    Args:
        node: 식(없을 수 있다).
        names: 알려진 객체 이름.

    Returns:
        맞으면 True.
    """
    return isinstance(node, ast.Attribute) and node.attr in _REGISTRATION_METHODS and _last_name(node.value) in names


def _last_name(node: ast.expr) -> str | None:
    """`a`·`a.b` 식의 끝 이름이다.

    Args:
        node: 식.

    Returns:
        끝 이름, 이름·속성 식이 아니면 None.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _block_targets(statement: ast.stmt) -> list[ast.expr]:
    """블록 문장이 이름을 묶는 대상(`with … as`, 예외 이름, `match` 캡처)을 이름 식으로 돌려준다.

    Args:
        statement: 문장.

    Returns:
        대상 식 목록.
    """
    targets = [item.optional_vars for item in getattr(statement, "items", []) if item.optional_vars is not None]
    names = [handler.name for handler in getattr(statement, "handlers", []) if handler.name]
    for case in getattr(statement, "cases", []):
        for node in ast.walk(case.pattern):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                names.append(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.append(node.rest)
    return targets + [ast.Name(id=name, ctx=ast.Store()) for name in names]
