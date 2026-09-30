"""HTTP 클라이언트 라이브러리 표와 클라이언트 객체 추적.

라이브러리 동작은 설치본 소스로 확인한 것만 쓴다(출처는 `docs/HTTP-CLIENTS.md`). 분석 대상 코드를 실행하지 않고
이름 해석(`graph.scope.Resolver`)으로 외부 이름을 판정하므로, 프로젝트가 같은 이름의 함수를 정의해도 라이브러리
호출로 보지 않는다.

| 라이브러리 | 요청 API | base 결합 | 점 세그먼트 |
|---|---|---|---|
| requests | 최상위 동사 함수·`request`, `Session`(=`session()`) 메서드 | base 없음 | 지운다(urllib3 `parse_url`) |
| httpx | 최상위 동사 함수·`request`·`stream`, `Client`·`AsyncClient` 메서드 | `httpx-merge` | 지운다 |
| aiohttp | `ClientSession` 메서드, 최상위 `request` | `rfc3986`(3.11+의 경로 있는 base, 끝 `/` 필수) | 지운다(yarl) |
| urllib | `urllib.request.urlopen(url 또는 Request)` | base 없음, `urllib.parse.urljoin`은 `rfc3986` | 지우지 않는다 |
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.graph.index import Definition
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue, ExternalValue, InstanceValue, ModuleValue
from pythograph.routes.client.compose import (
    JOIN_HTTPX,
    JOIN_RFC3986,
    JOIN_STRING,
    UNKNOWN_BASE,
    BaseUrl,
    base_from_parts,
)
from pythograph.routes.client.parts import PartBuilder, attribute_assignments
from pythograph.source.symbols import ValueSymbol

#: 동사 함수·메서드 이름 → 동사다(세 라이브러리 공통, `trace`는 어느 라이브러리에도 없다).
VERB_NAMES = {
    "get": "GET",
    "head": "HEAD",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "options": "OPTIONS",
}

#: 라이브러리 이름 → 점 세그먼트를 지우는지다.
REMOVES_DOTS = {"requests": True, "httpx": True, "aiohttp": True, "urllib": False}

#: 라이브러리 이름 → 동사 인자를 대문자로 바꾸는지다(requests `prepare_method`, httpx `Request`, aiohttp
#: `ClientSession._request`는 `upper()`, urllib `Request.get_method`는 그대로 보낸다).
UPPERCASES_METHOD = {"requests": True, "httpx": True, "aiohttp": True, "urllib": False}

#: 최상위 요청 함수: 점 경로 → (라이브러리, 동사 또는 None(첫 인자가 동사)).
MODULE_FUNCTIONS: dict[str, tuple[str, str | None]] = {
    **{f"requests.{name}": ("requests", verb) for name, verb in VERB_NAMES.items()},
    **{f"requests.api.{name}": ("requests", verb) for name, verb in VERB_NAMES.items()},
    "requests.request": ("requests", None),
    "requests.api.request": ("requests", None),
    **{f"httpx.{name}": ("httpx", verb) for name, verb in VERB_NAMES.items()},
    "httpx.request": ("httpx", None),
    "httpx.stream": ("httpx", None),
    "aiohttp.request": ("aiohttp", None),
}

#: 클라이언트 클래스: 점 경로 → 라이브러리.
CLIENT_CLASSES = {
    "requests.Session": "requests",
    "requests.session": "requests",
    "requests.sessions.Session": "requests",
    "requests.sessions.session": "requests",
    "httpx.Client": "httpx",
    "httpx.AsyncClient": "httpx",
    "aiohttp.ClientSession": "aiohttp",
    "aiohttp.client.ClientSession": "aiohttp",
}

#: 클라이언트 객체의 요청 메서드: 이름 → 동사 또는 None(첫 인자가 동사). `stream`은 httpx만 있다.
CLIENT_METHODS: dict[str, str | None] = {**VERB_NAMES, "request": None, "stream": None}

#: 모델링하지 않는 클라이언트 객체 메서드(요청 객체를 보내거나 만든다, 웹소켓).
UNMODELED_CLIENT_METHODS = frozenset({"send", "build_request", "prepare_request", "ws_connect"})

#: 모델링하지 않는 요청 API와 요청 객체 생성자(점 경로 또는 점 경로 접두사 `…*`).
UNMODELED_CALLS = (
    "urllib3.request",
    "urllib3.PoolManager",
    "urllib3.ProxyManager",
    "urllib3.HTTPConnectionPool",
    "urllib3.HTTPSConnectionPool",
    "urllib3.connection_from_url",
    "urllib3.poolmanager.PoolManager",
    "http.client.HTTPConnection",
    "http.client.HTTPSConnection",
    "urllib.request.build_opener",
    "urllib.request.OpenerDirector",
    "requests.Request",
    "requests.PreparedRequest",
    "httpx.Request",
    "aiohttp.ClientRequest",
    "tornado.httpclient.AsyncHTTPClient",
    "tornado.httpclient.HTTPClient",
    "tornado.httpclient.HTTPRequest",
    "pycurl.Curl",
    "treq.*",
    "grequests.*",
    "requests_futures.*",
)


@dataclass(frozen=True)
class ClientKind:
    """요청을 보내는 객체(또는 모듈)의 판정.

    Attributes:
        library: `requests`·`httpx`·`aiohttp`·`urllib`.
        style: 상대 경로 결합 방식(`JOIN_*`).
        base: base URL 판정. None이면 base가 없다(상대 URL 요청은 라이브러리가 거부한다).
        invalid: base 설정이 라이브러리에서 오류를 낸다(aiohttp base 경로가 `/`로 끝나지 않음).
    """

    library: str
    style: str
    base: BaseUrl | None = None
    invalid: bool = False


def module_client(library: str) -> ClientKind:
    """base 없는 최상위 호출의 판정을 만든다.

    Args:
        library: 라이브러리 이름.

    Returns:
        판정.
    """
    return ClientKind(library, JOIN_STRING)


def is_unmodeled_call(dotted: str) -> bool:
    """모델링하지 않는 요청 API인지 본다.

    Args:
        dotted: 외부 점 경로.

    Returns:
        모델링하지 않는 API면 True.
    """
    for entry in UNMODELED_CALLS:
        if entry.endswith(".*") and dotted.startswith(entry[:-1]):
            return True
        if dotted == entry:
            return True
    return False


class ClientTracker:
    """식이 가리키는 클라이언트 객체를 찾는다."""

    def __init__(self, resolver: Resolver, builder: PartBuilder, base_rewritten: bool) -> None:
        """추적기를 만든다.

        Args:
            resolver: 정적 값 해석기.
            builder: URL 조각 도우미.
            base_rewritten: 프로젝트가 어떤 객체의 `base_url` 속성을 대입하는지(httpx `Client.base_url` setter).
                참이면 httpx 리터럴 base를 믿지 않는다.
        """
        self.resolver = resolver
        self.builder = builder
        self.base_rewritten = base_rewritten
        self._attributes: dict[tuple[str, str, bool], ClientKind | None] = {}

    def client_of(self, scope: Definition, expr: ast.expr, depth: int = 0) -> ClientKind | None:
        """식이 가리키는 클라이언트 객체를 찾는다.

        Args:
            scope: 식을 담은 범위.
            expr: 수신자 식.
            depth: 재귀 깊이.

        Returns:
            판정, 클라이언트임을 증명하지 못하면 None.
        """
        if depth > 16:
            return None
        if isinstance(expr, ast.Call):
            return self._constructed(scope, expr)
        if isinstance(expr, ast.Name):
            return self._name(scope, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            return self._attribute(scope, expr, depth)
        if isinstance(expr, ast.Await):
            return None
        return None

    def _constructed(self, scope: Definition, call: ast.Call) -> ClientKind | None:
        """클라이언트 생성자 호출을 판정한다.

        Args:
            scope: 범위.
            call: 호출 식.

        Returns:
            판정 또는 None.
        """
        callee = self.resolver.value(scope, call.func)
        library = CLIENT_CLASSES.get(callee.dotted) if isinstance(callee, ExternalValue) else None
        if library is None:
            return None
        if library == "requests":
            return ClientKind("requests", JOIN_STRING)
        position = 0 if library == "aiohttp" else None
        base_expr, blocked = _base_argument(call, position)
        if base_expr is None:
            return ClientKind(library, style_for(library), UNKNOWN_BASE if blocked else None)
        if isinstance(base_expr, ast.Constant) and base_expr.value is None:
            return ClientKind(library, style_for(library))
        base = base_from_parts(self.builder.parts(scope, base_expr))
        return self._checked(library, base)

    def _checked(self, library: str, base: BaseUrl) -> ClientKind:
        """라이브러리별 base 제약을 적용한다.

        Args:
            library: `httpx` 또는 `aiohttp`.
            base: base 판정.

        Returns:
            판정.
        """
        if library == "httpx" and self.base_rewritten:
            base = UNKNOWN_BASE
        if library == "aiohttp" and base.known:
            path = base.path or "/"
            if not path.endswith("/"):
                return ClientKind(library, JOIN_RFC3986, base, invalid=True)
            base = BaseUrl(True, path, base.authority, base.rooted)
        return ClientKind(library, style_for(library), base)

    def _name(self, scope: Definition, name: str, depth: int) -> ClientKind | None:
        """이름이 가리키는 클라이언트를 찾는다(지역 묶음 → 모듈 전역).

        Args:
            scope: 범위.
            name: 이름.
            depth: 재귀 깊이.

        Returns:
            판정 또는 None.
        """
        binding = self.builder.local_binding(scope, name)
        if binding is None:
            symbol = self.resolver.symbols.resolve_name(scope.path, name)
            return self._module_value(symbol, depth)
        if binding.kind in ("assign", "with") and binding.value is not None:
            return self.client_of(binding.scope, binding.value, depth + 1)
        if binding.kind == "param" and binding.value is not None and binding.scope.parent is not None:
            return self.annotated(binding.scope.parent, binding.value)
        return None

    def _module_value(self, symbol: object, depth: int) -> ClientKind | None:
        """모듈 수준 변수의 클라이언트를 찾는다.

        Args:
            symbol: 이름 해석 결과.
            depth: 재귀 깊이.

        Returns:
            판정 또는 None.
        """
        if not isinstance(symbol, ValueSymbol):
            return None
        module = self.resolver.index.definitions.get(f"{symbol.path}#<module>")
        return self.client_of(module, symbol.node, depth + 1) if module is not None else None

    def _attribute(self, scope: Definition, expr: ast.Attribute, depth: int) -> ClientKind | None:
        """속성 접근이 가리키는 클라이언트를 찾는다(인스턴스 필드·클래스 속성·모듈 변수).

        Args:
            scope: 범위.
            expr: 속성 접근 식.
            depth: 재귀 깊이.

        Returns:
            판정 또는 None.
        """
        receiver = self.resolver.value(scope, expr.value)
        if isinstance(receiver, (ClassValue, InstanceValue)):
            return self.attribute_client(receiver.definition, expr.attr, receiver.exact, depth)
        if isinstance(receiver, ModuleValue):
            symbol = self.resolver.symbols.resolve_name(receiver.path, expr.attr)
            return self._module_value(symbol, depth)
        return None

    def attribute_client(self, owner: Definition, name: str, exact: bool, depth: int = 0) -> ClientKind | None:
        """클래스 속성·인스턴스 필드의 모든 대입이 같은 라이브러리의 클라이언트면 그 판정을 돌려준다(캐시).

        base가 대입마다 다르면 base를 모른다. 대입 하나라도 클라이언트임을 증명하지 못하면 None이다.

        Args:
            owner: 수신자의 정적 클래스.
            name: 속성 이름.
            exact: 수신자 클래스가 정확한지.
            depth: 재귀 깊이.

        Returns:
            판정 또는 None.
        """
        key = (owner.id, name, exact)
        if key not in self._attributes:
            self._attributes[key] = None
            self._attributes[key] = self._attribute_client(owner, name, exact, depth)
        return self._attributes[key]

    def _attribute_client(self, owner: Definition, name: str, exact: bool, depth: int) -> ClientKind | None:
        """`attribute_client`의 계산이다.

        Args:
            owner: 수신자의 정적 클래스.
            name: 속성 이름.
            exact: 수신자 클래스가 정확한지.
            depth: 재귀 깊이.

        Returns:
            판정 또는 None.
        """
        if self.builder.writes.touches(self.resolver, owner, name):
            return None
        kinds = [
            self.client_of(scope, value, depth + 1)
            for scope, value in attribute_assignments(self.resolver, owner, name, exact)
        ]
        kinds.extend(self._annotated_attributes(owner, name))
        if not kinds or any(kind is None for kind in kinds):
            return None
        return _merge_kinds([kind for kind in kinds if kind is not None])

    def _annotated_attributes(self, owner: Definition, name: str) -> list[ClientKind | None]:
        """클래스 본문의 값 없는 주석(`client: httpx.Client`)을 판정한다(데이터 클래스·주입 필드).

        Args:
            owner: 클래스 정의.
            name: 속성 이름.

        Returns:
            판정 목록.
        """
        node = owner.node
        if not isinstance(node, ast.ClassDef):
            return []
        return [
            self.annotated(owner, statement.annotation)
            for statement in node.body
            if isinstance(statement, ast.AnnAssign)
            and statement.value is None
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ]

    def annotated(self, scope: Definition, annotation: ast.expr) -> ClientKind | None:
        """타입 주석이 클라이언트 클래스면 base를 모르는 판정을 돌려준다(주입된 클라이언트).

        Args:
            scope: 주석을 푸는 범위.
            annotation: 주석 식(`X`, `X | None`, `Optional[X]`).

        Returns:
            판정 또는 None.
        """
        target = _annotation_target(annotation)
        if target is None:
            return None
        resolved = self.resolver.value(scope, target)
        library = CLIENT_CLASSES.get(resolved.dotted) if isinstance(resolved, ExternalValue) else None
        if library is None:
            return None
        return ClientKind(library, style_for(library), None if library == "requests" else UNKNOWN_BASE)


def style_for(library: str) -> str:
    """라이브러리의 상대 경로 결합 방식을 돌려준다.

    Args:
        library: 라이브러리 이름.

    Returns:
        결합 방식 이름.
    """
    return {"httpx": JOIN_HTTPX, "aiohttp": JOIN_RFC3986}.get(library, JOIN_STRING)


def _base_argument(call: ast.Call, position: int | None) -> tuple[ast.expr | None, bool]:
    """생성자 호출의 `base_url` 인자를 찾는다.

    Args:
        call: 생성자 호출.
        position: 위치 인자로 받는 자리(aiohttp는 0, httpx는 키워드 전용이라 None).

    Returns:
        (인자 식 또는 None, `*args`·`**kwargs` 때문에 있는지 모르는지).
    """
    for keyword in call.keywords:
        if keyword.arg == "base_url":
            return keyword.value, False
    if position is not None and len(call.args) > position:
        argument = call.args[position]
        if isinstance(argument, ast.Starred):
            return None, True
        return argument, False
    blocked = any(keyword.arg is None for keyword in call.keywords) or (
        position is not None and any(isinstance(argument, ast.Starred) for argument in call.args)
    )
    return None, blocked


def _merge_kinds(kinds: list[ClientKind]) -> ClientKind | None:
    """여러 대입의 판정을 합친다.

    Args:
        kinds: 판정 목록(비어 있지 않음).

    Returns:
        같은 라이브러리면 합친 판정(base가 다르면 모름), 아니면 None.
    """
    first = kinds[0]
    if any(kind.library != first.library for kind in kinds):
        return None
    if all(kind == first for kind in kinds):
        return first
    if first.library == "requests":
        return ClientKind("requests", JOIN_STRING)
    return ClientKind(first.library, first.style, UNKNOWN_BASE)


def _annotation_target(annotation: ast.expr) -> ast.expr | None:
    """주석에서 클래스 식을 꺼낸다(`Optional[X]`, `X | None`, 문자열 주석).

    Args:
        annotation: 주석 식.

    Returns:
        클래스 식 또는 None.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value, mode="eval").body
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
        return _annotation_target(parsed)
    if isinstance(annotation, (ast.Name, ast.Attribute)):
        return annotation
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        sides = [side for side in (annotation.left, annotation.right) if not _is_none(side)]
        return _annotation_target(sides[0]) if len(sides) == 1 else None
    if isinstance(annotation, ast.Subscript) and _subscript_name(annotation.value) == "Optional":
        return _annotation_target(annotation.slice)
    return None


def _is_none(node: ast.expr) -> bool:
    """`None` 주석인지 본다.

    Args:
        node: 식.

    Returns:
        `None`이면 True.
    """
    return isinstance(node, ast.Constant) and node.value is None


def _subscript_name(node: ast.expr) -> str:
    """첨자 주석의 이름(`Optional`, `typing.Optional`)을 돌려준다.

    Args:
        node: 첨자 대상 식.

    Returns:
        마지막 이름(없으면 빈 문자열).
    """
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""
