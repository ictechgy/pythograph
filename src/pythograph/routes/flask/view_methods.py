"""Flask 클래스 뷰의 `methods` 클래스 속성 값을 Flask가 만드는 대로 구한다.

`View.as_view`는 `view.methods = cls.methods`로 클래스 속성을 뷰 함수에 옮기고, `App.add_url_rule`은
`methods`를 주지 않으면 그 값을, 없거나 거짓(빈 목록 포함)이면 GET을 쓴다. 클래스 속성 값은 다음과 같다(Flask
3.1.3 `flask/views.py`):

- `View`는 `methods = None`을 정의하고 `MethodView`는 정의하지 않는다.
- `MethodView` 하위 클래스는 만들어질 때(`MethodView.__init_subclass__`) 본문이 `methods`를 정의하지
  않으면 직접 기반마다 `getattr(base, "methods")`가 참인 값과 `hasattr(cls, name)`인 핸들러 이름(대문자)의
  합집합을 `cls.methods`로 둔다. 합이 비면 두지 않는다. 그때 상속한 값도 거짓이다(직접 기반의 조회 값이
  모두 거짓이면 사슬에서 처음 정의한 값도 거짓이다). 그래서 기반을 따라 내려가며 본문이 정의한 값과
  `MethodView`가 아닌 기반의 조회 값을 모으고, 핸들러는 등록한 클래스의 사슬 전체에서 본다(기반이 보는
  핸들러는 하위 클래스도 본다).
- 그 밖의 클래스는 보통 속성 조회다(상속 사슬에서 `methods`를 처음 정의한 프로젝트 클래스의 값, 그 전에
  외부 `View`를 만나면 None).

본문 맨 위의 이름 대입과 함수 정의만 값으로 읽고, 다른 방법으로 `methods`·핸들러 이름·`__init_subclass__`를
묶은 클래스(`class_body.irregular_names`: 블록 안 대입·정의, 누적 대입, `del`, 풀어 쓰는 대입 등)는 그 이름을
모른다고 본다. 다음도 값을 모른다고 본다(쓰는 순서·조건은 따지지 않는다):

- 조회가 지나는 클래스(기반 포함)에 본문 밖에서 `methods`를 쓴 코드가 있다(`view_options`의 대상 이름).
- 조회가 지나는 클래스에 옵션 속성을 쓰는 프로젝트 클래스 장식자나 풀지 못한 장식자가 있다(프로젝트 밖
  장식자는 쓰지 않는다고 본다, 함수 뷰 장식자와 같은 원칙).
- 사슬의 프로젝트 클래스가 `type`·`abc.ABCMeta`가 아닌 메타클래스를 준다(메타클래스는 상속된다).
- 프로젝트 기반 클래스의 `__init_subclass__`(하위 클래스를 만들 때 돈다)가 옵션 속성을 쓰거나 함수 정의가
  아니다. 본문이 정의하지 않은 `MethodView` 하위 클래스에서는 `MethodView`보다 앞선 그 훅이 다음 훅을 반드시
  부르지 않을 때도 모른다(`class_body.calls_super_hook`).

표에 없는 외부 기반 클래스는 값에 더하지 않는다(등록한 클래스의 `ClassInfo.unknown_bases`가 확정 불가로 센다).
"""

from __future__ import annotations

import ast
import builtins
from types import EllipsisType

from pythograph.routes.classes import ClassInfo, class_chain, framework_class, own_assignment
from pythograph.routes.flask.class_body import (
    NAMESPACE_WRITES,
    calls_super_hook,
    irregular_names,
    returns_class_decorator,
    returns_its_argument,
)
from pythograph.routes.flask.view_options import stores_options
from pythograph.source.evaluate import literal_value
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, SymbolTable

#: `MethodView`가 핸들러로 보는 이름이다(`flask/views.py` `http_method_funcs`).
HTTP_METHOD_FUNCS = ("get", "post", "head", "options", "delete", "put", "trace", "patch")

#: 클래스 속성 값: 대문자 method 튜플, 거짓 값(None·빈 모음, add_url_rule은 GET), 모름(`...`).
MethodsValue = tuple[str, ...] | None | EllipsisType

#: 펼친 상속 사슬의 항목이다(`ClassInfo.chain`).
Chain = tuple[ProjectSymbol | ExternalSymbol | None, ...]

#: 본문 묶음을 볼 이름들: 클래스 값, `MethodView` 핸들러, 하위 클래스 훅이다.
_METHODS_NAMES = frozenset({"methods"})
_HANDLER_NAMES = frozenset(HTTP_METHOD_FUNCS)
_HOOK_NAMES = frozenset({"__init_subclass__"})

#: 내장 이름이다. 다시 묶지 않은 내장 이름 장식자는 프로젝트 밖 이름이다.
_BUILTIN_NAMES = frozenset(dir(builtins))

#: `methods` 판정을 바꾸지 않는 메타클래스(외부 점 경로)다.
_PLAIN_METACLASSES = frozenset({"abc.ABCMeta"})

#: 훅 하나: (`__init_subclass__` 정의(함수 정의가 아니면 None), 정의한 클래스 이름, `MethodView`보다 앞서는지).
_Hook = tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, str, bool]


class ClassViewMethods:
    """프로젝트 클래스마다 Flask가 뷰 함수에 옮길 `methods` 값을 구해 기억한다(추출 하나 동안 쓴다).

    같은 클래스를 여러 번 등록해도 한 번만 구하고, 등록한 클래스의 사슬은 `analyze_class`가 편 것을 그대로 쓴다.
    기반의 사슬·직접 기반·`MethodView` 여부·본문 판정·기반에서 물려받은 값도 클래스마다 한 번만 구한다.
    """

    def __init__(self, symbols: SymbolTable, option_targets: frozenset[str]) -> None:
        """계산기를 만든다.

        Args:
            symbols: 이름 해석기.
            option_targets: 본문 밖에서 뷰 옵션 속성을 쓴 대상의 끝 이름(`ProjectOptionWrites.targets`).
        """
        self.symbols = symbols
        self.option_targets = option_targets
        # 아래 기억은 모두 정의마다다(같은 점 경로의 재정의는 노드가 달라 따로 센다).
        self._memo: dict[ProjectSymbol, MethodsValue] = {}
        self._chains: dict[ProjectSymbol, Chain] = {}
        self._bases: dict[ProjectSymbol, list[ProjectSymbol]] = {}
        self._inherits: dict[ProjectSymbol, MethodsValue] = {}
        self._method_views: dict[ProjectSymbol, bool] = {}
        self._changed: dict[ProjectSymbol, bool] = {}
        self._irregular: dict[ast.ClassDef, frozenset[str]] = {}

    def value(self, info: ClassInfo) -> MethodsValue:
        """클래스가 만들어진 뒤의 `getattr(cls, "methods")`를 돌려준다.

        Args:
            info: 등록한 프로젝트 클래스의 `analyze_class` 결과.

        Returns:
            대문자 method 튜플, 거짓 값이면 None, 정하지 못하면 `...`.
        """
        symbol = info.symbol
        assert isinstance(symbol, ProjectSymbol)
        self._chains.setdefault(symbol, info.chain)
        if symbol not in self._memo:
            self._memo[symbol] = self._compute(symbol, info.names)
        return self._memo[symbol]

    def _compute(self, symbol: ProjectSymbol, names: set[str]) -> MethodsValue:
        """클래스 하나의 값을 구한다(본문 정의 → `MethodView` 합집합 → 보통 속성 조회).

        Args:
            symbol: 프로젝트 클래스.
            names: 클래스 사슬에서 정의한 이름(`ClassInfo.names`, `hasattr` 판정).

        Returns:
            `value`와 같다.
        """
        chain = self._chain(symbol)
        hooks = self._subclass_hooks(chain)
        hook_writes = any(hook is None or stores_options(hook) for hook, *_ in hooks)
        if self._altered(symbol) or self._metaclass(chain) or hook_writes:
            return ...
        defined, own = self._own(symbol)
        if defined:
            return own
        if not self._is_method_view(symbol):
            return self._looked_up(chain)
        if not all(hook is not None and calls_super_hook(hook, owner) for hook, owner, first in hooks if first):
            return ...
        if any(self._binds_irregularly(entry, _HANDLER_NAMES) for entry in _project_classes(chain)):
            return ...
        return self._subclass_union(symbol, names)

    def _subclass_hooks(self, chain: Chain) -> list[_Hook]:
        """프로젝트 기반 클래스 본문이 정의한 `__init_subclass__`를 모은다(클래스 자신의 것은 하위 클래스에만 돈다).

        사슬에서 외부 `MethodView`보다 앞선 것인지도 표시한다. 앞선 것이 다음 훅을 부르지 않으면 `MethodView`의
        합집합이 돌지 않는다. 뒤의 것은 `MethodView`가 먼저 부른 뒤 합집합을 만든다.

        Args:
            chain: 클래스의 펼친 상속 사슬.

        Returns:
            훅 목록.
        """
        hooks: list[_Hook] = []
        first = True
        for entry in chain[1:]:
            if isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-methodview":
                first = False
            if not isinstance(entry, ProjectSymbol) or not isinstance(entry.node, ast.ClassDef):
                continue
            node = entry.node
            hooks.extend(
                (statement, node.name, first)
                for statement in node.body
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == "__init_subclass__"
            )
            assigned = own_assignment(node, "__init_subclass__") is not None
            if assigned or self._binds_irregularly(entry, _HOOK_NAMES):
                hooks.append((None, node.name, first))
        return hooks

    def _subclass_union(self, symbol: ProjectSymbol, names: set[str]) -> MethodsValue:
        """본문이 `methods`를 정의하지 않은 `MethodView` 하위 클래스의 값을 `__init_subclass__`처럼 모은다.

        기반에서 물려받은 값(`_inherited`)에 클래스 사슬 전체의 핸들러 이름을 더한다. 기반의 값은 그 기반이 물려받은
        값과 그 기반의 핸들러 이름의 합인데, 기반이 보는 핸들러는 하위 클래스도 보므로 물려받은 값만 모으면 된다.

        Args:
            symbol: 등록한 클래스.
            names: 클래스 사슬에서 정의한 이름.

        Returns:
            합집합(비면 None), 모르는 기반 값이 있으면 `...`.
        """
        inherited = self._inherited(symbol)
        if isinstance(inherited, EllipsisType):
            return ...
        handlers = [name.upper() for name in HTTP_METHOD_FUNCS if name in names]
        return tuple(dict.fromkeys([*(inherited or ()), *handlers])) or None

    def _inherited(self, root: ProjectSymbol) -> MethodsValue:
        """`MethodView` 하위 클래스가 직접 기반에서 물려받는 값을 클래스마다 한 번 구한다(재귀 없이, 기반 먼저).

        본문이 정의하지 않은 `MethodView` 하위 기반은 그 기반이 물려받은 값을 쓰므로, 그런 기반을 먼저 구하도록 쌓는다.
        다이아몬드의 같은 기반은 기억한 값을 다시 쓰고(등록 수 × 깊이를 걷지 않는다), 정적 상속 순환(다시 묶인 이름이
        자기 자신으로 풀린다)에 걸린 클래스는 모른다.

        Args:
            root: 프로젝트 클래스.

        Returns:
            물려받은 대문자 method 튜플(없으면 빈 튜플), 모르면 `...`.
        """
        stack: list[tuple[ProjectSymbol, bool]] = [(root, False)]
        active: set[ProjectSymbol] = set()
        while stack:
            symbol, ready = stack.pop()
            if symbol in self._inherits:
                continue
            if ready:
                # 순환으로 아직 구하지 못한 기반은 `_collect`가 모름으로 센다.
                self._inherits[symbol] = self._collect(symbol)
                active.discard(symbol)
                continue
            active.add(symbol)
            stack.append((symbol, True))
            pending = [base for base in self._descended(symbol) if base not in active and base not in self._inherits]
            stack.extend((base, False) for base in pending)
        return self._inherits[root]

    def _descended(self, symbol: ProjectSymbol) -> list[ProjectSymbol]:
        """물려받은 값을 먼저 구해야 하는 직접 기반(본문이 정의하지 않은 `MethodView` 하위 기반)을 돌려준다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            기반 목록.
        """
        return [base for base in self._project_bases(symbol) if not self._own(base)[0] and self._is_method_view(base)]

    def _collect(self, symbol: ProjectSymbol) -> MethodsValue:
        """직접 기반마다 `getattr(base, "methods")`의 기여를 모은다(물려받을 기반의 값은 이미 구했다).

        본문이 정의한 기반은 그 값, `MethodView`가 아닌 기반은 보통 속성 조회 값, 정의하지 않은 `MethodView` 하위
        기반은 그 기반이 물려받은 값이다. 외부 기반은 더하지 않는다: `View`·`MethodView`·투명 믹스인의 값은 거짓이고
        표에 없는 외부 기반은 등록한 클래스의 `unknown_bases`가 센다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            대문자 method 튜플, 모르면 `...`.
        """
        collected: list[str] = []
        for base in self._project_bases(symbol):
            if self._altered(base):
                return ...
            defined, own = self._own(base)
            if defined:
                found = own
            elif self._is_method_view(base):
                # 순환에 걸린 기반(아직 구하지 못했다)은 모른다.
                found = self._inherits.get(base, ...)
            else:
                found = self._looked_up(self._chain(base))
            if isinstance(found, EllipsisType):
                return ...
            collected.extend(found or ())
        return tuple(dict.fromkeys(collected))

    def _project_bases(self, symbol: ProjectSymbol) -> list[ProjectSymbol]:
        """프로젝트 직접 기반 클래스를 돌려준다(풀이는 클래스마다 한 번).

        Args:
            symbol: 기반을 적은 클래스.

        Returns:
            기반 목록(적은 순서).
        """
        if symbol not in self._bases:
            node = symbol.node
            assert isinstance(node, ast.ClassDef)
            resolved = [self.symbols.resolve_expr(symbol.path, expr) for expr in node.bases]
            self._bases[symbol] = [
                base for base in resolved if isinstance(base, ProjectSymbol) and isinstance(base.node, ast.ClassDef)
            ]
        return self._bases[symbol]

    def _looked_up(self, chain: Chain) -> MethodsValue:
        """`MethodView`가 아닌 클래스의 보통 속성 조회 값을 구한다.

        상속 사슬을 차례로 보며 본문이 정의한 첫 프로젝트 클래스의 값을 쓰고, 그 전에 외부 `View`를 만나면 None이다.

        Args:
            chain: 클래스의 펼친 상속 사슬.

        Returns:
            값(정의가 없으면 None), 값을 바꿀 수 있는 클래스나 읽지 못하는 정의를 지나면 `...`.
        """
        for entry in chain:
            if isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-view":
                return None
            if not isinstance(entry, ProjectSymbol) or not isinstance(entry.node, ast.ClassDef):
                continue
            if self._altered(entry):
                return ...
            defined, own = self._own(entry)
            if defined:
                return own
        return None

    def _chain(self, symbol: ProjectSymbol) -> Chain:
        """클래스의 상속 사슬을 돌려준다(등록한 클래스는 `analyze_class`가 편 것, 그 밖은 한 번 펴 기억한다).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            클래스 목록(첫 항목이 클래스 자신, 해석 실패는 None).
        """
        if symbol not in self._chains:
            self._chains[symbol] = class_chain(self.symbols, symbol)
        return self._chains[symbol]

    def _is_method_view(self, symbol: ProjectSymbol) -> bool:
        """클래스가 `MethodView` 하위 클래스인지 본다(사슬에 외부 `MethodView`가 있다).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            맞으면 True.
        """
        if symbol not in self._method_views:
            self._method_views[symbol] = any(
                isinstance(entry, ExternalSymbol) and _flask_kind(entry) == "flask-methodview"
                for entry in self._chain(symbol)
            )
        return self._method_views[symbol]

    def _altered(self, symbol: ProjectSymbol) -> bool:
        """클래스가 만들어진 뒤 `methods`가 바뀔 수 있는지 본다: 본문 밖 쓰기(`Hook.methods = …`)나 장식자.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            바뀔 수 있으면 True.
        """
        if symbol not in self._changed:
            node = symbol.node
            assert isinstance(node, ast.ClassDef)
            self._changed[symbol] = node.name in self.option_targets or any(
                self._decorator_may_set(symbol.path, decorator) for decorator in node.decorator_list
            )
        return self._changed[symbol]

    def _decorator_may_set(self, path: str, decorator: ast.expr) -> bool:
        """클래스 장식자가 `methods`를 바꿀 수 있는지 본다(옵션 속성을 쓰거나 다른 클래스를 돌려준다).

        프로젝트 함수는 본문(장식자 팩토리면 팩토리 본문 전체)에 옵션 속성 쓰기가 없고 받은 클래스를 그대로 돌려줄 때만
        바꾸지 않는다고 본다(`class_body.returns_its_argument`, 팩토리는 `returns_class_decorator`). 다른 클래스·하위
        클래스·None을 돌려줄 수 있으면 그 결과의 `methods`를 모른다. 다시 묶지 않은 내장 이름과 프로젝트 밖
        이름(`dataclass` 등)은 바꾸지 않는다고 본다(함수 뷰 장식자와 같은 원칙). 그 밖(프로젝트 클래스, 매개변수,
        모듈 변수, 인스턴스 메서드처럼 풀지 못한 장식자)은 바꿀 수 있다고 본다.

        Args:
            path: 클래스를 정의한 모듈 경로.
            decorator: 장식자 식.

        Returns:
            바꿀 수 있으면 True.
        """
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        resolved = self.symbols.resolve_expr(path, target) if isinstance(target, (ast.Name, ast.Attribute)) else None
        if isinstance(resolved, ProjectSymbol) and isinstance(
            resolved.node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            node = resolved.node
            unchanged = returns_class_decorator(node) if isinstance(decorator, ast.Call) else returns_its_argument(node)
            return stores_options(node) or not unchanged
        return not isinstance(resolved, ExternalSymbol) and not _builtin(target, resolved)

    def _metaclass(self, chain: Chain) -> bool:
        """사슬의 프로젝트 클래스가 `methods` 판정을 바꿀 수 있는 메타클래스를 주는지 본다(`type`·`abc.ABCMeta` 밖).

        Args:
            chain: 클래스의 펼친 상속 사슬.

        Returns:
            주면 True.
        """
        for entry in _project_classes(chain):
            node = entry.node
            assert isinstance(node, ast.ClassDef)
            for keyword in node.keywords:
                if keyword.arg != "metaclass":
                    continue
                value = keyword.value
                named = isinstance(value, (ast.Name, ast.Attribute))
                resolved = self.symbols.resolve_expr(entry.path, value) if named else None
                plain = (isinstance(resolved, ExternalSymbol) and resolved.dotted in _PLAIN_METACLASSES) or (
                    resolved is None and isinstance(value, ast.Name) and value.id == "type"
                )
                if not plain:
                    return True
        return False

    def _binds_irregularly(self, symbol: ProjectSymbol, names: frozenset[str]) -> bool:
        """클래스 본문이 이름 중 하나를 정적으로 읽을 수 없게 묶거나 이름공간 전체를 바꿀 수 있는지 본다.

        Args:
            symbol: 프로젝트 클래스.
            names: 볼 이름들.

        Returns:
            그러면 True.
        """
        found = self._irregular_names(symbol)
        return NAMESPACE_WRITES in found or not found.isdisjoint(names)

    def _irregular_names(self, symbol: ProjectSymbol) -> frozenset[str]:
        """클래스 본문이 정적으로 읽을 수 없게 묶은 이름을 기억해 둔 결과로 돌려준다(`class_body.irregular_names`).

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            이름 집합.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        if node not in self._irregular:
            self._irregular[node] = irregular_names(node)
        return self._irregular[node]

    def _own(self, symbol: ProjectSymbol) -> tuple[bool, MethodsValue]:
        """클래스 본문이 직접 정의한 `methods` 값을 읽는다.

        Args:
            symbol: 프로젝트 클래스.

        Returns:
            (정의했는지, 값). 값은 대문자 method 튜플, `None`이나 빈 모음이면 None, 리터럴 문자열 모음이 아니거나
            본문이 맨 위 대입 밖에서 `methods`를 묶으면 `...`이다.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        if self._binds_irregularly(symbol, _METHODS_NAMES):
            return True, ...
        expr = own_assignment(node, "methods")
        if expr is None:
            return False, None
        declared = literal_value(expr)
        if declared is None:
            return True, None
        if isinstance(declared, (list, tuple, set)) and all(isinstance(item, str) for item in declared):
            return True, tuple(dict.fromkeys(str(item).upper() for item in declared)) or None
        return True, ...


def _project_classes(chain: Chain) -> list[ProjectSymbol]:
    """사슬에서 프로젝트 클래스 정의만 고른다.

    Args:
        chain: 펼친 상속 사슬.

    Returns:
        프로젝트 클래스 목록(사슬 순서).
    """
    return [entry for entry in chain if isinstance(entry, ProjectSymbol) and isinstance(entry.node, ast.ClassDef)]


def _builtin(target: ast.expr, resolved: object) -> bool:
    """풀리지 않은 이름이 다시 묶지 않은 내장 이름인지 본다.

    Args:
        target: 장식자 식.
        resolved: 풀이 결과.

    Returns:
        내장 이름이면 True.
    """
    return resolved is None and isinstance(target, ast.Name) and target.id in _BUILTIN_NAMES


def _flask_kind(entry: ExternalSymbol) -> str | None:
    """외부 클래스의 Flask 뷰 종류를 돌려준다.

    Args:
        entry: 외부 클래스.

    Returns:
        `flask-view`·`flask-methodview`, 그 밖이면 None.
    """
    known = framework_class(entry.dotted)
    return known[0] if known is not None and known[0].startswith("flask-") else None
