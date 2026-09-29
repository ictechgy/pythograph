"""persistence 추출의 내부 데이터 모델.

ORM 추출기(Django·SQLAlchemy·Flask-SQLAlchemy)와 SQL 텍스트 추출기는 이 모델로 관계 사용과 공백을 내고,
문서 조립기(`pythograph.exchange.persistence`)가 bridge-facts v1 `relation-use` JSON으로 바꾼다.
관계 이름은 세그먼트 튜플(`RelationName`)로 다루고 교환 형식의 escape는 조립 직전에 한 번만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pythograph.persistence.sql import escape_name
from pythograph.routes.model import Gap, Location


@dataclass(frozen=True)
class RelationName:
    """관계 이름. `segments`는 escape하지 않은 이름 조각(한정자 포함)이다.

    `("blog_article",)`는 비한정, `("audit", "events")`는 `audit.events`다. 이름 자체에 든 `.`는 조각 안에
    남고 채널로 바꿀 때 `%2E`가 된다.
    """

    segments: tuple[str, ...]

    @property
    def channel(self) -> str:
        """교환 형식의 channel 문자열을 돌려준다.

        Returns:
            조각마다 escape해 `.`로 이은 이름.
        """
        return ".".join(escape_name(segment) for segment in self.segments)


@dataclass(frozen=True)
class RelationUse:
    """relation-use 사실 하나의 내용.

    Attributes:
        channel: 정적이면 escape한 관계 이름, dynamic이면 원문 표현.
        column: 컬럼 이름(관계 수준 사실이면 None). dynamic 사실에는 싣지 않는다.
        dynamic: 이름을 확정하지 못했는지.
        location: 근거 위치.
        symbol: 감싸는 선언의 pythograph 심볼 id(모듈 수준이면 None).
    """

    channel: str
    column: str | None
    dynamic: bool
    location: Location
    symbol: str | None


@dataclass
class PersistenceExtraction:
    """persistence 추출 결과."""

    uses: list[RelationUse] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def add_gap(self, prefix: str, message: str) -> None:
        """공백을 하나 더한다. persistence 한계에는 스코프가 없다.

        Args:
            prefix: limitation 접두사(콜론 포함).
            message: `{count}`를 담을 수 있는 문장 틀.
        """
        self.gaps.append(Gap(prefix, message))

    def add_relation(self, name: RelationName, location: Location, symbol: str | None) -> None:
        """관계 수준 정적 사실을 더한다.

        Args:
            name: 관계 이름.
            location: 위치.
            symbol: 감싸는 선언 id.
        """
        self.uses.append(RelationUse(name.channel, None, False, location, symbol))

    def add_column(self, name: RelationName, column: str, location: Location, symbol: str | None) -> None:
        """(관계, 컬럼) 정적 사실을 더한다. 계약상 컬럼 사실은 관계 사실을 함축하지 않으므로 따로 낸다.

        Args:
            name: 관계 이름.
            column: 컬럼 이름.
            location: 위치.
            symbol: 감싸는 선언 id.
        """
        self.uses.append(RelationUse(name.channel, column, False, location, symbol))

    def add_dynamic(self, expression: str, location: Location, symbol: str | None, reason: str) -> None:
        """이름을 확정하지 못한 사용을 dynamic 사실과 이유별 한계로 남긴다.

        Args:
            expression: 원문 표현(비면 `<dynamic>`).
            location: 위치.
            symbol: 감싸는 선언 id.
            reason: `dynamic-relation-names:` 뒤에 붙일 이유 문장 틀.
        """
        self.uses.append(RelationUse(expression or "<dynamic>", None, True, location, symbol))
        self.add_gap("dynamic-relation-names:", reason)
