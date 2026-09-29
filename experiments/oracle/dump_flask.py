"""Flask 오라클 덤프다: `app.url_map` 규칙과 pythograph 사실의 표본 매칭을 기록한다.

사용법(scratch venv에서만): python dump_flask.py <fixture_dir> <pythograph_doc.json> <out.json>

fixture의 `blog.create_app()`으로 앱을 만든다. 끝점 method는 규칙의 methods에서 Werkzeug가 자동으로
붙인 HEAD(GET이 있을 때)와 Flask 자동 OPTIONS(`provide_automatic_options`)를 뺀 값이다. 정적 파일
규칙(endpoint `static`)은 프레임워크 제공 경로라 건너뛴다.
"""

import sys
from pathlib import Path

from common import (
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


def create_app(fixture_dir):
    """fixture를 sys.path에 넣고 앱 팩토리로 앱을 만든다.

    :param fixture_dir: fixture 루트(절대 Path)
    :returns: Flask 앱
    """
    sys.path.insert(0, str(fixture_dir))
    from blog import create_app as factory

    return factory()


def declared_methods(rule):
    """규칙의 methods에서 자동으로 붙은 HEAD·OPTIONS를 뺀다.

    :param rule: Werkzeug Rule
    :returns: 정렬한 method 목록
    """
    methods = set(rule.methods or ())
    if "GET" in methods:
        methods.discard("HEAD")
    if getattr(rule, "provide_automatic_options", False):
        methods.discard("OPTIONS")
    return sorted(methods)


def handler_id(app, endpoint, method, fixture_dir):
    """endpoint와 method에서 pythograph 형식 핸들러 신원을 계산한다.

    :param app: Flask 앱
    :param endpoint: 규칙 endpoint
    :param method: HTTP method
    :param fixture_dir: fixture 루트
    :returns: 신원 문자열
    """
    view = app.view_functions[endpoint]
    cls = getattr(view, "view_class", None)
    if cls is None:
        return object_id(unwrap_function(view), fixture_dir)
    verb = method.lower()
    if hasattr(cls, verb) or (verb == "head" and hasattr(cls, "get")):
        return f"{object_id(cls, fixture_dir)}.{verb if hasattr(cls, verb) else 'get'}"
    return f"{object_id(cls, fixture_dir)}.dispatch_request"


def rule_key(rule):
    """규칙 식별 문자열이다. 같은 경로의 규칙이 여럿일 수 있어 endpoint를 붙인다.

    :param rule: Werkzeug Rule
    :returns: 식별 문자열
    """
    return f"{rule.rule} -> {rule.endpoint}"


def dump_endpoints(app, fixture_dir):
    """url_map 규칙을 등록 순서대로 기록한다.

    :param app: Flask 앱
    :param fixture_dir: fixture 루트
    :returns: (끝점 기록 목록, 건너뛴 수 사전)
    """
    records, skipped = [], {"static": 0}
    for index, rule in enumerate(app.url_map.iter_rules()):
        if rule.endpoint == "static":
            skipped["static"] += 1
            continue
        entries = [{"method": m, "handler": handler_id(app, rule.endpoint, m, fixture_dir)} for m in declared_methods(rule)]
        records.append({"index": index, "route": rule_key(rule), "rule": rule.rule, "strictSlashes": rule.strict_slashes,
                        "flags": [], "entries": entries})
    return records, skipped


def match_rule(adapter, path, method):
    """경로와 method를 매칭한다.

    :param adapter: 바인딩한 MapAdapter
    :param path: 요청 경로
    :param method: HTTP method
    :returns: (Rule 또는 None, 결과 문자열)
    """
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    from werkzeug.routing import RequestRedirect

    try:
        rule, _ = adapter.match(path, method=method, return_rule=True)
        return rule, "match"
    except RequestRedirect:
        return None, "redirect"
    except MethodNotAllowed:
        return None, "method-not-allowed"
    except NotFound:
        return None, "not-found"


def probe_fact(app, adapter, fact, fixture_dir):
    """사실 하나를 표본 경로로 매칭해 검증 기록을 만든다.

    :param app: Flask 앱
    :param adapter: 바인딩한 MapAdapter
    :param fact: dynamic이 아닌 route-decl 사실
    :param fixture_dir: fixture 루트
    :returns: 탐침 기록
    """
    path = sample_path(fact)
    method = "GET" if fact["method"] == "ANY" else fact["method"]
    rule, outcome = match_rule(adapter, path, method)
    usr = fact.get("symbol", {}).get("usr")
    record = {"fact": fact_summary(fact), "sample": path, "outcome": outcome}
    if rule is None:
        return {**record, "resolvedRoute": None, "resolvedHandler": None, "handlerMatches": False,
                "methodAllowed": False, "trailingSlashOk": None, "verified": False}
    handler = handler_id(app, rule.endpoint, method, fixture_dir)
    matches = None if usr is None else handler == usr
    trailing = trailing_check(fact, lambda p: same_rule(adapter, p, method, rule))
    verified = matches is not False and trailing is not False
    return {**record, "resolvedRoute": rule_key(rule), "resolvedHandler": handler, "handlerMatches": matches,
            "methodAllowed": True, "trailingSlashOk": trailing, "verified": verified}


def same_rule(adapter, path, method, rule):
    """다른 경로가 같은 규칙으로 매칭되는지 본다.

    :param adapter: 바인딩한 MapAdapter
    :param path: 요청 경로
    :param method: HTTP method
    :param rule: 원래 규칙
    :returns: 같으면 True
    """
    other, _ = match_rule(adapter, path, method)
    return other is rule


def main(argv):
    """명령줄 인자를 받아 덤프를 쓴다.

    :param argv: [fixture_dir, doc_path, out_path]
    """
    fixture_dir = Path(argv[0]).resolve()
    document = load_document(argv[1])
    app = create_app(fixture_dir)
    adapter = app.url_map.bind("localhost")
    endpoints, skipped = dump_endpoints(app, fixture_dir)
    probes = [probe_fact(app, adapter, fact, fixture_dir) for fact in static_facts(document)]
    write_json(argv[2], {
        "framework": "flask",
        "fixture": fixture_dir.name,
        "endpoints": endpoints,
        "skipped": skipped,
        "probes": probes,
        "dynamicFacts": dynamic_facts(document),
        "limitations": sorted(document.get("limitations", [])),
    })


if __name__ == "__main__":
    main(sys.argv[1:])
