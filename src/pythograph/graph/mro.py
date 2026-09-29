"""프로젝트 클래스의 C3 메서드 결정 순서(MRO)와 멤버 조회, 하위 클래스 색인.

MRO 항목은 네 종류다: 프로젝트 클래스, 알려진 프레임워크 클래스(`framework_table`의 선형화를 그대로 쓴다),
표에 없는 외부 클래스(멤버를 모른다), 해석하지 못한 기반(프로젝트 클래스일 수도 있다). 파이썬 `type.mro()`와 같은
C3 병합이며, 병합이 실패하면(일관성 없는 계층) 왼쪽 우선 깊이 우선 근사로 바꾸고 `approximated`에 남긴다.

멤버 조회는 MRO를 앞에서부터 본다. 표에 없는 외부 클래스는 어떤 이름을 정의하는지 모르므로 건너뛰되 기억해 두고,
뒤에서 프로젝트·프레임워크 정의를 찾으면 그것을 쓴다(외부 클래스가 같은 이름을 가리는 경우는 근사다). 끝까지
못 찾으면 외부 클래스를 지났으면 외부 멤버, 해석하지 못한 기반을 지났으면 모름, 아니면 없음이다.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

from pythograph.graph.framework_table import FRAMEWORK_ALIASES, FRAMEWORK_CLASSES
from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.values import ClassValue, ExternalValue, Value

#: 선형화에서 빼는 외부 기반 클래스다(멤버가 프로젝트 호출에 영향을 주지 않는다).
IGNORED_BASES = frozenset(
    {"builtins.object", "object", "typing.Generic", "typing.Protocol", "abc.ABC", "typing_extensions.Protocol"}
)

#: 상속 사슬을 따라가는 최대 깊이다.
MAX_CLASS_DEPTH = 64


@dataclass(frozen=True)
class MroEntry:
    """MRO 항목.

    Attributes:
        kind: `project`·`framework`·`external`·`unknown`.
        key: 비교 키(프로젝트 id, 프레임워크·외부 점 경로, 모름은 고유 표식).
        definition: 프로젝트 클래스 정의(프로젝트 항목만).
    """

    kind: str
    key: str
    definition: Definition | None = None


@dataclass(frozen=True)
class Member:
    """MRO 멤버 조회 결과.

    Attributes:
        kind: `method`·`class`·`attribute`·`framework-method`·`framework-attribute`·`external`·`unknown`·`missing`.
        name: 멤버 이름.
        owner: 멤버를 정의한 MRO 항목(찾았을 때).
        definition: 프로젝트 메서드·중첩 클래스 정의.
        expr: 프로젝트 클래스 속성 값 식.
    """

    kind: str
    name: str
    owner: MroEntry | None = None
    definition: Definition | None = None
    expr: ast.expr | None = None


#: 기반 클래스 식을 해석하는 함수 타입이다(클래스 정의, 식) → 값.
BaseResolver = Callable[[Definition, ast.expr], Value]


def framework_key(dotted: str) -> str | None:
    """외부 점 경로가 표의 프레임워크 클래스면 정의 경로를 돌려준다.

    Args:
        dotted: 외부 점 경로(재수출 표기 포함).

    Returns:
        정의 경로 또는 None.
    """
    canonical = FRAMEWORK_ALIASES.get(dotted, dotted)
    return canonical if canonical in FRAMEWORK_CLASSES else None


class Linearizer:
    """프로젝트 클래스의 MRO를 계산하고 멤버를 찾는다."""

    def __init__(self, index: DefinitionIndex, resolve_base: BaseResolver) -> None:
        """계산기를 만든다.

        Args:
            index: 선언 색인.
            resolve_base: 기반 클래스 식 해석 함수.
        """
        self.index = index
        self.resolve_base = resolve_base
        self._mro: dict[str, list[MroEntry]] = {}
        self._active: set[str] = set()
        self.approximated: set[str] = set()
        self._subclasses: dict[str, list[Definition]] | None = None

    def bases(self, definition: Definition) -> list[MroEntry]:
        """클래스의 직접 기반 항목을 돌려준다(무시하는 기반은 뺀다).

        Args:
            definition: 클래스 정의.

        Returns:
            기반 항목 목록.
        """
        node = definition.node
        assert isinstance(node, ast.ClassDef)
        entries: list[MroEntry] = []
        for position, base in enumerate(node.bases):
            expr = base.value if isinstance(base, ast.Subscript) else base
            entry = self._base_entry(definition, expr, position)
            if entry is not None:
                entries.append(entry)
        return entries

    def _base_entry(self, definition: Definition, expr: ast.expr, position: int) -> MroEntry | None:
        """기반 클래스 식 하나를 항목으로 바꾼다.

        Args:
            definition: 클래스 정의.
            expr: 기반 식.
            position: 기반 순번(모름 항목의 고유 키용).

        Returns:
            항목, 무시하는 기반이면 None.
        """
        value = self.resolve_base(definition, expr)
        if isinstance(value, ClassValue):
            return MroEntry("project", value.definition.id, value.definition)
        if isinstance(value, ExternalValue):
            if value.dotted in IGNORED_BASES:
                return None
            key = framework_key(value.dotted)
            return MroEntry("framework", key) if key is not None else MroEntry("external", value.dotted)
        return MroEntry("unknown", f"{definition.id}#base{position}")

    def mro(self, definition: Definition) -> list[MroEntry]:
        """클래스의 MRO를 돌려준다(캐시).

        Args:
            definition: 클래스 정의.

        Returns:
            자신부터 시작하는 항목 목록.
        """
        cached = self._mro.get(definition.id)
        if cached is not None:
            return cached
        if definition.id in self._active or len(self._active) > MAX_CLASS_DEPTH:
            self.approximated.add(definition.id)
            return [MroEntry("project", definition.id, definition), MroEntry("unknown", f"{definition.id}#cycle")]
        self._active.add(definition.id)
        try:
            result = self._linearize(definition)
        finally:
            self._active.discard(definition.id)
        self._mro[definition.id] = result
        return result

    def _linearize(self, definition: Definition) -> list[MroEntry]:
        """C3 선형화를 계산한다. 실패하면 근사한다.

        Args:
            definition: 클래스 정의.

        Returns:
            MRO 항목 목록.
        """
        head = MroEntry("project", definition.id, definition)
        bases = self.bases(definition)
        sequences = [self._entry_mro(base) for base in bases] + [list(bases)]
        merged = c3_merge(sequences)
        if merged is None:
            self.approximated.add(definition.id)
            merged = _keep_last([entry for sequence in sequences[:-1] for entry in sequence])
        return [head, *merged]

    def _entry_mro(self, entry: MroEntry) -> list[MroEntry]:
        """기반 항목 하나의 선형화를 돌려준다.

        Args:
            entry: 기반 항목.

        Returns:
            항목 목록.
        """
        if entry.kind == "project" and entry.definition is not None:
            return list(self.mro(entry.definition))
        if entry.kind == "framework":
            return [MroEntry("framework", key) for key in FRAMEWORK_CLASSES[entry.key]["mro"]]
        return [entry]

    def lookup(self, definition: Definition, name: str, after: str | None = None) -> Member:
        """MRO에서 멤버를 찾는다.

        Args:
            definition: 수신자 클래스 정의.
            name: 멤버 이름.
            after: 이 키의 항목 다음부터 찾는다(`super()`).

        Returns:
            조회 결과.
        """
        entries = self.mro(definition)
        if after is not None:
            keys = [entry.key for entry in entries]
            entries = entries[keys.index(after) + 1 :] if after in keys else []
        passed_external = passed_unknown = False
        for entry in entries:
            found = self._entry_member(entry, name)
            if found is not None:
                return found
            passed_external = passed_external or entry.kind == "external"
            passed_unknown = passed_unknown or entry.kind == "unknown"
        if passed_unknown:
            return Member("unknown", name)
        return Member("external", name) if passed_external else Member("missing", name)

    def _entry_member(self, entry: MroEntry, name: str) -> Member | None:
        """MRO 항목 하나에서 멤버를 찾는다.

        Args:
            entry: 항목.
            name: 이름.

        Returns:
            찾으면 결과, 아니면 None.
        """
        if entry.kind == "project" and entry.definition is not None:
            members = self.index.class_members(entry.definition)
            if name in members.methods:
                return Member("method", name, entry, members.methods[name])
            if name in members.classes:
                return Member("class", name, entry, members.classes[name])
            if name in members.attributes:
                return Member("attribute", name, entry, expr=members.attributes[name])
            return None
        if entry.kind == "framework":
            table = FRAMEWORK_CLASSES[entry.key]
            if name in table["methods"]:
                return Member("framework-method", name, entry)
            if name in table["attributes"]:
                return Member("framework-attribute", name, entry)
        return None

    def subclasses(self, definition: Definition) -> list[Definition]:
        """프로젝트 하위 클래스(간접 포함, 자신 제외)를 id 순으로 돌려준다.

        Args:
            definition: 클래스 정의.

        Returns:
            하위 클래스 목록.
        """
        if self._subclasses is None:
            table: dict[str, list[Definition]] = {}
            for candidate in self.index.classes():
                for entry in self.mro(candidate)[1:]:
                    if entry.kind == "project":
                        table.setdefault(entry.key, []).append(candidate)
            self._subclasses = table
        return self._subclasses.get(definition.id, [])


def c3_merge(sequences: list[list[MroEntry]]) -> list[MroEntry] | None:
    """C3 병합이다.

    Args:
        sequences: 기반들의 선형화와 기반 목록.

    Returns:
        병합 결과, 일관성이 없으면 None.
    """
    pending = [list(sequence) for sequence in sequences if sequence]
    result: list[MroEntry] = []
    while pending:
        head = _next_head(pending)
        if head is None:
            return None
        result.append(head)
        pending = [[entry for entry in sequence if entry.key != head.key] for sequence in pending]
        pending = [sequence for sequence in pending if sequence]
    return result


def _next_head(pending: list[list[MroEntry]]) -> MroEntry | None:
    """어느 목록의 꼬리에도 없는 첫 머리를 고른다.

    Args:
        pending: 남은 목록들.

    Returns:
        머리 항목, 없으면 None.
    """
    for sequence in pending:
        head = sequence[0]
        if not any(head.key in (entry.key for entry in other[1:]) for other in pending):
            return head
    return None


def _keep_last(order: list[MroEntry]) -> list[MroEntry]:
    """같은 항목이 여러 번 나오면 마지막 위치만 남긴다.

    Args:
        order: 펼친 목록.

    Returns:
        정리한 목록.
    """
    keys = [entry.key for entry in order]
    return [entry for position, entry in enumerate(order) if entry.key not in keys[position + 1 :]]
