"""교환 형식의 HTTP method 집합(모든 계층이 함께 쓰는 한 벌).

정본은 isthmus `docs/GRAPH-EXCHANGE.md`의 http 사실 절이다. `method`는 `GET`·`HEAD`·`POST`·`PUT`·`PATCH`·`DELETE`·
`OPTIONS`·`TRACE`(대문자 그대로) 또는 route-decl 전용 `ANY`이고, limitation 스코프의 `methods`는 `ANY` 없이 같은
동사만 쓴다. 소비자는 집합 밖 값을 고치지 않고 문서 전체를 입력 오류로 거부하므로, 서버 추출·클라이언트 추출·스코프
검사가 사본을 따로 두지 않고 이 한 벌을 쓴다. 다른 pythograph 모듈을 import하지 않아 어느 계층에서 써도 순환이
생기지 않는다.
"""

from __future__ import annotations

#: 계약의 HTTP 동사다. `route-call`의 `method`와 스코프 `methods`에는 이 값만 올 수 있다.
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")

#: `route-decl`의 `method`에 올 수 있는 값이다. `ANY`(모든 method를 받는 선언)는 route-decl 전용이다.
ROUTE_DECL_METHODS = frozenset((*HTTP_METHODS, "ANY"))
