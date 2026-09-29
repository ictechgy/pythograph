"""Django·DRF 뷰 참조를 (method, 핸들러 id) 목록으로 푼다.

규칙(설치한 Django 5.2.17·DRF 3.18.1 소스로 확인, `docs/HTTP-ROUTES.md`):

- 함수 뷰는 모든 method를 받는다(`ANY`). `require_http_methods`·`require_GET`·`require_POST`·
  `require_safe`(`django/views/decorators/http.py`)와 DRF `api_view`(`rest_framework/decorators.py`,
  인자 없으면 `["GET"]`)가 받는 method를 좁힌다. 여러 개가 겹치면 교집합이다. 그 밖의 장식자는 투명하다.
- 클래스 뷰(`X.as_view()`)는 `http_method_names`(기본 get·post·put·patch·delete·head·options·trace) 중
  클래스 사슬에 핸들러가 있는 이름이다(`View.dispatch`). `head`는 `get`이 있으면 자동이고(`View.setup`),
  `options`는 `View.options`가 항상 받으므로 둘 다 decl로 내지 않는다(isthmus `head-as-get`·`options-any`).
- 핸들러 id는 등록한 클래스 기준 `<파일>#<클래스>.<메서드>`다. 상속한 메서드도 같다(다음 단계 그래프가
  상속 멤버 정점으로 잇는다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.routes.classes import ClassInfo, analyze_class, attribute_literal
from pythograph.source.evaluate import Evaluator
from pythograph.source.symbols import (
    ExternalSymbol,
    MemberSymbol,
    ProjectSymbol,
    Symbol,
    SymbolTable,
    is_external,
)

#: Django `View.http_method_names`와 DRF `APIView.http_method_names`의 기본값이다.
DEFAULT_HTTP_METHOD_NAMES = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

#: 방법을 좁히는 Django 장식자다(외부 점 경로 → 허용 동사, None이면 인자에서 읽음).
_RESTRICTING_DECORATORS: dict[str, tuple[str, ...] | None] = {
    "django.views.decorators.http.require_http_methods": None,
    "django.views.decorators.http.require_GET": ("GET",),
    "django.views.decorators.http.require_POST": ("POST",),
    "django.views.decorators.http.require_safe": ("GET", "HEAD"),
    "rest_framework.decorators.api_view": None,
}


@dataclass(frozen=True)
class Handler:
    """method 하나의 핸들러.

    Attributes:
        method: 대문자 HTTP 동사 또는 `ANY`.
        usr: 핸들러의 pythograph id(프로젝트 밖이면 None).
        qualified_name: 사람이 읽을 이름.
    """

    method: str
    usr: str | None
    qualified_name: str


@dataclass(frozen=True)
class ViewResolution:
    """뷰 참조를 푼 결과.

    Attributes:
        handlers: method별 핸들러.
        uncertain: 받는 method 집합을 확정하지 못했는지(알 수 없는 기반 클래스·장식자 인자 등).
    """

    handlers: tuple[Handler, ...]
    uncertain: bool


class ViewResolver:
    """Django·DRF 뷰 참조 해석기."""

    def __init__(self, symbols: SymbolTable, evaluator: Evaluator) -> None:
        """해석기를 만든다.

        Args:
            symbols: 이름 해석기.
            evaluator: 상수 평가기.
        """
        self.symbols = symbols
        self.evaluator = evaluator

    def resolve(self, path: str, expr: ast.expr) -> ViewResolution:
        """URLconf의 뷰 인자를 푼다.

        Args:
            path: URLconf 모듈 경로.
            expr: `path()`의 두 번째 인자.

        Returns:
            해석 결과. 풀지 못하면 `ANY` 하나와 uncertain.
        """
        if isinstance(expr, ast.Call):
            return self._call_view(path, expr)
        symbol = self.symbols.resolve_expr(path, expr)
        if isinstance(symbol, ProjectSymbol) and isinstance(symbol.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self.function_view(symbol)
        return _unknown(_describe(symbol))

    def function_view(self, symbol: ProjectSymbol, restriction: tuple[str, ...] | None = None) -> ViewResolution:
        """함수 뷰의 method를 장식자로 정한다.

        Args:
            symbol: 함수 정의.
            restriction: 호출 감싸기(`require_POST(view)`)로 이미 좁힌 동사.

        Returns:
            해석 결과.
        """
        node = symbol.node
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        allowed = restriction
        uncertain = False
        for decorator in node.decorator_list:
            methods = self._decorator_methods(symbol.path, decorator)
            if isinstance(methods, tuple):
                allowed = methods if allowed is None else tuple(item for item in allowed if item in methods)
            elif methods is not None:
                uncertain = True
        return ViewResolution(_function_handlers(symbol, allowed), uncertain)

    def _decorator_methods(self, path: str, decorator: ast.expr) -> tuple[str, ...] | None | object:
        """장식자 하나가 좁히는 동사를 구한다.

        Args:
            path: 모듈 경로.
            decorator: 장식자 식.

        Returns:
            동사 튜플, 좁히지 않으면 None, 좁히지만 인자를 읽지 못하면 `...`.
        """
        call = decorator if isinstance(decorator, ast.Call) else None
        resolved = self.symbols.resolve_expr(path, call.func if call else decorator)
        dotted = resolved.dotted if isinstance(resolved, ExternalSymbol) else None
        if dotted not in _RESTRICTING_DECORATORS:
            return None
        fixed = _RESTRICTING_DECORATORS[dotted]
        if fixed is not None:
            return fixed if call is None else ...
        if call is None:
            return ...
        if dotted == "rest_framework.decorators.api_view" and not call.args and not call.keywords:
            return ("GET",)
        argument = call.args[0] if call.args else (call.keywords[0].value if call.keywords else None)
        methods = self.evaluator.string_list(path, argument)
        return ... if methods is None else tuple(method.upper() for method in methods)

    def _call_view(self, path: str, call: ast.Call) -> ViewResolution:
        """호출 식 뷰(`X.as_view()`, `csrf_exempt(view)`)를 푼다.

        Args:
            path: 모듈 경로.
            call: 호출 식.

        Returns:
            해석 결과.
        """
        if isinstance(call.func, ast.Attribute) and call.func.attr == "as_view":
            owner = self.symbols.resolve_expr(path, call.func.value)
            return self.class_view(owner, path, call)
        if len(call.args) == 1 and not call.keywords:
            inner = self.resolve(path, call.args[0])
            restriction = self._decorator_methods(path, call.func)
            return _restrict(inner, restriction)
        return _unknown("an unrecognized call")

    def class_view(self, owner: Symbol | None, path: str, call: ast.Call) -> ViewResolution:
        """클래스 뷰(`X.as_view(...)`)의 method와 핸들러를 정한다.

        Args:
            owner: 클래스 해석 결과.
            path: URLconf 모듈 경로(`as_view` 인자 평가용).
            call: `as_view` 호출.

        Returns:
            해석 결과.
        """
        info = analyze_class(self.symbols, owner)
        if info is None:
            return _unknown(_describe(owner))
        if "drf-viewset" in info.kinds:
            return self._manual_viewset(info, path, call)
        names = self._method_names(info, path, call)
        if names is None:
            return _unknown("http_method_names is not a literal list")
        handlers = [name for name in names if name in info.names and name != "options"]
        if "head" in handlers and "get" in handlers:
            handlers.remove("head")
        resolved = tuple(_class_handler(info, name, name.upper()) for name in handlers)
        if not resolved:
            return _unknown("the class defines no handler")
        return ViewResolution(resolved, info.unknown_bases)

    def _method_names(self, info: ClassInfo, path: str, call: ast.Call) -> list[str] | None:
        """`http_method_names`를 정한다. `as_view(http_method_names=…)`가 클래스 속성보다 앞선다.

        Args:
            info: 클래스 분석 결과.
            path: URLconf 모듈 경로.
            call: `as_view` 호출.

        Returns:
            소문자 동사 목록, 읽지 못하면 None.
        """
        for keyword in call.keywords:
            if keyword.arg == "http_method_names":
                return self.evaluator.string_list(path, keyword.value)
        value = attribute_literal(info, "http_method_names")
        if value is None:
            return list(DEFAULT_HTTP_METHOD_NAMES)
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return [str(item) for item in value]
        return None

    def _manual_viewset(self, info: ClassInfo, path: str, call: ast.Call) -> ViewResolution:
        """`ViewSet.as_view({"get": "list"})`처럼 직접 연결한 viewset을 푼다.

        Args:
            info: viewset 분석 결과.
            path: URLconf 모듈 경로.
            call: `as_view` 호출.

        Returns:
            해석 결과.
        """
        mapping_node = (
            call.args[0]
            if call.args
            else next((keyword.value for keyword in call.keywords if keyword.arg == "actions"), None)
        )
        if not isinstance(mapping_node, ast.Dict):
            return _unknown("the viewset action mapping is not a literal dict")
        handlers: list[Handler] = []
        for key_node, value_node in zip(mapping_node.keys, mapping_node.values, strict=False):
            method = self.evaluator.string(path, key_node)
            action = self.evaluator.string(path, value_node)
            if method is None or action is None:
                return _unknown("the viewset action mapping is not literal")
            if method.lower() != "head" or "get" not in [h.method.lower() for h in handlers]:
                handlers.append(_class_handler(info, action, method.upper()))
        return ViewResolution(tuple(handlers), info.unknown_bases)


def viewset_handler(info: ClassInfo, action: str, method: str) -> Handler:
    """라우터가 연결한 viewset action의 핸들러를 만든다.

    Args:
        info: viewset 분석 결과.
        action: action(메서드) 이름.
        method: 대문자 HTTP 동사.

    Returns:
        핸들러.
    """
    return _class_handler(info, action, method)


def _class_handler(info: ClassInfo, member: str, method: str) -> Handler:
    """클래스 멤버 핸들러를 만든다. 프로젝트 클래스면 등록한 클래스 기준 id를 싣는다.

    Args:
        info: 클래스 분석 결과.
        member: 메서드 이름.
        method: 대문자 HTTP 동사.

    Returns:
        핸들러.
    """
    symbol = info.symbol
    if isinstance(symbol, ProjectSymbol):
        usr = f"{symbol.id}.{member}"
        return Handler(method, usr, usr)
    return Handler(method, None, f"{symbol.dotted}.{member}")


def _function_handlers(symbol: ProjectSymbol, allowed: tuple[str, ...] | None) -> tuple[Handler, ...]:
    """함수 뷰의 핸들러 목록을 만든다. HEAD는 GET이 있으면 뺀다.

    Args:
        symbol: 함수 정의.
        allowed: 허용 동사(None이면 모두).

    Returns:
        핸들러 목록.
    """
    if allowed is None:
        return (Handler("ANY", symbol.id, symbol.id),)
    return tuple(Handler(method, symbol.id, symbol.id) for method in emitted_methods(allowed))


def emitted_methods(methods: tuple[str, ...] | list[str]) -> list[str]:
    """허용 동사에서 decl로 낼 동사를 고른다.

    HEAD는 GET이 있으면 프레임워크가 GET 핸들러로 처리하고 소비자의 `head-as-get`이 맞추므로 뺀다.
    명시한 OPTIONS는 그대로 둔다(자동 OPTIONS는 애초에 목록에 없다). 순서를 지키고 중복을 없앤다.

    Args:
        methods: 대문자 동사 목록.

    Returns:
        낼 동사 목록.
    """
    unique = list(dict.fromkeys(method.upper() for method in methods))
    if "GET" in unique and "HEAD" in unique:
        unique.remove("HEAD")
    return unique


def _restrict(inner: ViewResolution, restriction: object) -> ViewResolution:
    """호출로 감싼 뷰에 감싸기 함수의 동사 제한을 적용한다.

    Args:
        inner: 감싼 뷰의 해석 결과.
        restriction: 감싸기 함수가 좁히는 동사, None, 또는 `...`(읽지 못함).

    Returns:
        제한을 적용한 결과.
    """
    if restriction is None:
        return inner
    if not isinstance(restriction, tuple):
        return ViewResolution(inner.handlers, True)
    handlers: list[Handler] = []
    for handler in inner.handlers:
        if handler.method == "ANY":
            handlers.extend(
                Handler(method, handler.usr, handler.qualified_name) for method in emitted_methods(restriction)
            )
        elif handler.method in restriction:
            handlers.append(handler)
    return ViewResolution(tuple(handlers), inner.uncertain)


def _unknown(reason: str) -> ViewResolution:
    """풀지 못한 뷰를 `ANY` 하나로 낸다(거짓 error를 만들지 않는 쪽).

    Args:
        reason: 이유(사람이 읽는 설명, 현재는 쓰지 않는다).

    Returns:
        uncertain 결과.
    """
    del reason
    return ViewResolution((Handler("ANY", None, "unresolved-view"),), True)


def _describe(symbol: Symbol | None) -> str:
    """해석 결과를 이유 문구로 바꾼다.

    Args:
        symbol: 해석 결과.

    Returns:
        설명.
    """
    if isinstance(symbol, ExternalSymbol):
        return "a view outside the project"
    if isinstance(symbol, MemberSymbol):
        return "a class attribute"
    return "an unresolved view"


def is_django_path_function(symbol: Symbol | None) -> str | None:
    """`path`·`re_path`(+ 옛 `url`) 함수면 패턴 종류를 돌려준다.

    Args:
        symbol: 호출 대상 해석 결과.

    Returns:
        `route`·`regex` 또는 None.
    """
    if is_external(symbol, "django.urls.path", "django.urls.conf.path"):
        return "route"
    if is_external(symbol, "django.urls.re_path", "django.urls.conf.re_path", "django.conf.urls.url"):
        return "regex"
    return None
