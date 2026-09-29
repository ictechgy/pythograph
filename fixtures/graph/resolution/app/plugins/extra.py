"""별 import와 외부 기반 클래스다."""

from app.helpers import *  # noqa: F403

import collections


class Counter(collections.OrderedDict):
    """외부 기반 클래스를 상속한다(멤버는 외부)."""

    def bump(self, key):
        """외부 멤버 호출과 자기 메서드다."""
        self.setdefault(key, 0)
        return self.total()

    def total(self):
        """자기 메서드다."""
        return sum(self.values())


def use_star():
    """별 import로 들어온 프로젝트 함수다."""
    return slugify("A B")  # noqa: F405
