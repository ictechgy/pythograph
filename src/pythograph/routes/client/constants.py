"""모듈 상수 증명: URL 조각에 치환해도 되는 모듈 수준 이름인지 판정한다.

계약(HTTP-WRAPPERS `compose` 5): Python은 모듈 최상위에서 한 번만 대입되고 다시 대입되지 않는 이름만 상수로 치환한다.
대문자 이름 관례는 증명이 아니다. 다음이 하나라도 있으면 그 이름은 상수가 아니다:

- 모듈 수준(조건문·반복문·`try`·`with` 안 포함, 함수·클래스 본문 제외)에서 두 번 이상 묶임 — 대입·주석 대입·누적
  대입·반복 변수·`with … as`·import·함수·클래스 정의·`del`·예외 이름·바다코끼리·match 캡처를 모두 센다. 셈은 routes·
  schema 상수 평가와 같은 `source.bindings.module_bindings`다.
- 그 모듈의 어느 함수·클래스든 `global 이름`을 선언함(실행 중 다시 대입할 수 있다).
- 그 모듈이 어느 범위든 `globals()`를, 모듈 수준에서 `vars()`·`locals()`를 부름(`globals()["API_ROOT"] = …`처럼
  이름공간 사전으로 어떤 이름이든 다시 쓸 수 있다). 판정은 graph 해석기와 같은 `source.symbols.uses_namespace`다.
- 프로젝트 어디서든 모듈 속성으로 대입함(`constants.API_ROOT = …`, 리터럴 `setattr(constants, "API_ROOT", …)`).
- 프로젝트 어디서든 수신자 타입을 모르는 같은 이름의 속성 대입이 있음(`sys.modules[__name__].API_ROOT = …`, 타입 모르는
  매개변수의 `target.API_ROOT = …`). 그 수신자가 이 모듈일 수 있다. graph 해석기는 수신자와 무관하게 같은 이름의 모든
  속성 쓰기를 보지만, 여기서는 수신자를 증명한 클래스·인스턴스 대입은 모듈을 바꿀 수 없어 빼고 본다.
"""

from __future__ import annotations

from collections import Counter

from pythograph.source.bindings import ModuleBindings
from pythograph.source.symbols import SymbolTable, uses_namespace

#: 읽지 못한 모듈의 묶음 요약이다(아무 이름도 한 번 묶이지 않아 상수가 없다).
_UNREAD = ModuleBindings(Counter(), frozenset(), frozenset(), frozenset(), Counter())


class ModuleConstants:
    """모듈별 이름 묶음 수를 세어 상수를 증명한다."""

    def __init__(self, symbols: SymbolTable, module_writes: set[tuple[str, str]], untyped_writes: set[str]) -> None:
        """판정기를 만든다.

        Args:
            symbols: 모듈 색인.
            module_writes: 모듈 속성으로 대입된 (모듈 경로, 이름).
            untyped_writes: 수신자 타입을 모르는 속성 대입의 이름(어느 모듈의 같은 이름이든 바꿀 수 있다).
        """
        self.symbols = symbols
        self.module_writes = module_writes
        self.untyped_writes = untyped_writes
        self._modules: dict[str, tuple[ModuleBindings, bool]] = {}

    def proven(self, path: str, name: str) -> bool:
        """모듈의 이름이 한 번만 묶이고 다시 쓰이지 않는 상수인지 본다.

        Args:
            path: 모듈 경로.
            name: 이름.

        Returns:
            상수면 True.
        """
        if (path, name) in self.module_writes or name in self.untyped_writes:
            return False
        bindings, namespace = self._module(path)
        return not namespace and bindings.counts[name] == 1 and name not in bindings.declared_globals

    def _module(self, path: str) -> tuple[ModuleBindings, bool]:
        """모듈의 묶음 요약과 이름공간 사전 사용 여부를 모은다(캐시).

        Args:
            path: 모듈 경로.

        Returns:
            (모듈 색인의 묶음 요약, `globals()`·모듈 수준 `vars()`·`locals()`를 부르는지). 읽지 못한 모듈은 빈 요약이다.
        """
        if path not in self._modules:
            index = self.symbols.index(path)
            self._modules[path] = (
                (_UNREAD, False) if index is None else (index.bindings(), uses_namespace(index.module.tree))
            )
        return self._modules[path]
