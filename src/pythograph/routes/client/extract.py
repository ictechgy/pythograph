"""`routes --role client`: 프로젝트의 HTTP 요청 호출을 route-call로 추출한다.

순서: 선언 색인(그래프와 같은 id) → 정의마다 자기 범위의 호출식 → (1) 선언된 래퍼 호출, (2) 알려진 라이브러리의
요청 함수·클라이언트 메서드, (3) 모델링하지 않은 요청 API·타입 모르는 수신자의 URL 리터럴 호출(한계로 센다).
`symbol.usr`는 호출을 감싸는 선언(함수·메서드·클래스 본문·모듈)의 pythograph id라 `pythograph graph`·`reach`·
`impact`의 정점과 같다.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue, ExternalValue, FunctionValue, MethodValue
from pythograph.routes.client.compose import (
    DYNAMIC_URL,
    UNKNOWN_BASE,
    ComposedUrl,
    Joined,
    Literal,
    Part,
    PathResult,
    Value,
    compose_path,
    finish,
    join_path,
    merge_literals,
    split_url,
    with_prefix,
)
from pythograph.routes.client.libraries import (
    CLIENT_CLASSES,
    CLIENT_METHODS,
    MODULE_FUNCTIONS,
    REMOVES_DOTS,
    UNMODELED_CLIENT_METHODS,
    UPPERCASES_METHOD,
    VERB_NAMES,
    ClientKind,
    ClientTracker,
    is_unmodeled_call,
    module_client,
)
from pythograph.routes.client.model import ClientExtraction, RouteCall
from pythograph.routes.client.parts import PartBuilder, collect_attribute_writes, own_nodes
from pythograph.routes.client.wrappers import (
    CallArgument,
    MethodToken,
    WrapperDecl,
    bind_method,
    find_argument,
)
from pythograph.routes.django.urlconf import node_location
from pythograph.routes.versions import declared_minimum
from pythograph.source.project import Project, ReadFailure, is_test_path
from pythograph.source.symbols import SymbolTable

#: 호출 측 커버리지 접두사다.
COVERAGE = "route-call-coverage:"


@dataclass(frozen=True)
class ClientOptions:
    """클라이언트 추출 옵션.

    Attributes:
        include_tests: 테스트 소스도 스캔할지.
        wrappers: `language: "python"` 래퍼 선언.
    """

    include_tests: bool
    wrappers: tuple[WrapperDecl, ...] = ()


@dataclass
class _Counters:
    """한계 문구용 계수."""

    unmodeled: Counter[str] = field(default_factory=Counter)
    untyped_receivers: int = 0
    ambiguous: int = 0
    sinks: set[str] = field(default_factory=set)
    wrapper_calls: Counter[int] = field(default_factory=Counter)
    requests_built: dict[int, bool] = field(default_factory=dict)


def extract_client_calls(project: Project, options: ClientOptions) -> ClientExtraction:
    """프로젝트의 HTTP 요청 호출을 추출한다.

    Args:
        project: 분석 대상 프로젝트.
        options: 옵션.

    Returns:
        추출 결과.
    """
    symbols = SymbolTable(project)
    index = DefinitionIndex(project, symbols, options.include_tests)
    resolver = Resolver(index, symbols)
    writes = collect_attribute_writes(resolver)
    builder = PartBuilder(resolver, writes)
    rewritten = "base_url" in writes.untyped or any(name == "base_url" for _, name in writes.typed)
    tracker = ClientTracker(resolver, builder, rewritten, declared_minimum(project, "aiohttp"))
    scanner = _Scanner(resolver, builder, tracker, options.wrappers)
    for definition in sorted(index.definitions.values(), key=lambda item: item.id):
        scanner.scan(definition)
    scanner.report(index)
    _project_gaps(project, scanner.extraction)
    return scanner.extraction


class _Scanner:
    """정의마다 호출식을 판정해 사실과 계수를 모은다."""

    def __init__(
        self, resolver: Resolver, builder: PartBuilder, tracker: ClientTracker, wrappers: tuple[WrapperDecl, ...]
    ) -> None:
        """스캐너를 만든다.

        Args:
            resolver: 정적 값 해석기.
            builder: URL 조각 도우미.
            tracker: 클라이언트 객체 추적기.
            wrappers: 파이썬 래퍼 선언.
        """
        self.resolver = resolver
        self.builder = builder
        self.tracker = tracker
        self.wrappers = wrappers
        self.wrappers_by_id: dict[str, list[WrapperDecl]] = {}
        for wrapper in wrappers:
            self.wrappers_by_id.setdefault(wrapper.target_id, []).append(wrapper)
        self.extraction = ClientExtraction()
        self.counters = _Counters()
        self.scope: Definition | None = None

    def scan(self, definition: Definition) -> None:
        """정의 하나의 자기 범위 호출식을 판정한다.

        Args:
            definition: 정의(모듈·클래스·함수).
        """
        self.scope = definition
        for node in own_nodes(definition):
            if isinstance(node, ast.Call):
                self._call(definition, node)

    def _call(self, scope: Definition, call: ast.Call) -> None:
        """호출식 하나를 판정한다.

        Args:
            scope: 범위.
            call: 호출식.
        """
        if self._wrapper_call(scope, call):
            return
        callee = self.resolver.value(scope, call.func)
        if isinstance(callee, ExternalValue):
            self._external_call(scope, call, callee.dotted)
        elif isinstance(call.func, ast.Attribute) and not isinstance(callee, (FunctionValue, MethodValue)):
            self._method_call(scope, call, call.func)

    def _external_call(self, scope: Definition, call: ast.Call, dotted: str) -> None:
        """외부 이름으로 푼 호출을 판정한다.

        Args:
            scope: 범위.
            call: 호출식.
            dotted: 외부 점 경로.
        """
        if dotted in MODULE_FUNCTIONS:
            library, verb = MODULE_FUNCTIONS[dotted]
            self._request(scope, call, module_client(library), verb)
        elif dotted == "urllib.request.urlopen":
            self._urlopen(scope, call)
        elif dotted == "urllib.request.Request":
            self.counters.requests_built.setdefault(id(call), False)
        elif self._inherited_client_method(scope, call, dotted):
            return
        elif is_unmodeled_call(dotted):
            self.counters.unmodeled[dotted.split(".")[0]] += 1

    def _inherited_client_method(self, scope: Definition, call: ast.Call, dotted: str) -> bool:
        """프로젝트 하위 클래스가 물려받은 클라이언트 메서드 호출(`class Api(httpx.Client)`)을 판정한다.

        base는 하위 클래스의 `__init__`이 정할 수 있어 모른다고 본다.

        Args:
            scope: 범위.
            call: 호출식.
            dotted: `<클라이언트 클래스>.<메서드>` 모양의 외부 점 경로.

        Returns:
            클라이언트 메서드였으면 True.
        """
        owner, _, name = dotted.rpartition(".")
        library = CLIENT_CLASSES.get(owner)
        if library is None:
            return False
        if name in UNMODELED_CLIENT_METHODS:
            self.counters.unmodeled[library] += 1
        elif name in CLIENT_METHODS and (name != "stream" or library == "httpx"):
            kind = self.tracker.kind(library, None if library == "requests" else UNKNOWN_BASE)
            self._request(scope, call, kind, CLIENT_METHODS[name])
        return True

    def _method_call(self, scope: Definition, call: ast.Call, func: ast.Attribute) -> None:
        """수신자 객체의 메서드 호출을 판정한다.

        Args:
            scope: 범위.
            call: 호출식.
            func: 피호출 속성 식.
        """
        name = func.attr
        if name not in CLIENT_METHODS and name not in UNMODELED_CLIENT_METHODS:
            return
        kind = self.tracker.client_of(scope, func.value)
        if kind is None:
            if (
                name in CLIENT_METHODS
                and not self._unmodeled_receiver(scope, func.value)
                and self._looks_like_url_call(scope, call, CLIENT_METHODS[name])
            ):
                self.counters.untyped_receivers += 1
            return
        if name in UNMODELED_CLIENT_METHODS:
            self.counters.unmodeled[kind.library] += 1
        elif name != "stream" or kind.library == "httpx":
            self._request(scope, call, kind, CLIENT_METHODS[name])

    def _unmodeled_receiver(self, scope: Definition, expr: ast.expr) -> bool:
        """수신자가 모델링하지 않는 요청 API가 만든 객체(`urllib3.PoolManager()`)인지 본다(이중 계수 방지).

        Args:
            scope: 범위.
            expr: 수신자 식.

        Returns:
            그런 객체면 True.
        """
        call = self._single_call(scope, expr)
        if call is None:
            return False
        callee = self.resolver.value(scope, call.func)
        return isinstance(callee, ExternalValue) and is_unmodeled_call(callee.dotted)

    def _looks_like_url_call(self, scope: Definition, call: ast.Call, verb: str | None) -> bool:
        """타입 모르는 수신자의 호출이 URL 리터럴을 넘기는지 본다(한계 계수용).

        Args:
            scope: 범위.
            call: 호출식.
            verb: 고정 동사(None이면 URL은 두 번째 인자).

        Returns:
            첫 조각이 `/`·`http://`·`https://`로 시작하는 리터럴이면 True.
        """
        expr = _argument(call, 0 if verb else 1, "url")
        if expr is None:
            return False
        parts = self.builder.parts(scope, expr)
        head = parts[0] if parts else None
        return isinstance(head, Literal) and head.text.startswith(("/", "http://", "https://"))

    def _request(self, scope: Definition, call: ast.Call, kind: ClientKind, verb: str | None) -> None:
        """라이브러리 요청 호출 하나를 사실로 만든다.

        Args:
            scope: 범위.
            call: 호출식.
            kind: 클라이언트 판정.
            verb: 고정 동사(None이면 첫 인자가 동사).
        """
        url_expr = _argument(call, 0 if verb else 1, "url")
        method = verb if verb is not None else self._method_value(scope, _argument(call, 0, "method"), kind.library)
        parts = self.builder.parts(scope, url_expr) if url_expr is not None else (Value(),)
        url = self._url(scope, url_expr, parts, kind) if url_expr is not None else DYNAMIC_URL
        self._emit(scope, call, method, url, parts)

    def _method_value(self, scope: Definition, expr: ast.expr | None, library: str) -> str | None:
        """동사 인자 값을 정한다. 라이브러리가 대문자로 바꾸면 바꾼 값이 계약 동사일 때만 동사다.

        Args:
            scope: 범위.
            expr: 동사 인자 식.
            library: 라이브러리 이름.

        Returns:
            동사 또는 None.
        """
        text = self.builder.string(scope, expr) if expr is not None else None
        if text is None:
            return None
        verb = text.upper() if UPPERCASES_METHOD[library] else text
        return verb if verb in _VERBS else None

    def _url(self, scope: Definition, expr: ast.expr, parts: tuple[Part, ...], kind: ClientKind) -> ComposedUrl:
        """URL 식을 사실 필드로 만든다. `urllib.parse.urljoin(base, path)`는 결합 결과를 주장하지 않는다(`urljoin_url`).

        Args:
            scope: 범위.
            expr: URL 식.
            parts: URL 식의 조각.
            kind: 클라이언트 판정.

        Returns:
            경로 필드.
        """
        joined = self._urljoin(scope, expr)
        if joined is not None:
            return joined
        return url_from_parts(kind, parts)

    def _urljoin(self, scope: Definition, expr: ast.expr) -> ComposedUrl | None:
        """`urljoin(base, path)` 식(또는 한 번 묶인 지역 이름)이면 경로 인자만으로 사실 필드를 만든다.

        Args:
            scope: 범위.
            expr: URL 식.

        Returns:
            경로 필드, urljoin이 아니면 None.
        """
        call = self._single_call(scope, expr)
        if call is None or len(call.args) != 2 or call.keywords:
            return None
        callee = self.resolver.value(scope, call.func)
        if not (isinstance(callee, ExternalValue) and callee.dotted in ("urllib.parse.urljoin", "urlparse.urljoin")):
            return None
        return urljoin_url(self.builder.parts(scope, call.args[1]))

    def _single_call(self, scope: Definition, expr: ast.expr) -> ast.Call | None:
        """식이 호출식이거나 호출식에 한 번 묶인 지역 이름이면 그 호출식을 돌려준다.

        Args:
            scope: 범위.
            expr: 식.

        Returns:
            호출식 또는 None.
        """
        if isinstance(expr, ast.Call):
            return expr
        if isinstance(expr, ast.Name):
            binding = self.builder.local_binding(scope, expr.id)
            if binding is not None and binding.kind == "assign" and isinstance(binding.value, ast.Call):
                return binding.value
        return None

    def _urlopen(self, scope: Definition, call: ast.Call) -> None:
        """`urllib.request.urlopen(url 또는 Request, data)`를 사실로 만든다.

        동사: `Request(method=…)`가 리터럴이면 그 값(urllib는 대문자로 바꾸지 않는다), 없으면 보낼 데이터(`urlopen`의
        `data`가 None이 아니면 그것, 아니면 `Request`의 `data`)가 없거나 None 리터럴이면 GET, None이 아닌 리터럴이면
        POST, 모르면 methodDynamic(`Request.get_method`, `OpenerDirector.open`).

        Args:
            scope: 범위.
            call: 호출식.
        """
        target = _argument(call, 0, "url")
        data = _argument(call, 1, "data")
        request = self._request_object(scope, target)
        kind = module_client("urllib")
        if request is None:
            parts = self.builder.parts(scope, target) if target is not None else (Value(),)
            opaque = target is None or (parts == (Value(),) and self._urljoin(scope, target) is None)
            method = None if opaque else _method_from_data(data)
            url = self._url(scope, target, parts, kind) if target is not None else DYNAMIC_URL
            self._emit(scope, call, method, url, parts)
            return
        self.counters.requests_built[id(request)] = True
        url_expr = _argument(request, 0, "url")
        parts = self.builder.parts(scope, url_expr) if url_expr is not None else (Value(),)
        url = self._url(scope, url_expr, parts, kind) if url_expr is not None else DYNAMIC_URL
        self._emit(scope, call, self._urllib_method(scope, request, data), url, parts)

    def _request_object(self, scope: Definition, target: ast.expr | None) -> ast.Call | None:
        """`urlopen` 인자가 `urllib.request.Request(...)`(또는 그 호출에 한 번 묶인 이름)면 그 호출식을 돌려준다.

        Args:
            scope: 범위.
            target: `urlopen` 첫 인자.

        Returns:
            Request 생성 호출식 또는 None.
        """
        call = self._single_call(scope, target) if target is not None else None
        if call is None:
            return None
        callee = self.resolver.value(scope, call.func)
        return call if isinstance(callee, ExternalValue) and callee.dotted == "urllib.request.Request" else None

    def _urllib_method(self, scope: Definition, request: ast.Call, data: ast.expr | None) -> str | None:
        """Request 객체 요청의 동사를 정한다.

        Args:
            scope: 범위.
            request: Request 생성 호출식.
            data: `urlopen`의 `data` 인자.

        Returns:
            동사 또는 None.
        """
        method = _argument(request, 5, "method")
        if method is not None and not (isinstance(method, ast.Constant) and method.value is None):
            return self._method_value(scope, method, "urllib")
        if data is not None and not (isinstance(data, ast.Constant) and data.value is None):
            return _method_from_data(data)
        return _method_from_data(_argument(request, 1, "data"))

    def _wrapper_call(self, scope: Definition, call: ast.Call) -> bool:
        """선언된 래퍼 호출이면 사실을 만든다.

        Args:
            scope: 범위.
            call: 호출식.

        Returns:
            래퍼 호출이었으면 True.
        """
        if not self.wrappers:
            return False
        declaration = self._wrapper_for(scope, call)
        if declaration is None:
            return False
        self.counters.wrapper_calls[declaration.position] += 1
        arguments = _call_arguments(call)
        path = find_argument(declaration.path_arg, arguments)
        parts: tuple[Part, ...] = (Value(),)
        url = DYNAMIC_URL
        if isinstance(path, CallArgument) and isinstance(path.value, ast.expr):
            parts = self.builder.parts(scope, path.value)
            url = wrapper_url(declaration.path_anchor, parts)
        method = bind_method(declaration, arguments, lambda value: self._method_token(scope, declaration, value))
        self._emit(scope, call, method, url, parts, declaration.service)
        return True

    def _wrapper_for(self, scope: Definition, call: ast.Call) -> WrapperDecl | None:
        """호출이 가리키는 래퍼 선언을 찾는다.

        Args:
            scope: 범위.
            call: 호출식.

        Returns:
            선언 또는 None.
        """
        callee = self.resolver.value(scope, call.func)
        candidates: list[str] = []
        if isinstance(callee, FunctionValue):
            candidates.append(callee.definition.id)
        elif isinstance(callee, MethodValue):
            candidates.append(callee.member.id)
        elif isinstance(callee, ClassValue):
            candidates.append(f"{callee.definition.id}.__init__")
            member = self.resolver.linearizer.lookup(callee.definition, "__init__")
            if member.kind == "method" and member.definition is not None:
                candidates.append(member.definition.id)
        for candidate in candidates:
            for declaration in self.wrappers_by_id.get(candidate, []):
                if declaration.kind == ("constructor" if isinstance(callee, ClassValue) else "function"):
                    return declaration
        return None

    def _method_token(self, scope: Definition, declaration: WrapperDecl, value: object) -> MethodToken:
        """래퍼 동사 인자 값을 토큰으로 바꾼다.

        이름·속성(`HttpMethod.GET`, `GET`)은 마지막 이름이 `methodEnum`에 있으면 enum case다. 없으면 상수 문자열로
        확정될 때 리터럴이다. 문자열 리터럴은 리터럴이다.

        Args:
            scope: 범위.
            declaration: 래퍼 선언.
            value: 인자 식.

        Returns:
            토큰.
        """
        if not isinstance(value, ast.expr):
            return MethodToken("value")
        if isinstance(value, ast.Constant):
            return MethodToken("literal", value.value) if isinstance(value.value, str) else MethodToken("value")
        name = value.attr if isinstance(value, ast.Attribute) else value.id if isinstance(value, ast.Name) else None
        if name is not None and name in declaration.method_enum:
            return MethodToken("enum", name)
        text = self.builder.string(scope, value)
        return MethodToken("literal", text) if text is not None else MethodToken("value")

    def _emit(
        self,
        scope: Definition,
        call: ast.Call,
        method: str | None,
        url: ComposedUrl,
        parts: tuple[Part, ...],
        service: str | None = None,
    ) -> None:
        """사실 하나를 더한다. 선언된 래퍼 본문의 dynamic 호출은 래퍼 호출 사실이 대신하므로 내지 않는다.

        Args:
            scope: 범위.
            call: 호출식.
            method: 동사(None이면 methodDynamic).
            url: 경로 필드.
            parts: URL 조각(래퍼 싱크 판정용).
            service: 래퍼 선언의 service.
        """
        if url.dynamic and self._inside_wrapper(scope):
            return
        if url.dynamic and service is None and passes_parameter(parts):
            self.counters.sinks.add(scope.id)
        if url.ambiguous:
            self.counters.ambiguous += 1
        module = scope.module.module
        self.extraction.calls.append(
            RouteCall(
                method=method,
                url=url,
                location=node_location(scope.path, call, module.has_bom),
                usr=scope.id,
                service=service,
                test_source=is_test_path(scope.path),
            )
        )

    def _inside_wrapper(self, scope: Definition) -> bool:
        """범위가 선언된 래퍼 본문(또는 그 안의 중첩 정의)인지 본다.

        Args:
            scope: 범위.

        Returns:
            래퍼 본문이면 True.
        """
        return any(scope.id == target or scope.id.startswith(target + ".") for target in self.wrappers_by_id)

    def report(self, index: DefinitionIndex) -> None:
        """계수를 한계 문구로 바꾼다.

        Args:
            index: 선언 색인(래퍼 대상 존재 확인용).
        """
        gaps = self.extraction
        for family, count in sorted(self.counters.unmodeled.items()):
            gaps.add_gap(COVERAGE, f"{count} calls use {family} request APIs that pythograph does not model")
        unsent = sum(1 for consumed in self.counters.requests_built.values() if not consumed)
        if unsent:
            gaps.add_gap(COVERAGE, f"{unsent} urllib.request.Request objects are not passed directly to urlopen")
        if self.counters.untyped_receivers:
            gaps.add_gap(
                COVERAGE,
                f"{self.counters.untyped_receivers} get/post/request-style calls pass a URL literal to a receiver "
                "whose HTTP client type could not be determined",
            )
        if self.counters.ambiguous:
            gaps.add_gap(
                "ambiguous-base-join:",
                f"{self.counters.ambiguous} calls join a path to a base URL in a way whose result cannot be proven "
                "(unknown base, '..', '//', urljoin, or an unproven library version)",
            )
        if self.counters.sinks:
            gaps.add_gap(
                "http-wrapper-undeclared:",
                f"{len(self.counters.sinks)} functions pass a parameter into an HTTP request URL; declare them in an "
                "http-wrappers file to resolve their callers",
            )
        self._wrapper_gaps(index)

    def _wrapper_gaps(self, index: DefinitionIndex) -> None:
        """대상이 없거나 호출이 0건인 래퍼 선언을 한계로 낸다.

        Args:
            index: 선언 색인.
        """
        for declaration in self.wrappers:
            where = f"wrappers[{declaration.position}]"
            if not _wrapper_exists(index, declaration):
                self.extraction.add_gap(
                    "http-wrapper-unresolved:", f"{where} names no Python definition in the scanned sources"
                )
            elif not self.counters.wrapper_calls[declaration.position]:
                self.extraction.add_gap("http-wrapper-unresolved:", f"{where} matched no calls")


#: 계약 동사다.
_VERBS = frozenset(VERB_NAMES.values()) | {"TRACE"}


def url_from_parts(kind: ClientKind, parts: tuple[Part, ...], remove_dots: bool | None = None) -> ComposedUrl:
    """URL 조각을 클라이언트 결합 규칙으로 사실 필드로 만든다.

    Args:
        kind: 클라이언트 판정.
        parts: URL 조각.
        remove_dots: 점 세그먼트를 지우는지(None이면 라이브러리 기본).

    Returns:
        경로 필드.
    """
    dots = REMOVES_DOTS[kind.library] if remove_dots is None else remove_dots
    split = split_url(parts)
    if split.kind in ("absolute", "dynamic-host"):
        if not kind.absolute_allowed():
            return DYNAMIC_URL
        anchor = "root" if split.kind == "absolute" else "base"
        authority = split.authority if split.kind == "absolute" else None
        return _compose(compose_path(split.path), lambda raw: Joined(raw, anchor, authority, remove_dots=dots))
    if split.kind == "base-value":
        return _compose(compose_path(split.path), _after_value)
    if split.kind == "relative" and kind.base is not None:
        base = kind.base
        return _compose(compose_path(split.path), lambda raw: join_path(kind.style, base, raw, kind.version))
    return DYNAMIC_URL


def urljoin_url(parts: tuple[Part, ...]) -> ComposedUrl:
    """`urllib.parse.urljoin(base, path)`의 경로 인자로 사실 필드를 만든다.

    계약(HTTP-WRAPPERS "그 밖")은 벡터가 없는 결합의 결과를 주장하지 않는다: 절대 URL 인자는 그대로 요청 URL이라
    `compose.strip`, `/`로 시작하는 리터럴은 base 앵커 꼬리(urljoin이 점 세그먼트를 지운 경로), 그 밖은 dynamic +
    `ambiguous-base-join:`이다. (모의 서버 오라클은 CPython `urljoin`이 RFC 3986처럼 동작함을 확인했지만 벡터가 생길
    때까지 root로 올리지 않는다.)

    Args:
        parts: urljoin 두 번째 인자의 조각.

    Returns:
        경로 필드.
    """
    split = split_url(parts)
    if split.kind in ("absolute", "dynamic-host"):
        return url_from_parts(module_client("urllib"), parts)
    if split.kind == "relative" and isinstance(split.path[0], Literal) and split.path[0].text.startswith("/"):
        return _compose(compose_path(split.path), lambda raw: Joined(raw, "base", remove_dots=True))
    return ComposedUrl(None, "base", ambiguous=True)


def wrapper_url(anchor: str, parts: tuple[Part, ...]) -> ComposedUrl:
    """래퍼 경로 인자를 선언의 `pathAnchor`로 사실 필드로 만든다.

    전체 URL 리터럴은 host 뒤 경로를 root로 쓴다. `/`로 시작하는 경로는 선언 앵커, 앞 값 뒤 `/` 경로는 base다.
    `/` 없이 시작하는 상대 경로는 래퍼 내부 결합을 몰라 dynamic과 `ambiguous-base-join:`이다.

    Args:
        anchor: 선언의 `pathAnchor`.
        parts: 경로 인자 조각.

    Returns:
        경로 필드.
    """
    split = split_url(parts)
    if split.kind in ("absolute", "dynamic-host"):
        root = split.kind == "absolute"
        authority = split.authority if root else None
        return _compose(compose_path(split.path), lambda raw: Joined(raw, "root" if root else "base", authority))
    if split.kind == "base-value":
        return _compose(compose_path(split.path), _after_value)
    if split.kind == "relative":
        return _compose(
            compose_path(split.path),
            lambda raw: Joined(raw, anchor) if raw.startswith("/") else Joined(None, "base", ambiguous=True),
        )
    return DYNAMIC_URL


def passes_parameter(parts: tuple[Part, ...]) -> bool:
    """URL이 감싼 함수의 매개변수를 URL 전체·앞머리(base)나 base 뒤 경로로 그대로 쓰는지 본다(래퍼 싱크).

    매개변수 조각이 첫 조각이거나, `/`로 끝나지 않는 리터럴 바로 뒤의 마지막 조각(`BASE + path`)이면 싱크다.
    세그먼트 하나를 채우는 매개변수(`/files/{name}.json`)는 경로를 흘려보내는 것이 아니라 싱크가 아니다.

    Args:
        parts: URL 조각.

    Returns:
        싱크면 True.
    """
    merged = merge_literals(parts)
    for index, part in enumerate(merged):
        if not (isinstance(part, Value) and part.parameter):
            continue
        if index == 0:
            return True
        previous = merged[index - 1]
        if index == len(merged) - 1 and isinstance(previous, Literal) and not previous.text.endswith("/"):
            return True
    return False


def _after_value(raw: str) -> Joined:
    """앞 값(모르는 base 식) 뒤 경로를 잇는다: `/`로 시작하면 base, 아니면 모호한 문자열 연결이다.

    점 세그먼트는 모르는 base 경로로 올라갈 수 있어 dynamic이다.

    Args:
        raw: 앞 값 뒤 원문 경로.

    Returns:
        결합 결과.
    """
    if not raw.startswith("/"):
        return Joined(None, "base", ambiguous=True)
    if any(segment in (".", "..") for segment in raw.split("/")):
        return Joined(None, "base")
    return Joined(raw, "base")


def _compose(path: PathResult, joiner: Callable[[str], Joined]) -> ComposedUrl:
    """조립한 경로를 결합·정규화한다. dynamic이면 접두사만 같은 규칙으로 잇는다.

    Args:
        path: 조립한 원문 경로.
        joiner: 원문 경로 → 결합 결과 함수.

    Returns:
        경로 필드.
    """
    if path.raw is not None:
        return finish(joiner(path.raw), path.query_tail_stripped)
    prefix = finish(joiner(path.prefix), False) if path.prefix else None
    return with_prefix(DYNAMIC_URL, prefix)


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    """호출의 인자 하나를 위치나 키워드로 찾는다. 그 자리 앞에 `*args`가 있으면 모른다.

    Args:
        call: 호출식.
        position: 위치 인자 자리.
        keyword: 키워드 이름.

    Returns:
        인자 식 또는 None.
    """
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if any(isinstance(argument, ast.Starred) for argument in call.args[: position + 1]):
        return None
    return call.args[position] if position < len(call.args) else None


def _call_arguments(call: ast.Call) -> list[CallArgument]:
    """호출 인자를 쓴 순서(위치 인자 → 키워드 인자)로 바꾼다.

    Args:
        call: 호출식.

    Returns:
        인자 목록.
    """
    arguments = [
        CallArgument(
            None, argument.value if isinstance(argument, ast.Starred) else argument, isinstance(argument, ast.Starred)
        )
        for argument in call.args
    ]
    arguments.extend(CallArgument(item.arg, item.value, item.arg is None) for item in call.keywords)
    return arguments


def _method_from_data(data: ast.expr | None) -> str | None:
    """urllib 요청 데이터로 동사를 정한다.

    Args:
        data: 보낼 데이터 식.

    Returns:
        없거나 None 리터럴이면 GET, None이 아닌 리터럴이면 POST, 모르면 None.
    """
    if data is None or (isinstance(data, ast.Constant) and data.value is None):
        return "GET"
    literal = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.JoinedStr, ast.Set)
    return "POST" if isinstance(data, literal) else None


def _wrapper_exists(index: DefinitionIndex, declaration: WrapperDecl) -> bool:
    """래퍼 선언의 대상이 스캔한 정의에 있는지 본다.

    Args:
        index: 선언 색인.
        declaration: 래퍼 선언.

    Returns:
        있으면 True.
    """
    if declaration.kind == "constructor":
        owner = index.definitions.get(declaration.owner)
        return owner is not None and owner.kind == "class"
    target = index.definitions.get(declaration.target_id)
    return target is not None and target.kind in ("function", "method")


def _project_gaps(project: Project, extraction: ClientExtraction) -> None:
    """순회 상한·링크·파싱 실패처럼 스캔 공백을 호출 측 커버리지로 더한다.

    Args:
        project: 프로젝트.
        extraction: 채울 결과.
    """
    if project.scan_capped:
        extraction.add_gap(COVERAGE, "the project scan stopped at its entry or depth limit")
    if project.unencodable_names:
        extraction.add_gap(
            COVERAGE,
            f"{project.unencodable_names} Python files have names that are not valid UTF-8 and were not analyzed",
        )
    if project.skipped_links:
        extraction.add_gap(COVERAGE, f"{project.skipped_links} symbolic links were not followed")
    for _, module in sorted(project.parsed_modules().items()):
        if isinstance(module, ReadFailure):
            extraction.add_gap(COVERAGE, "{count} Python files could not be analyzed: " + module.reason)
