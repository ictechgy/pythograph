"""종단 검증의 trace context와 세 질문의 기대 경로(`run_trace.py`와 `tests/test_e2e_trace.py`가 함께 쓴다)."""

# 서버 member의 합성 project 경로다.
SERVER_PROJECT = "/e2e/shop-api"


def trace_contexts():
    """두 trace context를 만든다(문서 경로는 context 파일 기준 상대 경로).

    :returns: {이름: context}
    """
    members = [
        {
            "name": "server",
            "project": SERVER_PROJECT,
            "revision": "e2e-shop-api",
            "documents": ["server.http.json", "server.persistence.json", "server.sql.json"],
            "analyses": [
                {"id": "server-forward", "platform": "python", "role": "forward", "path": "server-forward.json"},
                {"id": "server-reverse", "platform": "python", "role": "reverse", "path": "server-reverse.json"},
                {"id": "server-db", "platform": "sql", "role": "db-dependents", "path": "server-db.json"},
            ],
        },
        {
            "name": "ios",
            "project": "/e2e/clients/ios",
            "revision": "e2e-ios-client",
            "documents": ["ios.http.json"],
            "analyses": [{"id": "ios-reverse", "platform": "swift", "role": "reverse", "path": "ios-reverse.json"}],
        },
        {
            "name": "android",
            "project": "/e2e/clients/android",
            "revision": "a11d000000000000000000000000000000000e2e",
            "documents": ["android.http.json"],
            "analyses": [
                {"id": "android-reverse", "platform": "kotlin", "role": "reverse", "path": "android-reverse.json"}
            ],
        },
    ]
    links = [
        {"name": "ios->api", "client": "ios", "server": "server", "match": {"hosts": ["api.example.com"]}},
        {"name": "android->api", "client": "android", "server": "server", "match": {"hosts": ["api.example.com"]}},
    ]
    routes = [
        {"method": "GET", "template": "/api/orders/{}/"},
        {"method": "POST", "template": "/api/orders/{}/cancel/"},
        {"method": "GET", "template": "/api/products/"},
        {"method": "POST", "template": "/api/checkout/"},
    ]
    relations = [{"member": "server", "name": "store_auditentry"}, {"member": "server", "name": "store_product"}]
    base = {"format": "isthmus-trace-context", "version": 1, "members": members, "links": links}
    return {"routes": {**base, "selection": {"routes": routes}}, "relations": {**base, "selection": {"relations": relations}}}


# iOS(cartograph) 심볼이다.
IOS = {
    "fetch": "s:10ShopClient9OrdersAPIC10fetchOrder2id10Foundation4DataVSi_tYaKF",
    "cancel": "s:10ShopClient9OrdersAPIC11cancelOrder2id10Foundation4DataVSi_tYaKF",
    "products": "s:10ShopClient11ProductsAPIC4list10Foundation4DataVyYaKF",
    "load": "s:10ShopClient20OrderDetailViewModelC4load2idySi_tYaKF",
    "appear": "s:10ShopClient17OrderDetailScreenC6appearyyYaKF",
    "vm_cancel": "s:10ShopClient20OrderDetailViewModelC6cancel2idySi_tYaKF",
    "tap_cancel": "s:10ShopClient17OrderDetailScreenC9tapCancelyyYaKF",
    "catalog": "s:10ShopClient16CatalogViewModelC7refreshyyYaKF",
}

# Android(kartograph) 심볼이다. Retrofit 인터페이스 메서드의 usr와 baseUrl 결합(authority·root 템플릿)은 kartograph
# `4c09d91`(#122·#123)부터 나온다 — 그 전 판으로 기록하면 주문·결제 호출이 host link에 귀속되지 않는다.
ANDROID = {
    "get_order": "method:com/example/shop/OrdersService#getOrder(I)Lretrofit2/Call;",
    "load": "method:com/example/shop/OrderRepository#load(I)Ljava/util/Map;",
    "order_refresh": "method:com/example/shop/OrderViewModel#refresh(I)V",
    "checkout": "method:com/example/shop/OrdersService#checkout(Ljava/util/Map;)Lretrofit2/Call;",
    "submit": "method:com/example/shop/CheckoutRepository#submit()Ljava/util/Map;",
    "pay": "method:com/example/shop/CheckoutViewModel#pay()V",
    "products": "method:com/example/shop/ProductsClient#list()Ljava/lang/String;",
    "catalog": "method:com/example/shop/CatalogViewModel#refresh()V",
}

# 질문별 기대 경로다: (선택자 키, 핸들러, [(relation-use 심볼, relation, reachedFrom 경로)], {DB 정점: 의존자},
# {scope: [(호출 심볼, [(영향 심볼, 깊이)])]}).
ROUTE_EXPECTATIONS = [
    (
        ("GET", "/api/orders/{}/"),
        "store/views.py#OrderViewSet.retrieve",
        [("store/selectors.py#orders_for_customer", "main.store_order", None)],
        {"main.store_order": ["main.store_open_orders", "main.store_orderline"]},
        {
            "ios->api": [(IOS["fetch"], [(IOS["load"], 1), (IOS["appear"], 2)])],
            "android->api": [(ANDROID["get_order"], [(ANDROID["load"], 1), (ANDROID["order_refresh"], 2)])],
        },
    ),
    (
        ("POST", "/api/orders/{}/cancel/"),
        "store/views.py#OrderViewSet.cancel",
        [
            (
                "store/services.py#OrderService.cancel",
                "main.store_order",
                ["store/views.py#OrderViewSet.cancel", "store/services.py#OrderService.cancel"],
            ),
            (
                "store/audit.py#record",
                "main.store_auditentry",
                ["store/views.py#OrderViewSet.cancel", "store/services.py#OrderService.cancel", "store/audit.py#record"],
            ),
        ],
        {"main.store_order": ["main.store_open_orders", "main.store_orderline"], "main.store_auditentry": []},
        {"ios->api": [(IOS["cancel"], [(IOS["vm_cancel"], 1), (IOS["tap_cancel"], 2)])], "android->api": []},
    ),
    (
        ("GET", "/api/products/"),
        "store/views.py#ProductListView.get",
        [
            (
                "store/selectors.py#active_products",
                "main.store_product",
                [
                    "store/views.py#ProductListView.get",
                    "store/views.py#ProductListView.get_queryset",
                    "store/selectors.py#active_products",
                ],
            )
        ],
        {"main.store_product": ["main.store_orderline"]},
        {
            "ios->api": [(IOS["products"], [(IOS["catalog"], 1)])],
            "android->api": [(ANDROID["products"], [(ANDROID["catalog"], 1)])],
        },
    ),
    (
        ("POST", "/api/checkout/"),
        "store/views.py#CheckoutView.post",
        [
            (
                "store/services.py#OrderService.place",
                "main.store_order",
                ["store/views.py#CheckoutView.post", "store/services.py#OrderService.place"],
            ),
            (
                "store/services.py#OrderService.place",
                "main.store_orderline",
                ["store/views.py#CheckoutView.post", "store/services.py#OrderService.place"],
            ),
        ],
        {"main.store_order": ["main.store_open_orders", "main.store_orderline"], "main.store_orderline": []},
        {"ios->api": [], "android->api": [(ANDROID["checkout"], [(ANDROID["submit"], 1), (ANDROID["pay"], 2)])]},
    ),
]

# relation 선택의 기대 경로다: (relation, 핸들러, route, {scope: [(호출 심볼, [(영향 심볼, 깊이)])]}).
RELATION_EXPECTATIONS = [
    (
        "store_auditentry",
        "store/views.py#OrderViewSet.cancel",
        ("POST", "/api/orders/{}/cancel/"),
        {"ios->api": [(IOS["cancel"], [(IOS["vm_cancel"], 1), (IOS["tap_cancel"], 2)])], "android->api": []},
    ),
    (
        "store_product",
        "store/views.py#ProductListView.get",
        ("GET", "/api/products/"),
        {
            "ios->api": [(IOS["products"], [(IOS["catalog"], 1)])],
            "android->api": [(ANDROID["products"], [(ANDROID["catalog"], 1)])],
        },
    ),
]

# 기대하는 gap 코드(개수)다. 빈 목록·gap 없음이 완전성의 증거가 아니듯, 여기 없는 gap이 생기면 원인을 확인해야 한다.
ROUTE_GAPS = {"reach-possibly-incomplete": 2}
RELATION_GAPS = {"non-http-entry": 3}


def _gap_problems(trace, expected, label):
    """gap 코드 개수를 비교한다.

    :param trace: trace 문서
    :param expected: 코드 → 개수
    :param label: 문구 머리
    :returns: 불일치 목록
    """
    counts = {}
    for gap in trace["gaps"]:
        counts[gap["code"]] = counts.get(gap["code"], 0) + 1
    return [] if counts == expected else [f"{label}: gaps {counts} != {expected}"]


def _calls_problems(routes, expected, label):
    """scope별 호출과 영향 심볼을 비교한다.

    :param routes: 체인의 route 항목
    :param expected: scope → [(호출 심볼, [(영향 심볼, 깊이)])]
    :param label: 문구 머리
    :returns: 불일치 목록
    """
    problems = []
    for scope, calls in expected.items():
        route = next((item for item in routes if item["scope"] == scope), None)
        if route is None:
            problems.append(f"{label}: no route for scope {scope}")
            continue
        actual = sorted(
            (call["call"]["symbol"]["usr"], sorted((hop["usr"], hop["depth"]) for hop in call["affected"]))
            for call in route["calls"]
        )
        wanted = sorted((usr, sorted(hops)) for usr, hops in calls)
        if actual != wanted:
            problems.append(f"{label}: {scope} calls {actual} != {wanted}")
    return problems


def _chain(trace, selector):
    """선택자로 체인을 찾는다.

    :param trace: trace 문서
    :param selector: 체인 선택자
    :returns: 체인 또는 None
    """
    return next((chain for chain in trace["chains"] if chain["selector"] == selector), None)


def check_routes(trace):
    """질문 (a) API → DB 테이블·DB 의존자와 (b) API → 클라이언트 호출부 → 영향 심볼을 확인한다.

    :param trace: route 선택 trace
    :returns: 불일치 목록
    """
    problems = _gap_problems(trace, ROUTE_GAPS, "routes")
    for (method, template), handler, uses, database, calls in ROUTE_EXPECTATIONS:
        label = f"{method} {template}"
        chain = _chain(trace, {"route": {"method": method, "template": template}})
        if chain is None:
            problems.append(f"{label}: no chain")
            continue
        if [item["usr"] for item in chain["handlers"]] != [handler]:
            problems.append(f"{label}: handlers {[item['usr'] for item in chain['handlers']]}")
        problems.extend(_use_problems(chain, handler, uses, label))
        problems.extend(_database_problems(chain, database, label))
        problems.extend(_calls_problems(chain["routes"], calls, label))
    return problems


def _use_problems(chain, handler, uses, label):
    """relation-use 도달(질문 a)을 비교한다. 경로가 None이면 다른 root 목격(`witnessRoot`)을 허용한다.

    :param chain: 체인
    :param handler: 핸들러 usr
    :param uses: [(심볼, relation 선언, 경로)]
    :param label: 문구 머리
    :returns: 불일치 목록
    """
    problems = []
    for symbol, decl, path in uses:
        found = [
            reach
            for item in chain["relationUses"]
            if item["use"].get("symbol", {}).get("usr") == symbol
            and decl in [entry.get("symbol", {}).get("usr") for entry in item["decls"]]
            for reach in item["reachedFrom"]
            if reach["from"] == handler
        ]
        if not found:
            problems.append(f"{label}: {symbol} -> {decl} not reached from {handler}")
        elif path is not None and not any(reach["path"] == path and "witnessRoot" not in reach for reach in found):
            problems.append(f"{label}: {symbol} path {[reach['path'] for reach in found]} != {path}")
    return problems


def _database_problems(chain, database, label):
    """DB 정점과 의존자(질문 a)를 비교한다.

    :param chain: 체인
    :param database: 정점 → 의존자 목록
    :param label: 문구 머리
    :returns: 불일치 목록
    """
    actual = {item["vertex"]: sorted(dependent["usr"] for dependent in item["dependents"]) for item in chain["database"]}
    return [
        f"{label}: {vertex} dependents {actual.get(vertex)} != {sorted(dependents)}"
        for vertex, dependents in database.items()
        if actual.get(vertex) != sorted(dependents)
    ]


def check_relations(trace):
    """질문 (c) 테이블 → API → 클라이언트를 확인한다.

    :param trace: relation 선택 trace
    :returns: 불일치 목록
    """
    problems = _gap_problems(trace, RELATION_GAPS, "relations")
    for relation, handler, (method, template), calls in RELATION_EXPECTATIONS:
        chain = _chain(trace, {"member": "server", "relation": relation})
        if chain is None:
            problems.append(f"{relation}: no chain")
            continue
        if [item["usr"] for item in chain["handlers"]] != [handler]:
            problems.append(f"{relation}: handlers {[item['usr'] for item in chain['handlers']]}")
        routes = [item for item in chain["routes"] if (item["method"], item["template"]) == (method, template)]
        if len(routes) != len(chain["routes"]):
            problems.append(f"{relation}: unexpected routes {[(item['method'], item['template']) for item in chain['routes']]}")
        problems.extend(_calls_problems(routes, calls, relation))
    return problems
