"""호출 지점 하나를 간선 대상과 해석 상태로 바꾼다.

상태:
- `resolved`: 프로젝트 대상에 모두 이었다.
- `partial`: 정적으로 찾은 대상은 있지만 프로젝트 하위 클래스가 재정의한 메서드일 수 있다(또는 하위 클래스에만
  정의돼 있다). `direct`·`bound` 모드에서는 잇지 못한 호출로 세고, `candidates` 모드는 재정의 후보 간선으로 잇는다.
- `external`: 프로젝트 밖(표준 라이브러리·설치 패키지·프레임워크 멤버)으로 확정했다. 타입을 모르는 수신자의
  메서드 호출도, 그 이름을 정의하거나 대입하는 프로젝트 클래스·모듈·속성 쓰기가 하나도 없으면 프로젝트 코드로 갈 수
  없으므로 외부다(`NameFilter`).
- `unresolved`: 대상을 잇지 못했다(이유별로 센다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.graph.framework import framework_closure
from pythograph.graph.index import Definition
from pythograph.graph.scope import Resolver
from pythograph.graph.values import (
    AttributeValue,
    ClassValue,
    ExternalResult,
    ExternalValue,
    FrameworkMethodValue,
    FunctionValue,
    InstanceValue,
    MethodValue,
    UnknownValue,
    Value,
)

#: 해석기 내부 이유 → 출력 이유다.
REASONS = {
    "lambda-parameter": "parameter",
    "rebound-local": "local-value",
    "loop-variable": "local-value",
    "context-manager": "local-value",
    "exception": "local-value",
    "unpacked": "local-value",
    "opaque-local": "local-value",
    "comprehension-binding": "local-value",
    "comprehension-variable": "local-value",
    "match-capture": "local-value",
    "recursive-binding": "local-value",
    "call-result": "dynamic-callee",
    "dynamic-expression": "dynamic-callee",
    "depth-limit": "dynamic-callee",
    "dynamic-super": "dynamic-callee",
    "instance-attribute": "dynamic-attribute",
    "function-attribute": "dynamic-attribute",
}


def reason_name(reason: str) -> str:
    """해석기 이유를 출력 이유로 묶는다.

    Args:
        reason: 해석기 이유.

    Returns:
        출력 이유.
    """
    return REASONS.get(reason, reason)


@dataclass(frozen=True)
class Target:
    """간선 대상.

    Attributes:
        node_id: 도착 정점 id.
        kind: 간선 종류.
    """

    node_id: str
    kind: str


@dataclass
class CallOutcome:
    """호출 지점 하나의 해석 결과.

    Attributes:
        status: `resolved`·`partial`·`external`·`unresolved`.
        targets: direct 간선 대상.
        candidates: candidate 간선 대상.
        reason: `partial`·`unresolved`의 이유.
    """

    status: str
    targets: list[Target] = field(default_factory=list)
    candidates: list[Target] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class InheritedRequest:
    """물려받은 멤버 정점 요청(`<클래스 id>.<이름>`).

    Attributes:
        receiver: 멤버를 물려받은 클래스.
        name: 멤버 이름.
    """

    receiver: Definition
    name: str

    @property
    def id(self) -> str:
        """정점 id를 돌려준다.

        Returns:
            `<클래스 id>.<이름>`.
        """
        return f"{self.receiver.id}.{self.name}"


class NameFilter:
    """프로젝트 코드가 정의하거나 대입하는 속성 이름 집합(타입 모르는 수신자 판정용)."""

    def __init__(self, names: set[str], disabled: bool) -> None:
        """필터를 만든다.

        Args:
            names: 프로젝트 메서드·클래스 속성·중첩 클래스·모듈 수준 정의·속성 쓰기·리터럴 setattr 이름.
            disabled: 프로젝트 클래스가 `__getattr__`·`__getattribute__`를 정의해 어떤 이름이든 가능하면 True.
        """
        self.names = names
        self.disabled = disabled

    def possible(self, name: str) -> bool:
        """그 이름의 속성 호출이 프로젝트 코드에 닿을 수 있는지 돌려준다.

        Args:
            name: 속성 이름.

        Returns:
            닿을 수 있으면 True.
        """
        return self.disabled or name in self.names


class CallResolver:
    """호출 지점을 해석하고 물려받은 멤버 정점 요청을 모은다."""

    def __init__(self, resolver: Resolver, names: NameFilter) -> None:
        """해석기를 만든다.

        Args:
            resolver: 값 해석기.
            names: 속성 이름 필터.
        """
        self.resolver = resolver
        self.names = names
        self.requests: dict[str, InheritedRequest] = {}

    def resolve(self, scope: Definition, call: ast.Call, shadowed: bool) -> CallOutcome:
        """호출 지점 하나를 해석한다.

        Args:
            scope: 호출을 담은 범위.
            call: 호출 식.
            shadowed: 피호출 식의 뿌리 이름이 람다 매개변수·컴프리헨션 변수인지.

        Returns:
            해석 결과.
        """
        func = call.func
        if isinstance(func, ast.Attribute):
            if shadowed:
                return self._untyped_attribute(func.attr, "untyped-receiver")
            receiver = self.resolver.value(scope, func.value)
            if isinstance(receiver, (UnknownValue, ExternalResult)):
                return self._untyped_attribute(func.attr, "untyped-receiver")
            member = self.resolver.member_value(scope, receiver, func.attr, 0)
            return self._attribute_callee(receiver, member, func.attr)
        if shadowed:
            return CallOutcome("unresolved", reason="parameter")
        return self.callee(self.resolver.value(scope, func))

    def _untyped_attribute(self, name: str, reason: str) -> CallOutcome:
        """타입 모르는 수신자의 메서드 호출이다. 프로젝트가 그 이름을 쓰지 않으면 외부로 확정한다.

        Args:
            name: 속성 이름.
            reason: 잇지 못한 이유.

        Returns:
            해석 결과.
        """
        if self.names.possible(name):
            return CallOutcome("unresolved", reason=reason)
        return CallOutcome("external")

    def _attribute_callee(self, receiver: Value, member: Value, name: str) -> CallOutcome:
        """속성 피호출 식을 해석한다. 모르는 멤버는 하위 클래스 정의와 이름 필터로 판정한다.

        Args:
            receiver: 수신자 값.
            member: 멤버 값.
            name: 속성 이름.

        Returns:
            해석 결과.
        """
        if not isinstance(member, UnknownValue):
            return self.callee(member)
        if isinstance(receiver, (InstanceValue, ClassValue)) and not receiver.exact:
            candidates = self._subclass_definitions(receiver.definition, name)
            if candidates:
                return CallOutcome("partial", candidates=candidates, reason="subclass-method")
        if member.reason in ("instance-attribute", "function-attribute", "untyped-receiver"):
            return self._untyped_attribute(name, reason_name(member.reason))
        return CallOutcome("unresolved", reason=reason_name(member.reason))

    def callee(self, value: Value, depth: int = 0) -> CallOutcome:
        """피호출 값을 해석한다.

        Args:
            value: 피호출 값.
            depth: 클래스 속성을 따라간 깊이.

        Returns:
            해석 결과.
        """
        if isinstance(value, FunctionValue):
            return CallOutcome("resolved", [Target(value.definition.id, "call")])
        if isinstance(value, ClassValue):
            return self.instantiate(value)
        if isinstance(value, MethodValue):
            return self.method(value, "call")
        if isinstance(value, FrameworkMethodValue) and value.after is not None:
            return self._super_framework(value)
        if isinstance(value, FrameworkMethodValue):
            return CallOutcome("resolved", [Target(self.framework_member(value.receiver, value.name), "call")])
        if isinstance(value, AttributeValue) and depth < 8:
            return self.callee(self.resolver.attribute_target(value, 0), depth + 1)
        if isinstance(value, ExternalValue):
            return CallOutcome("external")
        if isinstance(value, UnknownValue):
            return CallOutcome("unresolved", reason=reason_name(value.reason))
        return CallOutcome("unresolved", reason="dynamic-callee")

    def _super_framework(self, value: FrameworkMethodValue) -> CallOutcome:
        """`super()`로 닿은 프레임워크 구현이 부르는 프로젝트 훅을 호출한 정점에서 직접 잇는다.

        Args:
            value: super 조회로 찾은 프레임워크 메서드.

        Returns:
            해석 결과(훅이 없으면 외부).
        """
        closure = framework_closure(self.resolver, value.receiver, [value.name], value.after)
        targets = [
            Target(self.exact_member(value.receiver, hook.definition), "framework")
            for hook in closure.hooks
            if hook.definition
        ]
        targets.extend(Target(member.owner.key, "attribute") for member in closure.attributes if member.owner)
        return CallOutcome("resolved", targets) if targets else CallOutcome("external")

    def instantiate(self, value: ClassValue) -> CallOutcome:
        """클래스 호출(인스턴스 생성)을 해석한다: 프로젝트 `__init__`, 프레임워크 `__init__`, 그 밖은 클래스 정점.

        Args:
            value: 클래스 값.

        Returns:
            해석 결과.
        """
        definition = value.definition
        member = self.resolver.linearizer.lookup(definition, "__init__")
        if member.kind == "method" and member.definition is not None:
            method = MethodValue(definition, member.definition, value.exact)
            return self.method(method, "new")
        if member.kind == "framework-method":
            return CallOutcome("resolved", [Target(self.framework_member(definition, "__init__"), "new")])
        return CallOutcome("resolved", [Target(definition.id, "new")])

    def method(self, value: MethodValue, kind: str) -> CallOutcome:
        """묶인 메서드 호출을 해석한다. 정확한 수신자는 그 클래스의 멤버 정점, 아니면 정의와 재정의 후보다.

        Args:
            value: 메서드 값.
            kind: 간선 종류.

        Returns:
            해석 결과.
        """
        if value.exact:
            return CallOutcome("resolved", [Target(self.exact_member(value.receiver, value.member), kind)])
        targets = [Target(value.member.id, kind)]
        if value.via_super:
            return CallOutcome("resolved", targets)
        name = getattr(value.member.node, "name", "")
        candidates = self.overrides(value.receiver, name, value.member, kind)
        return CallOutcome("partial" if candidates else "resolved", targets, candidates, "overridden-method")

    def exact_member(self, receiver: Definition, member: Definition) -> str:
        """정확한 수신자 클래스의 멤버 정점 id다. 자기 정의면 정의, 물려받았으면 상속 멤버 정점이다.

        이름으로 찾은 멤버가 이 정의가 아니면(`super()` 사슬로 닿은 정의) 정의 자체다.

        Args:
            receiver: 수신자 클래스.
            member: MRO로 찾은 메서드 정의.

        Returns:
            정점 id.
        """
        if member.parent is receiver:
            return member.id
        name = getattr(member.node, "name", "")
        found = self.resolver.linearizer.lookup(receiver, name)
        if found.kind != "method" or found.definition is not member:
            return member.id
        return self._request(receiver, name)

    def framework_member(self, receiver: Definition, name: str) -> str:
        """프레임워크가 구현한 멤버의 상속 멤버 정점 id다.

        Args:
            receiver: 수신자 클래스.
            name: 멤버 이름.

        Returns:
            정점 id.
        """
        return self._request(receiver, name)

    def _request(self, receiver: Definition, name: str) -> str:
        """상속 멤버 정점을 요청한다.

        Args:
            receiver: 수신자 클래스.
            name: 멤버 이름.

        Returns:
            정점 id.
        """
        request = InheritedRequest(receiver, name)
        self.requests.setdefault(request.id, request)
        return request.id

    def overrides(self, receiver: Definition, name: str, member: Definition, kind: str) -> list[Target]:
        """프로젝트 하위 클래스가 재정의한 같은 이름의 메서드다(candidate 간선).

        Args:
            receiver: 수신자의 정적 클래스.
            name: 메서드 이름.
            member: 정적으로 찾은 정의.
            kind: 간선 종류.

        Returns:
            대상 목록(id 순, 중복 없음).
        """
        found: dict[str, Target] = {}
        for subclass in self.resolver.linearizer.subclasses(receiver):
            result = self.resolver.linearizer.lookup(subclass, name)
            if result.kind == "method" and result.definition is not None and result.definition is not member:
                found.setdefault(result.definition.id, Target(result.definition.id, kind))
        return [found[key] for key in sorted(found)]

    def _subclass_definitions(self, receiver: Definition, name: str) -> list[Target]:
        """수신자 클래스에는 없고 하위 클래스에만 있는 메서드 정의다.

        Args:
            receiver: 수신자의 정적 클래스.
            name: 메서드 이름.

        Returns:
            대상 목록.
        """
        found: dict[str, Target] = {}
        for subclass in self.resolver.linearizer.subclasses(receiver):
            result = self.resolver.linearizer.lookup(subclass, name)
            if result.kind == "method" and result.definition is not None:
                found.setdefault(result.definition.id, Target(result.definition.id, "call"))
        return [found[key] for key in sorted(found)]
