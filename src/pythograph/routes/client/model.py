"""클라이언트 호출 추출의 내부 데이터 모델.

추출기(`extract.py`)는 이 모델로 호출과 공백을 내고, 문서 조립기(`pythograph.exchange.client_document`)가 bridge-facts
v1 `route-call` 문서로 바꾼다. 서버 쪽 `routes.model`과 같은 이유로 추출 규칙과 직렬화를 나눈다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pythograph.routes.client.compose import ComposedUrl
from pythograph.routes.model import Gap, Location


@dataclass(frozen=True)
class RouteCall:
    """route-call 사실 하나의 내용.

    Attributes:
        method: 확정한 동사. 모르면 None(`methodDynamic`).
        url: 경로 필드.
        location: 호출식이 시작하는 위치.
        usr: 호출을 감싸는 선언의 pythograph id(`qualifiedName`과 같다).
        service: 래퍼 선언이 정한 service(없으면 None — 문서 값을 쓴다).
        test_source: 테스트 소스의 호출인지.
    """

    method: str | None
    url: ComposedUrl
    location: Location
    usr: str
    service: str | None = None
    test_source: bool = False


@dataclass
class ClientExtraction:
    """클라이언트 추출 결과."""

    calls: list[RouteCall] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def add_gap(self, prefix: str, message: str) -> None:
        """공백을 하나 더한다(스코프 없음 — 호출 측 한계의 요청 상한을 증명하지 않는다).

        Args:
            prefix: limitation 접두사.
            message: 문장 틀(`{count}`는 개수로 채운다).
        """
        self.gaps.append(Gap(prefix, message))
