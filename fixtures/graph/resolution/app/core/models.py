"""클래스 계층: 단일 상속, 다이아몬드(C3), 재정의, 하위 클래스 전용 메서드, property, 클래스·정적 메서드."""

from __future__ import annotations


class Base:
    """기반 클래스다."""

    kind = "base"

    def __init__(self, name):
        """이름을 저장한다."""
        self.name = name

    def describe(self):
        """훅을 부르는 템플릿 메서드다(하위 클래스가 `label`을 재정의한다)."""
        return self.label() + self.suffix()

    def label(self):
        """기본 라벨이다."""
        return self.name

    def suffix(self):
        """기본 접미사다."""
        return ""

    def render(self):
        """하위 클래스에만 있는 `template`을 부른다."""
        return self.template()

    @property
    def title(self):
        """property 읽기는 호출이다."""
        return self.label().title()

    @title.setter
    def title(self, value):
        """property 쓰기도 호출이다."""
        self.name = value

    @classmethod
    def create(cls, name):
        """`cls(...)`는 하위 클래스일 수 있는 생성이다."""
        return cls(name)

    @staticmethod
    def normalize(name):
        """정적 메서드다."""
        return name.strip()


class Left(Base):
    """다이아몬드의 왼쪽이다."""

    def label(self):
        """라벨을 재정의하고 super로 기반을 부른다."""
        return "L:" + super().label()

    def template(self):
        """하위 클래스 전용 메서드다."""
        return "left"


class Right(Base):
    """다이아몬드의 오른쪽이다."""

    def suffix(self):
        """접미사를 재정의한다."""
        return "!"


class Diamond(Left, Right):
    """C3 선형화: Diamond, Left, Right, Base다."""

    def __init__(self, name, extra):
        """super().__init__은 C3상 Left 다음(Right→Base)의 `__init__`이다."""
        super().__init__(name)
        self.extra = extra


class Plain(Base):
    """아무것도 재정의하지 않는다(물려받은 멤버 정점)."""


def build() -> Base:
    """반환 주석으로 수신자 클래스를 안다."""
    return Plain("p")
