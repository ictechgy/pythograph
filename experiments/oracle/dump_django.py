"""Django 오라클 덤프다: `get_resolver()` 순회 결과와 pythograph 사실의 표본 해석을 기록한다.

사용법(scratch venv에서만): python dump_django.py <fixture_dir> <pythograph_doc.json> <out.json>

(a) 루트 resolver를 등록 순서대로 순회해 끝점마다 이어 붙인 route 문자열, 패턴 조각, 핸들러 신원,
    허용 method(RequestFactory 탐침: 405면 불허, 그 밖의 상태·예외는 허용)를 기록한다.
(b) pythograph의 정적 route-decl 사실마다 표본 경로를 `resolve()`로 풀어 같은 핸들러·허용 method인지 기록한다.
"""

import os
import re
import sys
from pathlib import Path

from common import (
    PROBE_METHODS,
    dynamic_facts,
    fact_summary,
    load_document,
    object_id,
    sample_path,
    static_facts,
    trailing_check,
    unwrap_function,
    write_json,
)

# Django 설정 모듈 기본값을 manage.py에서 읽는 정규식이다.
SETTINGS_RE = re.compile(r"""DJANGO_SETTINGS_MODULE["']\s*,\s*["']([\w.]+)["']""")


def setup_django(fixture_dir):
    """fixture를 sys.path에 넣고 manage.py의 설정 모듈로 Django를 초기화한다.

    :param fixture_dir: fixture 루트(절대 Path)
    """
    sys.path.insert(0, str(fixture_dir))
    match = SETTINGS_RE.search((fixture_dir / "manage.py").read_text(encoding="utf-8"))
    os.environ["DJANGO_SETTINGS_MODULE"] = match.group(1)
    import django

    django.setup()


def is_api_view_class(cls):
    """DRF `@api_view`가 만든 WrappedAPIView인지 판별한다.

    :param cls: 뷰 클래스
    :returns: WrappedAPIView이면 True
    """
    return cls.__qualname__.endswith("WrappedAPIView")


def api_view_function(cls):
    """WrappedAPIView의 핸들러 클로저에서 원래 함수를 꺼낸다.

    :param cls: WrappedAPIView 클래스
    :returns: 원래 함수
    """
    import inspect

    for name in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
        handler = cls.__dict__.get(name)
        if handler is not None and handler.__closure__:
            closure = inspect.getclosurevars(handler).nonlocals
            if "func" in closure:
                return unwrap_function(closure["func"])
    raise LookupError("api_view 원래 함수를 찾지 못했다")


def handler_id(func, method, fixture_dir):
    """해석된 콜백과 method에서 pythograph 형식 핸들러 신원을 계산한다.

    :param func: resolver가 돌려준 콜백
    :param method: HTTP method(ANY면 GET으로 본다)
    :param fixture_dir: fixture 루트
    :returns: 신원 문자열 또는 None(해당 method의 처리기가 없음)
    """
    verb = "get" if method in ("ANY", "HEAD") else method.lower()
    cls = getattr(func, "cls", None) or getattr(func, "view_class", None)
    if cls is None:
        return object_id(unwrap_function(func), fixture_dir)
    actions = getattr(func, "actions", None)
    if actions is not None:
        action = actions.get(method.lower()) or (actions.get("get") if method == "HEAD" else None)
        return f"{object_id(cls, fixture_dir)}.{action}" if action else None
    if is_api_view_class(cls):
        return object_id(api_view_function(cls), fixture_dir)
    return f"{object_id(cls, fixture_dir)}.{verb}"


def is_class_based(func):
    """콜백이 클래스 기반 뷰(Django CBV·DRF)인지 판별한다.

    :param func: 콜백
    :returns: 클래스 기반이면 True
    """
    cls = getattr(func, "cls", None) or getattr(func, "view_class", None)
    return cls is not None and not is_api_view_class(cls)


def class_allowed_methods(func):
    """클래스 기반 뷰가 받는 method를 프레임워크 자신의 계산으로 구한다.

    DRF `APIView.dispatch`는 인증·권한 검사(`initial`)를 method 검사보다 먼저 하므로, 익명 요청 탐침은 405
    대신 401·403을 받아 허용으로 오판한다. 그래서 클래스 기반 뷰는 탐침 대신 viewset이면 `func.actions`,
    DRF 뷰면 `allowed_methods`, Django 뷰면 `_allowed_methods()`를 쓴다(OPTIONS·자동 HEAD 포함).

    :param func: 콜백
    :returns: 대문자 method 집합, 클래스 기반이 아니면 None
    """
    actions = getattr(func, "actions", None)
    if actions:
        return {method.upper() for method in actions}
    cls = getattr(func, "cls", None) or getattr(func, "view_class", None)
    if cls is None:
        return None
    initkwargs = getattr(func, "initkwargs", None) or getattr(func, "view_initkwargs", None) or {}
    try:
        view = cls(**initkwargs)
    except Exception:  # noqa: BLE001 — 생성자가 실패하면 탐침으로 돌아간다.
        return None
    if hasattr(view, "allowed_methods") and not callable(view.allowed_methods):
        return {method.upper() for method in view.allowed_methods}
    if hasattr(view, "_allowed_methods"):
        return {method.upper() for method in view._allowed_methods()}
    return None


def method_allowed(func, method, args=(), kwargs=None):
    """method가 허용되는지 본다. 클래스 기반 뷰는 프레임워크 계산, 함수 뷰는 RequestFactory 탐침(405 여부)이다.

    :param func: 콜백
    :param method: HTTP method
    :param args: 위치 인자
    :param kwargs: 키워드 인자
    :returns: 허용이면 True
    """
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    allowed = class_allowed_methods(func)
    if allowed is not None:
        return method in allowed
    request = RequestFactory().generic(method, "/")
    request.user = AnonymousUser()
    try:
        response = func(request, *args, **(kwargs or {}))
    except Exception:  # noqa: BLE001 — 뷰 내부 오류는 method 허용 뒤에만 난다.
        return True
    return getattr(response, "status_code", 200) != 405


def endpoint_flags(route, func, namespaces):
    """프레임워크 제공 끝점 표식을 정한다.

    :param route: 이어 붙인 route 문자열
    :param func: 콜백
    :param namespaces: 이 끝점까지의 이름공간 목록
    :returns: 표식 목록
    """
    flags = []
    cls = getattr(func, "cls", None)
    if cls is not None and any(base.__name__ == "APIRootView" for base in cls.__mro__):
        flags.append("drf-api-root")
    if "(?P<format>" in route or "drf_format_suffix" in route:
        flags.append("drf-format-suffix")
    if "admin" in namespaces:
        flags.append("admin")
    return flags


def endpoint_entries(route, func, fixture_dir):
    """끝점 하나의 (method, 핸들러) 항목을 만든다.

    :param route: 이어 붙인 route 문자열
    :param func: 콜백
    :param fixture_dir: fixture 루트
    :returns: 항목 목록
    """
    allowed = [m for m in PROBE_METHODS if method_allowed(func, m)]
    if not is_class_based(func) and len(allowed) == len(PROBE_METHODS):
        return [{"method": "ANY", "handler": handler_id(func, "ANY", fixture_dir)}]
    return [{"method": m, "handler": handler_id(func, m, fixture_dir)} for m in allowed]


def walk_resolver(resolver, prefix, pieces, namespaces, sink, chain=()):
    """resolver를 등록 순서대로 깊이 우선 순회해 끝점을 모은다.

    :param resolver: URLResolver
    :param prefix: 지금까지 이어 붙인 route
    :param pieces: 지금까지의 패턴 조각
    :param namespaces: 지금까지의 이름공간
    :param sink: 끝점을 모을 목록
    :param chain: 지금까지의 패턴 객체(표본 경로를 끝점 하나에 직접 맞출 때 쓴다)
    """
    from django.urls.resolvers import URLResolver

    for pattern in resolver.url_patterns:
        route = URLResolver._join_route(prefix, str(pattern.pattern))
        piece = {"kind": type(pattern.pattern).__name__, "pattern": str(pattern.pattern)}
        if isinstance(pattern, URLResolver):
            walk_resolver(pattern, route, pieces + [piece], namespaces + [pattern.namespace], sink,
                          chain + (pattern.pattern,))
        else:
            sink.append((route, pieces + [piece], namespaces, pattern.callback, chain + (pattern.pattern,)))


# 순회한 모든 끝점(순번, route, 패턴 객체 사슬, 콜백)이다. 가려진 패턴 검증에 쓴다.
TRAVERSAL = []


def chain_matches(chain, path):
    """표본 경로가 끝점 하나의 패턴 사슬에 (다른 패턴과 무관하게) 맞는지 본다.

    Django `URLResolver.resolve`와 같은 방식이다: 루트 `^/`를 떼고 각 단계 패턴의 `match()`로 앞부분을
    소비하며, 끝점 패턴이 나머지를 모두 받아야 한다.

    :param chain: 패턴 객체 사슬
    :param path: 요청 경로
    :returns: (맞으면 True, 인자 튜플, 키워드 인자)
    """
    if not path.startswith("/"):
        return False, (), {}
    rest, args, kwargs = path[1:], (), {}
    for index, pattern in enumerate(chain):
        match = pattern.match(rest)
        if match is None:
            return False, (), {}
        rest, args, kwargs = match[0], args + tuple(match[1]), {**kwargs, **match[2]}
        if index == len(chain) - 1 and rest != "":
            return False, (), {}
    return True, args, kwargs


def dump_endpoints(fixture_dir):
    """루트 resolver의 모든 끝점을 기록으로 바꾼다. 관리자 끝점은 수만 센다.

    :param fixture_dir: fixture 루트
    :returns: (끝점 기록 목록, 건너뛴 수 사전)
    """
    from django.urls import get_resolver

    raw = []
    walk_resolver(get_resolver(), "", [], [], raw)
    TRAVERSAL.clear()
    TRAVERSAL.extend((index, item[0], item[4], item[3]) for index, item in enumerate(raw))
    records, skipped = [], {"admin": 0}
    for index, (route, pieces, namespaces, func, _chain) in enumerate(raw):
        flags = endpoint_flags(route, func, namespaces)
        if "admin" in flags:
            skipped["admin"] += 1
            continue
        entries = endpoint_entries(route, func, fixture_dir)
        records.append({"index": index, "route": route, "pieces": pieces, "flags": flags, "entries": entries})
    return records, skipped


def resolve_path(path):
    """경로를 resolve()로 푼다. 맞는 패턴이 없으면 None이다.

    :param path: 요청 경로
    :returns: ResolverMatch 또는 None
    """
    from django.urls import Resolver404, resolve

    try:
        return resolve(path)
    except Resolver404:
        return None


def probe_fact(fact, fixture_dir):
    """사실 하나를 표본 경로로 풀어 검증 기록을 만든다.

    먼저 `resolve()`(첫 매치)로 풀고, 핸들러가 다르면 순회한 끝점 중 표본에 맞는 뒤쪽 끝점을 등록 순서대로
    찾는다. registration-order 문서는 가려진 패턴도 선언이므로, 뒤쪽 끝점이 같은 핸들러·method면 검증하고
    `shadowedBy`에 가린 route를 적는다.

    :param fact: dynamic이 아닌 route-decl 사실
    :param fixture_dir: fixture 루트
    :returns: 탐침 기록
    """
    path = sample_path(fact)
    usr = fact.get("symbol", {}).get("usr")
    record = {"fact": fact_summary(fact), "sample": path, "resolvedRoute": None, "resolvedHandler": None,
              "shadowedBy": None, "traversalIndex": None}
    method = fact["method"]
    methods = PROBE_METHODS if method == "ANY" else (method,)
    first = None
    for index, route, chain, func in TRAVERSAL:
        matched, args, kwargs = chain_matches(chain, path)
        if not matched:
            continue
        first = first or route
        handler = handler_id(func, method, fixture_dir)
        if usr is not None and handler != usr:
            continue
        allowed = all(method_allowed(func, m, args, kwargs) for m in methods)
        trailing = trailing_check(fact, lambda p, c=chain: chain_matches(c, p)[0])
        verified = allowed and trailing is not False
        return {**record, "resolvedRoute": route, "resolvedHandler": handler, "handlerMatches": usr is None or True,
                "methodAllowed": allowed, "trailingSlashOk": trailing, "verified": verified,
                "shadowedBy": None if first == route else first, "traversalIndex": index}
    return {**record, "resolvedRoute": first, "handlerMatches": False, "methodAllowed": False,
            "trailingSlashOk": None, "verified": False}


def same_handler(path, route, handler, method, fixture_dir):
    """다른 경로가 같은 끝점·핸들러로 풀리는지 본다.

    :param path: 요청 경로
    :param route: 원래 표본이 푼 route
    :param handler: 원래 핸들러 신원
    :param method: method
    :param fixture_dir: fixture 루트
    :returns: 같으면 True
    """
    match = resolve_path(path)
    return match is not None and match.route == route and handler_id(match.func, method, fixture_dir) == handler


def main(argv):
    """명령줄 인자를 받아 덤프를 쓴다.

    :param argv: [fixture_dir, doc_path, out_path]
    """
    fixture_dir = Path(argv[0]).resolve()
    document = load_document(argv[1])
    setup_django(fixture_dir)
    endpoints, skipped = dump_endpoints(fixture_dir)
    probes = [probe_fact(fact, fixture_dir) for fact in static_facts(document)]
    write_json(argv[2], {
        "framework": "django",
        "fixture": fixture_dir.name,
        "endpoints": endpoints,
        "skipped": skipped,
        "probes": probes,
        "dynamicFacts": dynamic_facts(document),
        "limitations": sorted(document.get("limitations", [])),
    })


if __name__ == "__main__":
    main(sys.argv[1:])
