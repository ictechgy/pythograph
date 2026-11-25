"""Flask 클래스 뷰의 `methods` 클래스 속성 값을 Flask가 만드는 대로 구한다.

`View.as_view`는 `view.methods = cls.methods`로 클래스 속성을 뷰 함수에 옮기고, `App.add_url_rule`은
`methods`를 주지 않으면 그 값을, 없거나 거짓이면 GET을 쓴다. 클래스 속성 값은 다음과 같다(Flask 3.1.3
`flask/views.py`):

- `View`는 `methods = None`을 정의하고 `MethodView`는 정의하지 않는다.
- `MethodView` 하위 클래스는 만들어질 때(`MethodView.__init_subclass__`) 본문이 `methods`를 정의하지
  않으면 직접 기반마다 `getattr(base, "methods")`가 참인 값과 `hasattr(cls, name)`인 핸들러 이름(대문자)의
  합집합을 `cls.methods`로 둔다. 합이 비면 두지 않는다. 그때 상속한 값도 거짓이다(직접 기반의 조회 값이
  모두 거짓이면 사슬에서 처음 정의한 값도 거짓이다). 그래서 기반을 따라 내려가며 본문이 정의한 값과
  `MethodView`가 아닌 기반의 조회 값을 모으고, 핸들러는 등록한 클래스의 사슬 전체에서 본다(기반이 보는
  핸들러는 하위 클래스도 본다).
- 그 밖의 클래스는 보통 속성 조회다(상속 사슬에서 `methods`를 처음 정의한 프로젝트 클래스의 값, 그 전에
  외부 `View`를 만나면 None).

본문이 직접 대입한 값만 본다(`if`·`try` 블록 안 대입은 `analyze_class`처럼 보지 않는다). 본문 밖에서
`methods`를 쓴 클래스(`view_options`의 대상 이름)를 조회가 지나면 쓰는 순서를 따지지 않고 모른다고 본다.
프로젝트 기반 클래스가 정의한 `__init_subclass__`는 하위 클래스를 만들 때 돈다. 그것이 옵션 속성을 쓰면
값을 모르고, `super().__init_subclass__`를 부르지 않으면 `MethodView`의 합집합이 돌지 않으므로 본문이
정의하지 않은 `MethodView` 하위 클래스의 값을 모른다. 표에 없는 외부 기반 클래스는 값에 더하지 않는다
(등록한 클래스의 `ClassInfo.unknown_bases`가 확정 불가로 센다).
"""

from __future__ import annotations

import ast
from types import EllipsisType

from pythograph.routes.classes import MAX_CLASS_DEPTH, analyze_class, class_chain, framework_class, own_assignment
from pythograph.routes.flask.view_options import stores_options
from pythograph.source.evaluate import literal_value
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, SymbolTable

#: `MethodView`가 핸들러로 보는 이름이다(`flask/views.py` `http_method_funcs`).
HTTP_METHOD_FUNCS = ("get", "post", "head", "options", "delete", "put", "trace", "patch")

#: 클래스 속성 값: 대문자 method 튜플(본문이 정의한 빈 목록은 빈 튜플), 거짓 값(None), 모름(`...`).
MethodsValue = tuple[str, ...] | None | EllipsisType


class ClassViewMethods:
    """프로젝트 클래스마다 Flask가 뷰 함수에 옮길 `methods` 값을 구해 기억한다(추출 하나 동안 쓴다).

    같은 클래스를 여러 번 등록해도 상속 사슬을 한 번만 걷는다.
    """

    def __init__(self, symbols: SymbolTable, option_targets: frozenset[str]) -> None:
        """계산기를 만든다.

        Args:
            symbols: 이름 해석기.
            option_targets: 본문 밖에서 뷰 옵션 속성을 쓴 대상의 끝 이름(`ProjectOptionWrites.targets`).
        """
        self.symbols = symbols
        self.option_targets = option_targets
        # 정의마다 기억한다(같은 점 경로의 재정의는 노드가 달라 따로 센다).
        self._memo: dict[ProjectSymbol, MethodsValue] = {}
        # 클래스마다 펼친 상속 사슬이다(여러 하위 클래스가 같은 기반을 다시 펴지 않는다).
        self._chains: dict[ProjectSymbol, tuple[ProjectSymbol | ExternalSymbol | None, ...]] = {}

    def value(self, symbol: ProjectSymbol) -> MethodsValue:
        """클래스가 만들어진 뒤의 `getattr(cls, "methods")`를 돌려준다.

        Args:
            symbol: 프로젝트 클래스(`ast.ClassDef` 노드).

        Returns:
            대문자 method 튜플, 거짓 값이면 None, 정하지 못하면 `...`.
        """
        if symbol not in self._memo:
            self._memo[symbol] = self._compute(symbol)
        return self._memo[symbol]

    def _compute(self, symbol: ProjectSymbol) -> MethodsValue:
        """클래스 하나의 값을 구한다(본문 정의 → `MethodView` 합집합 → 보통 속성 조회).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            `value`와 같다.
        """
        hooks = self._subclass_hooks(symbol)
        if self._written(symbol) or any(hook is None or stores_options(hook) for hook, _ in hooks):
            return ...
        defined, own = self._own(symbol)
        if defined:
            return own
        if self._is_method_view(symbol):
            if not all(hook is not None and _calls_super_hook(hook) for hook, first in hooks if first):
                return ...
            return self._subclass_union(symbol)
        return self._looked_up(symbol)

    def _subclass_hooks(
        self, symbol: ProjectSymbol
    ) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, bool]]:
        """프로젝트 기반 클래스 본문이 정의한 `__init_subclass__`를 모은다(클래스 자신의 것은 하위 클래스에만 돈다).

        사슬에서 외부 `MethodView`보다 앞선 것인지도 표시한다. 앞선 것이 `super().__init_subclass__`를 부르지 않으면
        `MethodView`의 합집합이 돌지 않는다. 뒤의 것은 `MethodView`가 먼저 부른 뒤 합집합을 만든다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            (함수 정의(함수 정의가 아닌 대입은 None), `MethodView`보다 앞서는지) 목록.
        """
        hooks: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, bool]] = []
        first = True
        for entry in self._chain(symbol)[1:]:
            if isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-methodview":
                first = False
            if not isinstance(entry, ProjectSymbol) or not isinstance(entry.node, ast.ClassDef):
                continue
            hooks.extend(
                (statement, first)
                for statement in entry.node.body
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == "__init_subclass__"
            )
            if own_assignment(entry.node, "__init_subclass__") is not None:
                hooks.append((None, first))
        return hooks

    def _chain(self, symbol: ProjectSymbol) -> tuple[ProjectSymbol | ExternalSymbol | None, ...]:
        """클래스의 상속 사슬을 기억해 둔 결과로 돌려준다(`classes.class_chain`).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            클래스 목록(첫 항목이 클래스 자신, 해석 실패는 None).
        """
        if symbol not in self._chains:
            self._chains[symbol] = class_chain(self.symbols, symbol)
        return self._chains[symbol]

    def _subclass_union(self, symbol: ProjectSymbol) -> MethodsValue:
        """본문이 `methods`를 정의하지 않은 `MethodView` 하위 클래스의 값을 `__init_subclass__`처럼 모은다.

        직접 기반을 따라 내려가며, 본문이 정의한 기반은 그 값을, `MethodView`가 아닌 기반은 보통 속성 조회 값을 더하고,
        정의하지 않은 `MethodView` 하위 기반은 다시 그 기반들로 내려간다. 같은 기반은 한 번만 본다(다이아몬드).

        Args:
            symbol: 등록한 클래스.

        Returns:
            합집합(비면 None), 모르는 기반 값이 있으면 `...`.
        """
        collected: list[str] = []
        seen = {symbol}
        pending = [(symbol, 0)]
        while pending:
            current, depth = pending.pop()
            for base in self._project_bases(current, depth, seen):
                seen.add(base)
                if self._written(base):
                    return ...
                defined, own = self._own(base)
                if not defined and self._is_method_view(base):
                    pending.append((base, depth + 1))
                    continue
                found = own if defined else self._looked_up(base)
                if isinstance(found, EllipsisType):
                    return ...
                collected.extend(found or ())
        info = analyze_class(self.symbols, symbol)
        handlers = [name.upper() for name in HTTP_METHOD_FUNCS if info is not None and name in info.names]
        return tuple(dict.fromkeys([*collected, *handlers])) or None

    def _project_bases(self, symbol: ProjectSymbol, depth: int, seen: set[ProjectSymbol]) -> list[ProjectSymbol]:
        """아직 보지 않은 프로젝트 직접 기반 클래스를 돌려준다.

        외부 기반은 더하지 않는다: `View`·`MethodView`·투명 믹스인의 값은 거짓이고, 표에 없는 외부 기반과 깊이 상한
        너머는 등록한 클래스의 `unknown_bases`가 센다(`analyze_class`가 같은 상한으로 사슬을 편다).

        Args:
            symbol: 기반을 적은 클래스.
            depth: 그 클래스의 깊이.
            seen: 이미 본 클래스(정의).

        Returns:
            기반 목록(적은 순서).
        """
        if depth >= MAX_CLASS_DEPTH:
            return []
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        bases: list[ProjectSymbol] = []
        for expr in node.bases:
            resolved = self.symbols.resolve_expr(symbol.path, expr)
            if not isinstance(resolved, ProjectSymbol) or not isinstance(resolved.node, ast.ClassDef):
                continue
            if resolved not in seen:
                bases.append(resolved)
        return bases

    def _looked_up(self, symbol: ProjectSymbol) -> MethodsValue:
        """`MethodView`가 아닌 클래스의 보통 속성 조회 값을 구한다.

        상속 사슬을 차례로 보며 본문이 정의한 첫 프로젝트 클래스의 값을 쓰고, 그 전에 외부 `View`를 만나면 None이다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            값(정의가 없으면 None), 본문 밖에서 쓴 클래스나 리터럴이 아닌 정의를 지나면 `...`.
        """
        for entry in self._chain(symbol):
            if isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-view":
                return None
            if not isinstance(entry, ProjectSymbol) or not isinstance(entry.node, ast.ClassDef):
                continue
            if self._written(entry):
                return ...
            defined, own = self._own(entry)
            if defined:
                return own
        return None

    def _is_method_view(self, symbol: ProjectSymbol) -> bool:
        """클래스가 `MethodView` 하위 클래스인지 본다(사슬에 외부 `MethodView`가 있다).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            맞으면 True.
        """
        return any(
            isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-methodview"
            for entry in self._chain(symbol)
        )

    def _written(self, symbol: ProjectSymbol) -> bool:
        """본문 밖에서 그 클래스 이름에 뷰 옵션 속성을 쓰는 코드가 있는지 본다(`Hook.methods = …`).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            있으면 True.
        """
        node = symbol.node
        return isinstance(node, ast.ClassDef) and node.name in self.option_targets

    @staticmethod
    def _own(symbol: ProjectSymbol) -> tuple[bool, MethodsValue]:
        """클래스 본문이 직접 정의한 `methods` 값을 읽는다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            (정의했는지, 값). 값은 대문자 method 튜플, `None` 정의면 None, 리터럴 문자열 모음이 아니면 `...`이다.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        expr = own_assignment(node, "methods")
        if expr is None:
            return False, None
        declared = literal_value(expr)
        if declared is None:
            return True, None
        if isinstance(declared, (list, tuple, set)) and all(isinstance(item, str) for item in declared):
            return True, tuple(dict.fromkeys(str(item).upper() for item in declared))
        return True, ...


def _calls_super_hook(hook: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """`__init_subclass__` 정의가 본문 맨 위 문장에서 `super().__init_subclass__(…)`를 부르는지 본다.

    인자 있는 `super(…)`와 `return super().__init_subclass__(…)`도 받는다. 조건·반복·`try` 안의 호출은 돌지 않을 수
    있으므로(클래스 키워드 인자로 건너뛰는 훅) 부르지 않는다고 본다.

    Args:
        hook: `__init_subclass__` 정의.

    Returns:
        부르면 True.
    """
    return any(
        isinstance(statement, (ast.Expr, ast.Return)) and _is_super_hook_call(statement.value)
        for statement in hook.body
    )


def _is_super_hook_call(node: ast.expr | None) -> bool:
    """식이 `super(…).__init_subclass__(…)` 호출인지 본다.

    Args:
        node: 식.

    Returns:
        맞으면 True.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__init_subclass__"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    )


def _flask_kind(entry: ExternalSymbol) -> str | None:
    """외부 클래스의 Flask 뷰 종류를 돌려준다.

    Args:
        entry: 외부 클래스.

    Returns:
        `flask-view`·`flask-methodview`, 그 밖이면 None.
    """
    known = framework_class(entry.dotted)
    return known[0] if known is not None and known[0].startswith("flask-") else None
