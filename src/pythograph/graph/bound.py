"""`bound` 근거 등급: 수신자 흐름이 닫힌 메서드 호출을 관찰된 구현마다 잇는다.

후보 호출 지점(`calls.BoundSite` — 타입 모르는 수신자, 인스턴스 속성 수신자, 재정의 후보가 있는 수신자)마다:

1. 수신자 식의 흐름(`flow.FlowAnalyzer`)이 열려 있으면 잇지 않는다(열린 이유를 센다).
2. 흐름의 모든 클래스 K에서 메서드 이름을 MRO로 찾는다. 프로젝트 메서드(정의면 그 정의, 물려받았으면 상속 멤버 정점)나
   프레임워크 구현(상속 멤버 정점)이어야 한다. property·클래스 속성·못 찾은 이름, 인스턴스 속성 쓰기로 가려질 수 있는
   메서드(`obj.save = …`, 리터럴 `setattr`), 계산된 이름의 쓰기 대상 클래스, `__getattribute__` 훅이 있으면 그 호출
   전체를 잇지 않는다.
3. 프로젝트 대상이 하나 이상이면 `bound` 간선이고, 그 호출은 `bound`·`candidates` 모드에서 미해석이 아니다.

`self`·`cls` 수신자는 잇지 않는다: 프레임워크·ORM·DI 컨테이너가 하위 클래스 인스턴스를 만들 수 있다(재정의 후보는
`candidate` 간선이 맡는다). 테스트 소스의 호출 지점도 잇지 않는다(별도 프로그램이다).
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field

from pythograph.graph.calls import BoundSite, CallResolver, Target
from pythograph.graph.exposure import ProgramFacts
from pythograph.graph.flow import FlowAnalyzer
from pythograph.graph.framework_table import FRAMEWORK_CLASSES
from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.source.project import is_test_path


@dataclass
class BoundSummary:
    """`bound` 결과 집계.

    Attributes:
        linked: 원래 미해석 이유 → `bound`로 이은 호출 수.
        open: 열린 이유 → 잇지 못한 후보 호출 수.
        unknown_writes: 수신자 흐름이 열려 대상을 모르는 계산된 이름의 쓰기 수(모델링하지 않은 틈).
    """

    linked: Counter[str] = field(default_factory=Counter)
    open: Counter[str] = field(default_factory=Counter)
    unknown_writes: int = 0


class BoundDispatcher:
    """후보 호출 지점의 `bound` 대상을 구한다."""

    def __init__(self, index: DefinitionIndex, calls: CallResolver, facts: ProgramFacts, flows: FlowAnalyzer) -> None:
        """디스패처를 만든다.

        Args:
            index: 선언 색인.
            calls: 호출 해석기(상속 멤버 정점 요청).
            facts: 전체 프로그램 사실.
            flows: 값 흐름 질의기.
        """
        self.index = index
        self.calls = calls
        self.facts = facts
        self.flows = flows
        self.linearizer = calls.resolver.linearizer

    def resolve(self, site: BoundSite) -> tuple[list[Target], str | None]:
        """호출 지점 하나를 잇는다.

        Args:
            site: 후보 호출 지점.

        Returns:
            (`bound` 대상, 잇지 못했으면 이유).
        """
        if is_test_path(site.scope.path):
            return [], "test-source"
        if site.lambda_scoped:
            return [], "lambda-scope"
        func = site.call.func
        assert isinstance(func, ast.Attribute)
        scope = self.flows.index.definitions.get(site.scope.id, site.scope)
        flow = self.flows.receiver_flow(scope, func.value)
        if flow.open is not None:
            return [], flow.open
        if not flow.classes:
            return [], "no-instance-flow"
        return self._targets(sorted(flow.classes), func.attr)

    def _targets(self, class_ids: list[str], name: str) -> tuple[list[Target], str | None]:
        """흐름의 클래스마다 메서드 대상을 찾는다. 하나라도 막히면 잇지 않는다.

        Args:
            class_ids: 흐름의 클래스 id(정렬).
            name: 메서드 이름.

        Returns:
            (대상, 이유).
        """
        targets: list[Target] = []
        for class_id in class_ids:
            definition = self.index.definitions.get(class_id)
            if definition is None:
                return [], "excluded-source"
            target, reason = self._method_target(definition, name)
            if reason is not None:
                return [], reason
            if target is not None:
                targets.append(target)
        return (targets, None) if targets else ([], "no-project-method")

    def _method_target(self, definition: Definition, name: str) -> tuple[Target | None, str | None]:
        """정확한 클래스 K의 메서드 대상이다.

        Args:
            definition: 클래스 K.
            name: 메서드 이름.

        Returns:
            (대상, 막혔으면 이유).
        """
        reason = self._shadowed(definition, name)
        if reason is not None:
            return None, reason
        member = self.linearizer.lookup(definition, name)
        if member.kind == "method" and member.definition is not None:
            if self._is_property(member.definition, name):
                return None, "property"
            return Target(self.calls.exact_member(definition, member.definition), "call"), None
        if member.kind == "framework-method" and member.owner is not None:
            if FRAMEWORK_CLASSES[member.owner.key]["methods"][name].get("property"):
                return None, "property"
            return Target(self.calls.framework_member(definition, name), "call"), None
        return None, "unresolved-method"

    def _shadowed(self, definition: Definition, name: str) -> str | None:
        """인스턴스 속성이 메서드를 가릴 수 있는지 본다.

        Args:
            definition: 클래스 K.
            name: 메서드 이름.

        Returns:
            가릴 수 있으면 이유, 아니면 None.
        """
        for entry in self.linearizer.mro(definition):
            if (
                entry.definition is not None
                and "__getattribute__" in self.index.class_members(entry.definition).methods
            ):
                return "attribute-hook"
        return self.flows.method_blocked(definition.id, name)

    def _is_property(self, method: Definition, name: str) -> bool:
        """메서드가 property·cached_property인지 본다(호출하면 반환 값을 부른다).

        Args:
            method: 메서드 정의.
            name: 이름.

        Returns:
            그렇다면 True.
        """
        if method.parent is None:
            return False
        decorators = self.index.class_members(method.parent).decorators.get(name, set())
        return bool(decorators & {"property", "cached_property"})
