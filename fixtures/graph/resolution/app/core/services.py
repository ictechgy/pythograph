"""수신자 해석: 생성자, 반환 주석, 매개변수 주석, 모듈 수준 인스턴스, 콜백, 참조, getattr."""

from app import slugify
from app.core import models as m
from app.core.models import Diamond, Plain, build
from app.helpers import cached, traced
from ..helpers import dump as serialize
from .missing import nothing


class Registry:
    """콜백과 참조를 보관한다."""

    def __init__(self):
        """콜백 목록을 만든다."""
        self.callbacks = []

    def register(self, callback):
        """콜백을 등록한다."""
        self.callbacks.append(callback)

    def fire(self):
        """등록한 콜백을 부른다(반복 변수 호출이라 잇지 못한다)."""
        for callback in self.callbacks:
            callback()


registry = Registry()


@traced
def exact_calls():
    """생성자로 만든 정확한 인스턴스의 호출이다."""
    diamond = Diamond("d", 1)
    plain = Plain("p")
    plain.describe()
    diamond.describe()
    return m.Base.normalize(" x ")


@cached(60)
def inexact_calls(item: m.Base, other: "Plain | None" = None):
    """주석으로만 아는 수신자다(하위 클래스 재정의 후보)."""
    item.describe()
    item.render()
    build().label()
    return other


def callbacks_and_references():
    """콜백·참조 간선이다."""
    registry.register(exact_calls)
    handler = inexact_calls
    table = {"slug": slugify}
    items = sorted(["b", "a"], key=slugify)
    return handler, table, items, serialize


def dynamic_calls(name, factory):
    """잇지 못하는 호출: 매개변수, getattr, 동적 피호출, 람다, 컴프리헨션, 풀지 못한 import."""
    factory()
    getattr(registry, name)()
    getattr(registry, "fire")()
    registry.callbacks[0]()
    apply = lambda value: value.describe()  # noqa: E731
    apply(Plain("x"))
    [entry.render() for entry in [Plain("y")]]
    nothing()
    unknown_name()  # noqa: F821
    return name.upper()


def property_use(item: m.Base):
    """property 읽기·쓰기다."""
    item.title = "new"
    return item.title


def nested():
    """중첩 함수와 클로저다."""

    def inner():
        """바깥 함수의 지역 함수다."""
        return exact_calls()

    return inner()
