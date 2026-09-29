"""정적 값 해석의 결과 타입.

식 하나가 무엇을 가리키는지를 실행 없이 나타낸다. 모르면 `UnknownValue(이유)`이고 추측하지 않는다.
`exact`는 수신자 클래스가 정확히 그 클래스인지(생성자 호출·프레임워크가 만든 인스턴스)다. `self`·주석으로만 아는
값은 하위 클래스일 수 있어 `exact=False`다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.graph.index import Definition


@dataclass(frozen=True)
class FunctionValue:
    """프로젝트 함수(또는 클래스를 거치지 않은 메서드 정의)."""

    definition: Definition


@dataclass(frozen=True)
class ClassValue:
    """프로젝트 클래스 객체. `exact`가 거짓이면 하위 클래스일 수 있다(`cls`)."""

    definition: Definition
    exact: bool = True


@dataclass(frozen=True)
class InstanceValue:
    """프로젝트 클래스의 인스턴스."""

    definition: Definition
    exact: bool


@dataclass(frozen=True)
class MethodValue:
    """수신자에 묶인 프로젝트 메서드.

    Attributes:
        receiver: 수신자의 정적 클래스.
        member: MRO로 찾은 메서드 정의.
        exact: 수신자 클래스가 정확한지.
        via_super: `super()`로 찾았는지(하위 클래스 재정의 후보를 만들지 않는다).
        is_property: property·cached_property인지(읽기가 호출이다).
    """

    receiver: Definition
    member: Definition
    exact: bool
    via_super: bool = False
    is_property: bool = False


@dataclass(frozen=True)
class FrameworkMethodValue:
    """프로젝트 클래스가 물려받은 알려진 프레임워크 메서드(`framework_table`). property면 읽기가 호출이다.

    `after`는 `super()`로 찾았을 때 호출한 클래스 id다(그 다음 MRO 항목부터 찾았다).
    """

    receiver: Definition
    name: str
    exact: bool
    is_property: bool = False
    after: str | None = None


@dataclass(frozen=True)
class AttributeValue:
    """프로젝트 클래스 본문의 대입 속성. 읽으면 그 클래스 정점에 기댄다."""

    owner: Definition
    name: str
    expr: ast.expr


@dataclass(frozen=True)
class ModuleValue:
    """프로젝트 모듈."""

    path: str
    dotted: str


@dataclass(frozen=True)
class ExternalValue:
    """프로젝트 밖의 이름(점 경로)."""

    dotted: str


@dataclass(frozen=True)
class ExternalResult:
    """외부 호출의 결과. 타입은 모르지만 프로젝트 정의에서 온 값이라는 증거도 없다."""


@dataclass(frozen=True)
class SuperValue:
    """`super()` 프록시. `owner` 다음부터 `receiver` 클래스의 MRO를 찾는다."""

    owner: Definition
    receiver: Definition


@dataclass(frozen=True)
class UnknownValue:
    """해석하지 못한 값과 그 이유."""

    reason: str


#: 해석 결과 타입이다.
Value = (
    FunctionValue
    | ClassValue
    | InstanceValue
    | MethodValue
    | FrameworkMethodValue
    | AttributeValue
    | ModuleValue
    | ExternalValue
    | ExternalResult
    | SuperValue
    | UnknownValue
)
