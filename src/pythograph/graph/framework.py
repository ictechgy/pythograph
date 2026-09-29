"""프레임워크 디스패치: 클래스 기반 뷰의 핸들러 정점과 프레임워크 구현이 부르는 프로젝트 훅.

route-decl의 클래스 핸들러 id는 URL에 등록한 클래스 기준(`<파일>#<클래스>.<메서드>`)이다. 그래서 등록될 수 있는
뷰 클래스(Django `View`, DRF `APIView`·viewset, Flask `View`·`MethodView` 계열)의 핸들러 이름마다 정점을 둔다 —
클래스가 정의했으면 그 정의, 물려받았으면 상속 멤버 정점이다.

프레임워크 구현(`framework_table`, 설치본 소스를 ast로 읽은 표)은 `self.X`로 훅을 부른다. 그 이름을 수신자 클래스의
MRO로 풀어 프로젝트 정의가 나오면 훅 간선, 프로젝트 클래스 속성이 나오면 속성 간선이다. 프레임워크 정의가 나오면
그 메서드가 읽는 이름으로 계속 따라간다(`super().X`는 정의한 클래스 다음부터 찾는다). 수신자는 프레임워크가 만든
인스턴스라 정확한 클래스다.

- 디스패치 경로: `as_view()`가 만든 뷰 함수(Django `setup`·`dispatch`, DRF `initial`·권한 검사, 인스턴스 생성의
  `__init__`, Flask `dispatch_request`)가 부르는 훅은 그 클래스의 모든 핸들러 정점에서 `dispatch` 간선으로 잇는다.
- 프레임워크가 구현한 멤버(`ModelViewSet.list`, `ModelSerializer.save` 등)의 상속 멤버 정점은 그 구현이 부르는 훅을
  `framework` 간선으로 잇는다.
- 프레임워크가 클래스 속성 값으로 인스턴스를 만들어 부르는 프로젝트 코드(`serializer_class`·`permission_classes` 등,
  값이 프로젝트 클래스·함수이거나 그 목록인 속성)는 따라가지 않고 `framework-callback` 미해석 호출로 센다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from pythograph.graph.framework_table import FRAMEWORK_CLASSES
from pythograph.graph.index import Definition
from pythograph.graph.mro import Member
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue, FunctionValue
from pythograph.routes.classes import analyze_class
from pythograph.source.symbols import ProjectSymbol

#: Django·DRF·Flask 클래스 뷰의 HTTP 핸들러 이름이다(`View.http_method_names`, Flask `http_method_funcs`).
HTTP_HANDLERS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

#: DRF 라우터가 연결하는 viewset 표준 action이다(`SimpleRouter.routes`).
VIEWSET_ACTIONS = ("list", "create", "retrieve", "update", "partial_update", "destroy")

#: 뷰 함수를 만드는 진입 멤버다(모든 계열이 `as_view` 클래스 메서드로 뷰를 만든다).
DISPATCH_ENTRY = "as_view"


@dataclass
class Closure:
    """프레임워크 구현이 닿는 프로젝트 훅과 클래스 속성.

    Attributes:
        hooks: 프로젝트 메서드 멤버(이름 순, 중복 없음).
        attributes: 프로젝트 클래스 속성 멤버(이름 순, 중복 없음).
    """

    hooks: list[Member] = field(default_factory=list)
    attributes: list[Member] = field(default_factory=list)


def framework_closure(resolver: Resolver, receiver: Definition, names: list[str], after: str | None = None) -> Closure:
    """프레임워크 구현을 따라 프로젝트 훅과 속성을 모은다.

    호출한 이름과 property 읽기만 따라간다. 메서드를 호출하지 않고 읽기만 하면(`self.head = self.get`) 훅으로 보지
    않는다. 클래스 속성은 읽기·호출 모두 속성 간선이다.

    Args:
        resolver: 값 해석기(MRO 조회).
        receiver: 정확한 수신자 클래스.
        names: 시작 멤버 이름(호출로 본다).
        after: 시작 이름을 이 MRO 항목 다음부터 찾는다(`super()`).

    Returns:
        훅과 속성.
    """
    hooks: dict[str, Member] = {}
    attributes: dict[str, Member] = {}
    visited: set[tuple[str, str | None, bool]] = set()
    pending: list[tuple[str, str | None, bool]] = [(name, after, True) for name in reversed(names)]
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        visited.add(item)
        name, after, called = item
        member = resolver.linearizer.lookup(receiver, name, after)
        if member.kind == "framework-method" and member.owner is not None:
            entry = FRAMEWORK_CLASSES[member.owner.key]["methods"][name]
            if called or entry.get("property"):
                pending.extend(_entry_names(entry, member.owner.key))
        elif member.kind == "method" and member.definition is not None:
            if called or _is_property(resolver, member.definition, name):
                hooks.setdefault(name, member)
        elif member.kind == "attribute":
            attributes.setdefault(name, member)
    return Closure([hooks[key] for key in sorted(hooks)], [attributes[key] for key in sorted(attributes)])


def _entry_names(entry: dict[str, Any], owner: str) -> list[tuple[str, str | None, bool]]:
    """프레임워크 메서드 표 항목이 이어 가는 이름이다.

    Args:
        entry: 메서드 표 항목.
        owner: 메서드를 정의한 프레임워크 클래스.

    Returns:
        (이름, super 기준 클래스, 호출인지) 목록.
    """
    names: list[tuple[str, str | None, bool]] = [(name, None, True) for name in entry["calls"]]
    names.extend((name, None, False) for name in entry["reads"])
    names.extend((name, owner, True) for name in entry["super"])
    return list(reversed(names))


def _is_property(resolver: Resolver, method: Definition, name: str) -> bool:
    """프로젝트 메서드가 property·cached_property인지 본다.

    Args:
        resolver: 값 해석기.
        method: 메서드 정의.
        name: 이름.

    Returns:
        그렇다면 True.
    """
    if method.parent is None:
        return False
    decorators = resolver.index.class_members(method.parent).decorators.get(name, set())
    return bool(decorators & {"property", "cached_property"})


def is_callback_attribute(resolver: Resolver, member: Member) -> bool:
    """클래스 속성 값이 프로젝트 클래스·함수이거나 그 목록·튜플인지 본다(프레임워크가 만들어 부르는 값).

    Args:
        resolver: 값 해석기.
        member: 프로젝트 클래스 속성 멤버.

    Returns:
        그렇다면 True.
    """
    if member.expr is None or member.owner is None or member.owner.definition is None:
        return False
    elements = member.expr.elts if isinstance(member.expr, (ast.List, ast.Tuple)) else [member.expr]
    for element in elements:
        if isinstance(element, (ast.Name, ast.Attribute)):
            value = resolver.value(member.owner.definition, element)
            if isinstance(value, (ClassValue, FunctionValue)):
                return True
    return False


def handler_names(resolver: Resolver, definition: Definition) -> list[str]:
    """모듈 수준 뷰 클래스의 핸들러 이름을 돌려준다(뷰가 아니면 빈 목록).

    routes와 같은 클래스 분석(`routes.classes.analyze_class`)으로 뷰 종류와 DRF action을 정한다.

    Args:
        resolver: 값 해석기.
        definition: 클래스 정의.

    Returns:
        이름 목록(정렬).
    """
    if definition.parent is None or definition.parent.kind != "module":
        return []
    qualname = definition.id.split("#", 1)[1]
    info = analyze_class(resolver.symbols, ProjectSymbol(definition.path, qualname, definition.node))
    if info is None:
        return []
    names: set[str] = set()
    if info.kinds & {"django-view", "drf-apiview", "flask-methodview", "drf-viewset"}:
        names.update(HTTP_HANDLERS)
    if "drf-viewset" in info.kinds:
        names.update(VIEWSET_ACTIONS)
        names.update(action.name for action in info.actions)
        names.update(handler for action in info.actions for handler in action.methods.values())
    if "flask-view" in info.kinds:
        names.add("dispatch_request")
    return sorted(name for name in names if name in info.names)
