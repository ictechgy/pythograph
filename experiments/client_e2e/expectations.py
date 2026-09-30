"""파이썬 클라이언트 종단 trace의 context와 기대 경로(`run_trace.py`와 `tests/test_client_e2e.py`가 함께 쓴다).

서버 member 문서는 Phase 6 종단 기록(`experiments/e2e/recorded/`)을 그대로 쓴다 — 같은 pythograph로 만든 shop-api
문서이고 `tests/test_e2e_trace.py`가 지금 출력과 같은지 확인한다.
"""

# 클라이언트 member의 합성 project 경로다.
CLIENT_PROJECT = "/e2e/clients/python"

# 클라이언트 member의 합성 revision이다.
CLIENT_REVISION = "e2e-python-client"

# 서버 member 문서가 있는 곳(context 파일 기준 상대 경로)이다.
SERVER_RECORDED = "../../e2e/recorded"

# 선택한 route와 기대 체인: (method, template, 핸들러, 호출 usr, [(영향 심볼, 깊이)], relation-use 테이블).
EXPECTED = [
    (
        "GET",
        "/api/orders/{}/",
        "store/views.py#OrderViewSet.retrieve",
        "shopcli/api.py#OrdersApi.fetch",
        [("shopcli/screens.py#OrderScreen.show", 1)],
        ["main.store_order"],
    ),
    (
        "POST",
        "/api/orders/{}/cancel/",
        "store/views.py#OrderViewSet.cancel",
        "shopcli/api.py#OrdersApi.cancel",
        [("shopcli/screens.py#OrderScreen.tap_cancel", 1)],
        ["main.store_order", "main.store_auditentry"],
    ),
    (
        "GET",
        "/api/products/",
        "store/views.py#ProductListView.get",
        "shopcli/api.py#list_products",
        [("shopcli/screens.py#catalog_refresh", 1)],
        ["main.store_product"],
    ),
    (
        "POST",
        "/api/checkout/",
        "store/views.py#CheckoutView.post",
        "shopcli/api.py#checkout",
        [("shopcli/screens.py#pay", 1)],
        ["main.store_order", "main.store_orderline"],
    ),
]


def trace_context():
    """route 선택 trace context를 만든다(문서 경로는 context 파일 기준 상대 경로).

    :returns: context
    """
    server = SERVER_RECORDED
    members = [
        {
            "name": "server",
            "project": "/e2e/shop-api",
            "revision": "e2e-shop-api",
            "documents": [f"{server}/server.http.json", f"{server}/server.persistence.json", f"{server}/server.sql.json"],
            "analyses": [
                {"id": "server-forward", "platform": "python", "role": "forward", "path": f"{server}/server-forward.json"},
                {"id": "server-reverse", "platform": "python", "role": "reverse", "path": f"{server}/server-reverse.json"},
                {"id": "server-db", "platform": "sql", "role": "db-dependents", "path": f"{server}/server-db.json"},
            ],
        },
        {
            "name": "python",
            "project": CLIENT_PROJECT,
            "revision": CLIENT_REVISION,
            "documents": ["python.http.json"],
            "analyses": [{"id": "python-reverse", "platform": "python", "role": "reverse", "path": "python-reverse.json"}],
        },
    ]
    links = [{"name": "python->api", "client": "python", "server": "server", "match": {"hosts": ["api.example.com"]}}]
    routes = [{"method": method, "template": template} for method, template, *_ in EXPECTED]
    return {
        "format": "isthmus-trace-context",
        "version": 1,
        "members": members,
        "links": links,
        "selection": {"routes": routes},
    }


def _strings(value):
    """JSON 값 안의 모든 문자열을 모은다.

    :param value: JSON 값
    :returns: 문자열 집합
    """
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_strings(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_strings(item) for item in value)) if value else set()
    return set()


def _chain_for(trace, method, template):
    """trace 결과에서 route 하나의 체인을 찾는다.

    :param trace: trace 문서
    :param method: 동사
    :param template: 템플릿
    :returns: 체인 dict 또는 None
    """
    for chain in trace.get("chains", []):
        route = chain.get("selector", {}).get("route", {})
        if route.get("method") == method and route.get("template") == template:
            return chain
    return None


def _calls(chain):
    """체인의 호출부 항목을 모은다.

    :param chain: 체인 dict
    :returns: 호출 항목 목록
    """
    return [call for route in chain.get("routes", []) for call in route.get("calls", [])]


def check(trace):
    """기대 체인을 확인한다: route → 핸들러 → relation, route ← 파이썬 호출부 ← 영향 심볼.

    :param trace: isthmus trace 문서
    :returns: 문제 목록(비면 통과)
    """
    problems = []
    for method, template, handler, caller, impacted, relations in EXPECTED:
        chain = _chain_for(trace, method, template)
        if chain is None:
            problems.append(f"{method} {template}: no chain")
            continue
        texts = _strings(chain)
        for wanted in [handler, *relations]:
            if wanted not in texts:
                problems.append(f"{method} {template}: {wanted} missing")
        calls = [item for item in _calls(chain) if item["call"]["symbol"].get("usr") == caller]
        if len(calls) != 1 or calls[0]["quality"] != "exact":
            problems.append(f"{method} {template}: call {caller} is not an exact match")
            continue
        affected = sorted((entry["usr"], entry["depth"]) for entry in calls[0].get("affected", []))
        if affected != sorted(impacted):
            problems.append(f"{method} {template}: affected {affected} != {sorted(impacted)}")
    return problems
