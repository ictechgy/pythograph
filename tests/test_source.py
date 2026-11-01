"""소스 계층(프로젝트 읽기·상수 평가·이름 해석) 테스트."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

import pytest

from pythograph.graph import exposure
from pythograph.graph.build import scan_modules
from pythograph.graph.index import DefinitionIndex
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue
from pythograph.routes.client.constants import ModuleConstants
from pythograph.source import project as project_module
from pythograph.source import syntax
from pythograph.source.bindings import module_bindings
from pythograph.source.evaluate import MAX_EVALUATED_LENGTH, UNKNOWN, Evaluator, literal_value, value_size
from pythograph.source.project import Project, ReadFailure, SourceModule, is_test_path
from pythograph.source.symbols import (
    ExternalSymbol,
    MemberSymbol,
    ModuleSymbol,
    ProjectSymbol,
    SymbolTable,
    ValueSymbol,
    absolute_module,
)
from pythograph.source.syntax import (
    MAX_AST_DEPTH,
    MAX_CHAIN_TOKENS,
    MAX_EXTRACTION_AST_DEPTH,
    exceeds_depth,
    parse_annotation,
    tree_overflow,
)
from pythograph.source.text import is_contract_safe
from tests.conftest import DEEP_SOURCES, REPOSITORY, tree_depth


def _expr(source: str) -> ast.expr:
    """식 하나를 파싱한다.

    Args:
        source: 식 소스.

    Returns:
        식 노드.
    """
    return ast.parse(source, mode="eval").body


def test_evaluator_constants(make_project: Callable[[dict[str, str]], Path]) -> None:
    """리터럴·f-string·`+`·모듈 상수·다른 모듈 상수·설정값을 평가하고 나머지는 UNKNOWN이다."""
    root = make_project(
        {
            "pkg/__init__.py": "",
            "pkg/consts.py": 'PREFIX = "api"\nVERSION: str = "v1"\n',
            "pkg/mod.py": 'from pkg import consts\nfrom django.conf import settings\nNAME = consts.PREFIX + "/x"\n',
        }
    )
    symbols = SymbolTable(Project.open(root))
    evaluator = Evaluator(symbols, {"MEDIA_URL": "/media/"})
    assert evaluator.value("pkg/mod.py", _expr('f"{NAME}/{consts.VERSION}"')) == "api/x/v1"
    assert evaluator.value("pkg/mod.py", _expr("settings.MEDIA_URL")) == "/media/"
    assert evaluator.value("pkg/mod.py", _expr("settings.OTHER")) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr('["a"] + ["b"]')) == ["a", "b"]
    assert evaluator.value("pkg/mod.py", _expr('"a" + 1')) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr('f"{NAME:>5}"')) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr('f"{1}"')) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr("[missing]")) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr("b'x'")) is UNKNOWN
    assert evaluator.value("pkg/mod.py", _expr("call()")) is UNKNOWN
    assert evaluator.value("pkg/mod.py", None) is UNKNOWN
    assert evaluator.string("pkg/mod.py", _expr("1")) is None
    assert evaluator.string_list("pkg/mod.py", _expr("[1]")) is None


def _reference_chain(prefix: str, width: int, levels: int, *, as_list: bool) -> str:
    """앞 단계 이름을 `width`번 참조하는 모듈 상수 사슬 소스를 만든다(`X1 = f"{X0}{X0}…"` 또는 `[X0, X0, …]`).

    Args:
        prefix: 이름 접두사.
        width: 단계마다 참조 수.
        levels: 단계 수(맨 아래 `X0`은 `"a"`).
        as_list: 목록 사슬인지(아니면 f-string 사슬).

    Returns:
        모듈 소스.
    """
    lines = [f'{prefix}0 = "a"']
    for level in range(1, levels + 1):
        previous = f"{prefix}{level - 1}"
        body = ", ".join([previous] * width) if as_list else "".join("{" + previous + "}" for _ in range(width))
        lines.append(f"{prefix}{level} = " + (f"[{body}]" if as_list else f'f"{body}"'))
    return "\n".join(lines) + "\n"


def test_evaluator_expands_repeated_references_once(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 상수를 여러 번 참조하는 f-string·목록 사슬은 단계마다 한 번만 펼친다(평가 횟수가 참조 수의 거듭제곱이 아님).

    설정 모듈의 대문자 이름을 모두 평가하므로 수백 바이트 사슬 하나가 명령을 수십 초·GiB 메모리로 끌고 가던 문제다.
    """
    root = make_project(
        {
            "chain.py": _reference_chain("S", 4, 6, as_list=False) + _reference_chain("L", 4, 6, as_list=True),
        }
    )
    symbols = SymbolTable(Project.open(root))
    index = symbols.index("chain.py")
    assert index is not None
    resolved: list[str] = []
    resolve_name = symbols.resolve_name

    def counting(path: str, name: str, hops: int = 0) -> object:
        resolved.append(name)
        return resolve_name(path, name, hops)

    monkeypatch.setattr(symbols, "resolve_name", counting)
    evaluator = Evaluator(symbols)
    assert evaluator.value("chain.py", index.values["S6"]) == "a" * 4**6
    expected: object = "a"
    for _ in range(6):
        expected = [expected] * 4
    assert evaluator.value("chain.py", index.values["L6"]) == expected
    # 단계마다 참조 4개를 한 번씩 푼다(메모 없이 펼치면 4 + 4² + … + 4⁶ = 5,460번).
    assert len(resolved) == 2 * 6 * 4


def test_evaluator_caps_constructed_values(make_project: Callable[[dict[str, str]], Path]) -> None:
    """f-string·`+`·목록 리터럴로 만든 값이 `MAX_EVALUATED_LENGTH`를 넘으면 UNKNOWN이다.

    상한까지 만든 값과 소스의 문자열 리터럴 하나는 그대로다.
    """
    block = "a" * 1024
    eight = "{BLOCK}" * 8
    root = make_project(
        {
            "big.py": f'BLOCK = "{block}"\nEIGHT = f"{eight}"\n'
            'LIMIT = f"{EIGHT}{EIGHT}{EIGHT}{EIGHT}{EIGHT}{EIGHT}{EIGHT}{EIGHT}"\n'
            'OVER = f"{LIMIT}{BLOCK}"\nPLUS = LIMIT + "b"\n'
            "ITEMS = [EIGHT, EIGHT, EIGHT, EIGHT, EIGHT, EIGHT, EIGHT, EIGHT]\nFEW = [EIGHT, EIGHT]\n"
            f'LONG = "{"a" * (MAX_EVALUATED_LENGTH + 1)}"\n',
        }
    )
    symbols = SymbolTable(Project.open(root))
    index = symbols.index("big.py")
    assert index is not None
    evaluator = Evaluator(symbols)
    assert evaluator.value("big.py", index.values["LIMIT"]) == "a" * MAX_EVALUATED_LENGTH
    assert evaluator.value("big.py", index.values["OVER"]) is UNKNOWN
    assert evaluator.value("big.py", index.values["PLUS"]) is UNKNOWN
    # 목록 크기는 원소 수 8 + 문자 8 × 8 KiB라 상한을 넘는다.
    assert evaluator.value("big.py", index.values["ITEMS"]) is UNKNOWN
    assert evaluator.value("big.py", index.values["FEW"]) == ["a" * 8192] * 2
    assert evaluator.value("big.py", index.values["LONG"]) == "a" * (MAX_EVALUATED_LENGTH + 1)
    assert value_size([["ab", "c"], ["ab", "c"]]) == 2 + 2 * (2 + 3)
    assert value_size(["a" * 10], limit=5) == 6


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("'name'", "name"),
        ("-1", -1),
        ("['get', 'post']", ["get", "post"]),
        ("{'detail': True, 'methods': ('get',)}", {"detail": True, "methods": ("get",)}),
        ("None", None),
        ("...", ...),
        ("NAME", ...),
        ("'a' + NAME", ...),
        ("build()", ...),
        ("[NAME]", ...),
        # 해시할 수 없는 키는 literal_eval에서 TypeError다.
        ("{[1]: 2}", ...),
    ],
)
def test_literal_value_reads_only_literals(source: str, expected: object) -> None:
    """Django·DRF 선언 해석이 함께 쓰는 리터럴 판독은 이름을 따라가지 않고 리터럴이 아니면 `...`를 돌려준다."""
    assert literal_value(ast.parse(source, mode="eval").body) == expected


def test_literal_value_bounds_missing_and_deep_nodes() -> None:
    """없는 인자와 재귀 한도를 넘는 리터럴도 예외 없이 `...`로 낸다."""
    deep = ast.parse("[" * 50 + "]" * 50, mode="eval").body
    for _ in range(sys.getrecursionlimit()):
        deep = ast.List(elts=[deep], ctx=ast.Load())
    assert literal_value(None) is ...
    assert literal_value(deep) is ...


def test_evaluator_does_not_evaluate_rebound_module_names(make_project: Callable[[dict[str, str]], Path]) -> None:
    """두 번 이상 묶이거나 블록 안에서 묶이거나 `global`·이름공간·제자리 변경으로 바뀌는 모듈 이름은 상수가 아니다.

    마지막 무조건 대입을 상수로 써서 `PREFIX += "v2/"`·조건부 재대입·두 대입 사이의 사용·`.append`·함수의
    `global` 재대입을 무시했다. import·star import·모듈 별칭으로 거쳐 간 모듈이 이름을 다시 묶어도 같다. 한 번만
    무조건 대입한 이름은 그대로 상수다.
    """
    root = make_project(
        {
            "consts.py": """
                import os
                PREFIX = "api/"
                PREFIX += "v2/"
                OTHER = "shop/"
                if os.environ.get("X"):
                    OTHER = "debug-shop/"
                ONLY_IF = "a"
                if os.environ.get("Y"):
                    ONLY_COND = "b"
                TWICE = "first/"
                USE_BETWEEN = TWICE + "x"
                TWICE = "second/"
                for LOOPED in ("a", "b"):
                    pass
                with open("f") as WITHED:
                    pass
                APPS = ["a"]
                APPS.append("b")
                ITEMS = ["a"]
                ITEMS[0] = "b"
                ONCE = "once/"
                COPIED = ONCE + "x"


                def rebind():
                    global GLOBALED
                    GLOBALED = "z"


                GLOBALED = "y"
            """,
            "other_consts.py": 'ONCE = "other/"\n',
            "fallback.py": 'try:\n    from consts import ONCE as BASE\nexcept ImportError:\n    BASE = "fallback/"\n',
            "importer.py": "import consts\nfrom consts import ONCE, PREFIX\nfrom fallback import BASE\n",
            "star_base.py": 'SHARED = "base/"\nGROWN = "base/"\nAPPENDED = ["base"]\n',
            "star_user.py": """
                import os
                from star_base import *
                if os.environ.get("X"):
                    SHARED = "override/"
                GROWN += "more/"
                APPENDED.append("more")
            """,
            "alias.py": """
                import os
                if os.environ.get("X"):
                    import consts as chosen
                else:
                    import other_consts as chosen
            """,
            "namespace.py": 'NAME = "a"\nglobals()["NAME"] = "b"\n',
            "pkg/__init__.py": "",
            "pkg/consts.py": 'ROOT = "root/"\n',
            "pkg/other.py": "",
            # 별칭 없는 `import pkg.x`는 몇 번이든 같은 패키지 객체를 묶는다. 조건부 대입은 다시 묶는다.
            "dotted.py": "import pkg.consts\nimport pkg.other\n",
            "dotted_rebound.py": "import os\nimport pkg.consts\nif os.environ.get('X'):\n    pkg = None\n",
        }
    )
    evaluator = Evaluator(SymbolTable(Project.open(root)))
    constant = {
        ("consts.py", "ONCE"): "once/",
        ("consts.py", "COPIED"): "once/x",
        ("consts.py", "ONLY_IF"): "a",
        ("importer.py", "ONCE"): "once/",
        ("importer.py", "consts.ONCE"): "once/",
        ("star_base.py", "SHARED"): "base/",
        ("other_consts.py", "ONCE"): "other/",
        ("dotted.py", "pkg.consts.ROOT"): "root/",
    }
    for (path, source), expected in constant.items():
        assert evaluator.value(path, _expr(source)) == expected, (path, source)
    unknown = [
        ("consts.py", name)
        for name in ("PREFIX", "OTHER", "ONLY_COND", "TWICE", "USE_BETWEEN", "LOOPED", "WITHED", "APPS", "ITEMS")
    ]
    unknown += [("consts.py", "GLOBALED"), ("fallback.py", "BASE"), ("importer.py", "PREFIX")]
    unknown += [("importer.py", "consts.PREFIX"), ("importer.py", "BASE"), ("alias.py", "chosen.ONCE")]
    unknown += [("star_user.py", name) for name in ("SHARED", "GROWN", "APPENDED")]
    unknown += [("namespace.py", "NAME"), ("dotted_rebound.py", "pkg.consts.ROOT")]
    for path, source in unknown:
        assert evaluator.value(path, _expr(source)) is UNKNOWN, (path, source)


#: 재대입 판정을 고정하는 모듈 수준 문장 틀이다(이름 → 문장, `{a}`·`{b}`는 값 식). `ONCE`만 정적 값으로 증명된다.
_REBINDING_PATTERNS = {
    "ONCE": "ONCE = {a}\n",
    "TWICE": "TWICE = {a}\nTWICE = {b}\n",
    "GROWN": "GROWN = {a}\nGROWN += {b}\n",
    "BRANCHED": "BRANCHED = {a}\nif FLAG:\n    BRANCHED = {b}\n",
    "ONLY_BRANCH": "if FLAG:\n    ONLY_BRANCH = {a}\n",
    "TRIED": "TRIED = {a}\ntry:\n    TRIED = {b}\nexcept ValueError:\n    pass\n",
    "LOOPED": "LOOPED = {a}\nfor _item in ():\n    LOOPED = {b}\n",
    "WHILED": "WHILED = {a}\nwhile FLAG:\n    WHILED = {b}\n    break\n",
    "WITHED": "WITHED = {a}\nwith FLAG:\n    WITHED = {b}\n",
    "MATCHED": "MATCHED = {a}\nmatch FLAG:\n    case 1:\n        MATCHED = {b}\n",
    "GLOBALED": "GLOBALED = {a}\n\n\ndef rebind():\n    global GLOBALED\n    GLOBALED = {b}\n",
}


def _rebinding_module(first: str, second: str, namespace: bool = False) -> str:
    """재대입 사례를 모두 담은 모듈 소스를 만든다.

    Args:
        first: `{a}` 자리 값 식.
        second: `{b}` 자리 값 식.
        namespace: 모듈이 `globals()`로 이름공간을 다시 쓰는지.

    Returns:
        모듈 소스.
    """
    lines = ["FLAG = 0\n\n\nclass First:\n    pass\n\n\nclass Second:\n    pass\n"]
    lines += [pattern.format(a=first, b=second) for pattern in _REBINDING_PATTERNS.values()]
    if namespace:
        lines.append('globals()["FLAG"] = 1\n')
    return "\n".join(lines)


@pytest.mark.parametrize("namespace", [False, True], ids=["plain", "namespace"])
def test_rebinding_judgment_matches_graph_and_client(
    make_project: Callable[[dict[str, str]], Path], namespace: bool
) -> None:
    """상수 평가(routes·schema), graph 해석기, client 모듈 상수 증명이 같은 이름을 정적 값으로 본다.

    세 구현은 따로 있다(graph는 함수 범위까지 다루는 자기 묶음 수집기, client와 상수 평가는 `source.bindings`). 두 번
    이상 묶임(`+=` 포함), 조건·반복·예외·`with`·`match` 블록 안의 묶음, 함수의 `global` 재대입, 이름공간 쓰기에서
    판정이 갈라지지 않게 고정한다. graph는 클래스 값, 나머지는 문자열 값으로 본다.
    """
    root = make_project(
        {
            "strings.py": _rebinding_module('"/a"', '"/b"', namespace),
            "classes.py": _rebinding_module("First", "Second", namespace),
        }
    )
    project = Project.open(root)
    symbols = SymbolTable(project)
    evaluator = Evaluator(symbols)
    resolver = Resolver(DefinitionIndex(project, symbols, include_tests=False), symbols)
    constants = ModuleConstants(symbols, set(), set())
    for name in _REBINDING_PATTERNS:
        expected = name == "ONCE" and not namespace
        evaluated = evaluator.value("strings.py", _expr(name)) is not UNKNOWN
        graph = isinstance(resolver.module_name_value("classes.py", name, 0), ClassValue)
        client = isinstance(symbols.resolve_name("strings.py", name), ValueSymbol) and constants.proven(
            "strings.py", name
        )
        assert (evaluated, graph, client) == (expected, expected, expected), name


def test_module_bindings_marks_blocks_globals_and_in_place_changes() -> None:
    """묶음 수와 함께 블록 안 묶음, `global` 선언, 제자리 변경(변경 메서드·첨자 쓰기·지우기) 대상을 모은다."""
    source = """
import os
import os.path
import json as encoder
A = 1
if os.environ.get("X"):
    B = 2
    import json
for C in ():
    pass
D = [1]
D.append(2)
E = {}
E["k"] = 1
del E["k"]
F = [[1]]
F[0].append(2)
G = "a"
UPPER = G.upper()


def change(items):
    global H
    items.extend([1])


def typed(a=(d := 1)) -> (ret := int):
    pass


H = 0
X: int
[y := 1 for _ in ()]
[q for q in ()]
f = lambda x=(e := 2): x
try:
    pass
except ValueError as error:
    pass
match A:
    case [first, *rest]:
        pass
    case {"k": value, **others}:
        pass
"""
    summary = module_bindings(ast.parse(source))
    assert summary.counts["A"] == 1
    assert summary.counts["G"] == 1
    # 값 없는 주석 선언, 컴프리헨션 안 바다코끼리, 정의 시점 식(기본값·반환 주석·람다 기본값)의 바다코끼리도 모듈
    # 묶음으로 센다(client 상수 증명과 같은 셈). 바다코끼리 없는 컴프리헨션의 반복 변수는 세지 않는다.
    assert [summary.counts[name] for name in ("X", "y", "d", "ret", "e", "f", "typed")] == [1] * 7
    assert summary.counts["q"] == 0
    # 별칭 없는 `import os`·`import os.path`는 둘 다 묶음으로 세되 같은 패키지 객체를 묶는 import로도 센다.
    assert (summary.counts["os"], summary.package_imports["os"]) == (2, 2)
    assert (summary.counts["encoder"], summary.package_imports["encoder"]) == (1, 0)
    assert summary.nested == {"B", "json", "C", "error", "first", "rest", "value", "others"}
    assert summary.mutated == {"D", "E", "F", "items"}
    assert summary.declared_globals == {"H"}


def test_symbol_resolution(make_project: Callable[[dict[str, str]], Path]) -> None:
    """import·상대 import·하위 모듈·클래스 멤버·외부 이름을 해석한다."""
    root = make_project(
        {
            "pkg/__init__.py": "from .views import view as exported\n",
            "pkg/views.py": "def view():\n    pass\n\n\nclass K:\n    pass\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/leaf.py": (
                "import pkg.views\nimport os.path as osp\nfrom .. import views\nfrom ... import nothing\nvalue = 1\n"
            ),
        }
    )
    symbols = SymbolTable(Project.open(root))
    leaf = "pkg/sub/leaf.py"
    assert isinstance(symbols.resolve_expr(leaf, _expr("views.view")), ProjectSymbol)
    assert isinstance(symbols.resolve_expr(leaf, _expr("pkg.views")), ModuleSymbol)
    assert isinstance(symbols.resolve_expr(leaf, _expr("pkg.views.K.as_view")), MemberSymbol)
    assert symbols.resolve_expr(leaf, _expr("osp.join")) == ExternalSymbol("os.path.join")
    assert isinstance(symbols.resolve_expr(leaf, _expr("value")), ValueSymbol)
    assert symbols.resolve_expr(leaf, _expr("pkg.exported")) is not None
    assert symbols.resolve_expr(leaf, _expr("pkg.missing")) is None
    assert symbols.resolve_expr(leaf, _expr("nothing")) is None
    assert symbols.resolve_expr(leaf, _expr("call()")) is None
    assert symbols.index("missing.py") is None
    module = symbols.index(leaf)
    assert module is not None
    assert absolute_module(module.module, None, 5) is None


def test_module_resolution_is_remembered_per_module_roots(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 이름을 다시 풀 때는 파일시스템을 다시 보지 않고(외부 이름의 None 포함), 모듈 루트가 바뀌면 다시 푼다.

    이름이 소스에 나올 때마다 모듈 루트 수 × 후보 2개의 성분별 lstat을 되풀이해, `manage.py`가 여러 개인 저장소에서
    schema·routes·graph가 수 배~수십 배 느려지던 문제다.
    """
    root = make_project({"pkg/__init__.py": "", "pkg/mod.py": "", "lib/app.py": ""})
    project = Project.open(root)
    checked: list[str] = []
    checked_stat = project._checked_stat

    def counting(relative: str) -> os.stat_result:
        checked.append(relative)
        return checked_stat(relative)

    monkeypatch.setattr(project, "_checked_stat", counting)
    assert project.resolve_module("pkg.mod") == "pkg/mod.py"
    assert project.resolve_module("django.db") is None
    assert project.resolve_module("app") is None
    first = len(checked)
    assert first > 0
    for _ in range(100):
        assert project.resolve_module("pkg.mod") == "pkg/mod.py"
        assert project.resolve_module("django.db") is None
        assert project.resolve_module("app") is None
    assert len(checked) == first
    # 호출자가 open 뒤 모듈 루트를 제자리에서 바꾸면 기억한 결과를 버리고 새 루트 순서로 다시 푼다.
    project.module_roots.insert(0, "lib")
    assert project.resolve_module("app") == "lib/app.py"
    assert project.resolve_module("pkg.mod") == "pkg/mod.py"
    assert len(checked) > first


def _count_checked_stats(project: Project, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """프로젝트의 성분별 lstat 검사(`_checked_stat`) 호출을 인자 경로 순서대로 모은다.

    Args:
        project: 프로젝트.
        monkeypatch: pytest monkeypatch.

    Returns:
        호출마다 인자 경로가 쌓이는 목록.
    """
    checked: list[str] = []
    checked_stat = project._checked_stat

    def counting(relative: str) -> os.stat_result:
        checked.append(relative)
        return checked_stat(relative)

    monkeypatch.setattr(project, "_checked_stat", counting)
    return checked


def test_unique_names_do_not_recheck_every_module_root(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """고유 이름마다 모든 모듈 루트를 다시 lstat하지 않는다. 루트 항목은 처음 닿을 때 한 번만 읽는다.

    빈 `manage.py`를 수천 개 두면 고유 외부 이름 하나마다 모듈 루트 수 × 후보 2개의 성분별 lstat을 해, graph·reach·
    impact가 루트 수에 비례해 느려지던 문제다(루트 4,096개에서 고유 이름 400개를 푸는 graph가 66.8초).
    """
    files = {f"sites/s{index}/manage.py": "" for index in range(1000)}
    files["app/__init__.py"] = ""
    project = Project.open(make_project(files))
    checked = _count_checked_stats(project, monkeypatch)
    assert project.resolve_module("ext0.sub") is None
    first = len(checked)
    assert first <= len(project.module_roots)
    for number in range(1, 200):
        assert project.resolve_module(f"ext{number}.sub") is None
    assert len(checked) == first
    assert project.resolve_module("app") == "app/__init__.py"


class _CountingRoots(list[str]):
    """모듈 루트 목록을 훑은 횟수를 센다(`for`·`list()`·`any()`가 부르는 `__iter__`)."""

    def __init__(self, roots: list[str]) -> None:
        """목록을 감싼다.

        Args:
            roots: 모듈 루트.
        """
        super().__init__(roots)
        self.scans = 0

    def __iter__(self) -> Iterator[str]:
        """훑을 때마다 센다.

        Returns:
            원소 반복자.
        """
        self.scans += 1
        return super().__iter__()


def _reference_module_name(roots: list[str], relative: str) -> str:
    """파일마다 모든 모듈 루트와 비교하던 옛 모듈 이름 규칙이다(파일을 담은 가장 긴 루트 기준, 결과 대조용).

    Args:
        roots: 모듈 루트.
        relative: 프로젝트 상대 `.py` 경로.

    Returns:
        점으로 이은 모듈 이름.
    """
    path = PurePosixPath(relative)
    best = ""
    for root in roots:
        if root and relative.startswith(root + "/") and len(root) > len(best):
            best = root
    inner = path.relative_to(best) if best else path
    parts = list(inner.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def test_module_names_do_not_scan_every_module_root(make_project: Callable[[dict[str, str]], Path]) -> None:
    """모듈 이름은 파일을 담은 가장 긴 모듈 루트 기준 그대로이고, 파일마다 모듈 루트 전부를 훑지 않는다.

    파싱한 파일마다 모든 루트와 문자열을 비교해, `manage.py`가 수천 개인 저장소에서 파일 수 × 루트 수가 들었다.
    """
    project = Project.open(make_project({"app.py": ""}))
    roots = ["a", "a/b", "", "src", "a/b/", "x//y", *(f"sites/s{index}" for index in range(500))]
    paths = [
        "a/b/c/d.py",
        "a/b.py",
        "a/b/__init__.py",
        "a/b/c/__init__.py",
        "a//b.py",
        "x//y/z.py",
        "./a/b.py",
        "src/pkg/mod.py",
        "sites/s499/app/models.py",
        "sites/s4990/app.py",
        "top.py",
        "__init__.py",
    ]
    counting = _CountingRoots(roots)
    project.module_roots = counting
    names = {path: project.module_name(path) for path in paths * 20}
    assert names == {path: _reference_module_name(roots, path) for path in paths}
    assert counting.scans <= 1


def test_test_import_judgment_checks_module_roots_once(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """테스트 소스 import 판정은 모듈 루트가 테스트 디렉터리 아래인지를 import 이름마다가 아니라 한 번만 본다."""
    files = {f"sites/s{index}/manage.py": "" for index in range(50)}
    files["tests/test_app.py"] = ""
    files["app.py"] = "".join(f"import ext{number}\n" for number in range(100))
    project = Project.open(make_project(files))
    imports = scan_modules(DefinitionIndex(project, SymbolTable(project), False)).imports
    calls: list[str] = []
    is_test = exposure.is_test_path

    def counting(relative: str) -> bool:
        calls.append(relative)
        return is_test(relative)

    monkeypatch.setattr(exposure, "is_test_path", counting)
    # `tests_joined`를 테스트 모듈로 가져오면 pytest가 테스트로 수집하므로 모듈을 거쳐 부른다.
    assert not exposure.tests_joined(project, imports)
    assert len(imports) == 100
    assert len(calls) < 2 * (len(project.python_files()) + len(imports) + len(project.module_roots))


@pytest.mark.parametrize("count", [255, 256])
def test_only_the_shallowest_manage_directories_become_module_roots(
    make_project: Callable[[dict[str, str]], Path], count: int
) -> None:
    """`manage.py` 디렉터리는 (깊이, 경로) 순으로 앞 256개만 모듈 루트로 쓰고, 나머지 수를 센다.

    루트 항목 색인 뒤에도 모든 루트에 같은 첫 성분(`pkg/`)을 두면 이름 하나마다 모든 루트를 봐야 해서, 루트 수에
    비례하는 비용이 남는다(루트 4,096개에서 82.2초). 그래서 루트 수 자체에 상한을 둔다.
    """
    files = {f"sites/s{index:03d}/manage.py": "" for index in range(count)}
    files.update({f"sites/s{index:03d}/pkg/__init__.py": "" for index in range(count)})
    files["manage.py"] = ""
    files["src/app.py"] = ""
    project = Project.open(make_project(files))
    kept = ["", *(f"sites/s{index:03d}" for index in range(count))][: project_module.MAX_MODULE_ROOTS]
    assert project.module_roots == [*kept, "src"]
    assert project.skipped_module_roots == count + 1 - len(kept)
    # 상한 값 자체를 고정한다: 루트의 manage.py까지 256개면 모두 쓰고, 257개면 하나를 뺀다.
    assert project.skipped_module_roots == (1 if count == 256 else 0)


def test_star_import_resolution(make_project: Callable[[dict[str, str]], Path]) -> None:
    """`from x import *`를 중첩해서 따라가고 `__all__`·밑줄 규칙과 외부·미확정 `*`의 가림을 지킨다.

    도그푸딩에서 `from .models import *` → 패키지 `__init__`의 `from .sites import *`가 풀리지 않아 모델·뷰 이름을
    잃었다.
    """
    root = make_project(
        {
            "pkg/__init__.py": "",
            "pkg/models/__init__.py": "from .sites import *\nfrom .racks import *\n",
            "pkg/models/sites.py": "__all__ = (\n    'Site',\n)\n\nclass Site:\n    pass\n\nclass Hidden:\n    pass\n",
            "pkg/models/racks.py": "class Rack:\n    pass\n\nclass _Private:\n    pass\n",
            "pkg/views.py": "from .models import *\n",
            "pkg/annotated.py": "__all__: list[str] = ['Box']\n\nclass Box:\n    pass\n",
            "pkg/grown.py": "__all__ = ['A']\n__all__ += ['B']\n\nclass A:\n    pass\n\nclass B:\n    pass\n",
            "pkg/uses.py": "from pkg.annotated import *\nfrom pkg.grown import *\n",
            "pkg/shadowed.py": "from pkg.models import *\nfrom os.path import *\n",
            "pkg/local_all.py": (
                "__all__ = ['Kept']\n\nclass Kept:\n    pass\n\n"
                "def helper():\n    __all__ = ['x']\n    return __all__.copy()\n"
            ),
            "pkg/uses_local_all.py": "from pkg.local_all import *\n",
            "pkg/cycle_a.py": "from pkg.cycle_b import *\n",
            "pkg/cycle_b.py": "from pkg.cycle_a import *\n",
        }
    )
    symbols = SymbolTable(Project.open(root))
    site = symbols.resolve_name("pkg/views.py", "Site")
    assert isinstance(site, ProjectSymbol)
    assert site.path == "pkg/models/sites.py"
    assert isinstance(symbols.resolve_name("pkg/views.py", "Rack"), ProjectSymbol)
    assert symbols.resolve_name("pkg/views.py", "Hidden") is None
    assert symbols.resolve_name("pkg/views.py", "_Private") is None
    # 확정하지 못한 `__all__`(`+=`)은 이름을 가릴 수 있어 그 앞의 `*`도 따라가지 않는다.
    assert symbols.resolve_name("pkg/uses.py", "Box") is None
    assert isinstance(symbols.resolve_name("pkg/annotated.py", "Box"), ProjectSymbol)
    # 외부 모듈의 `*`가 뒤에 있으면 앞의 프로젝트 이름도 가려질 수 있어 모른다.
    assert symbols.resolve_name("pkg/shadowed.py", "Site") is None
    assert symbols.resolve_name("pkg/cycle_a.py", "Nothing") is None
    # 함수 안의 지역 `__all__`은 모듈의 내보내기를 흐리지 않는다.
    assert isinstance(symbols.resolve_name("pkg/uses_local_all.py", "Kept"), ProjectSymbol)
    assert symbols.star_blocked("pkg/shadowed.py", "Site")
    assert symbols.star_blocked("pkg/uses.py", "Box")
    assert not symbols.star_blocked("pkg/views.py", "len")
    assert not symbols.star_blocked("missing.py", "x")


def test_read_failures_and_limits(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """크기·인코딩·문법 실패는 이유로 바뀌고 순회 상한을 지킨다."""
    root = make_project({"ok.py": "x = 1\n", "big.py": "value = 123456789\n", "bad.py": "def (:\n", "a/b/c.py": ""})
    (root / "latin.py").write_bytes(b"x = '\xff'\n")
    (root / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
    monkeypatch.setattr(project_module, "MAX_SOURCE_BYTES", 8)
    project = Project.open(root)
    assert isinstance(project.module("big.py"), ReadFailure)
    monkeypatch.setattr(project_module, "MAX_SOURCE_BYTES", 4 * 1024 * 1024)
    project = Project.open(root)
    assert isinstance(project.module("latin.py"), ReadFailure)
    assert isinstance(project.module("bad.py"), ReadFailure)
    assert isinstance(project.module("missing.py"), ReadFailure)
    assert isinstance(project.module("../escape.py"), ReadFailure)
    bom = project.module("bom.py")
    assert isinstance(bom, SourceModule) and bom.has_bom
    assert project.read_text("missing.txt") is None
    assert project.read_text("ok.py", limit=2) is None
    assert project.read_text("latin.py") is None
    assert project.resolve_module("not a module") is None
    monkeypatch.setattr(project_module, "MAX_SCAN_DEPTH", 0)
    capped = Project.open(root)
    assert capped.scan_capped and "a/b/c.py" not in capped.python_files()
    monkeypatch.setattr(project_module, "MAX_SCANNED_ENTRIES", 1)
    assert Project.open(root).scan_capped


def test_deeply_nested_modules_are_read_failures(make_project: Callable[[dict[str, str]], Path]) -> None:
    """구문 트리가 너무 깊은 모듈은 분석하지 않고 읽지 못한 소스(중첩 이유)로 돌린다.

    파싱은 되지만 재귀 방문자가 파이썬 재귀 한도를 넘겨 명령 전체가 내부 오류로 끝나던 모양이다.
    """
    root = make_project({"ok.py": "VALUE = 1 + 2\n", **{f"pkg/{name}": text for name, text in DEEP_SOURCES.items()}})
    project = Project.open(root)
    for name in DEEP_SOURCES:
        failure = project.module(f"pkg/{name}")
        assert isinstance(failure, ReadFailure), name
        assert failure.reason == "source nesting too deep to parse"
    assert isinstance(project.module("ok.py"), SourceModule)


def test_nesting_limit_boundary(make_project: Callable[[dict[str, str]], Path]) -> None:
    """상한 깊이의 모듈은 그대로 파싱하고, 한 단계 더 깊으면 읽지 못한 소스다."""
    edge = "VALUE = " + " + ".join(["1"] * (MAX_AST_DEPTH - 2)) + "\n"
    over = "VALUE = " + " + ".join(["1"] * (MAX_AST_DEPTH - 1)) + "\n"
    assert (tree_depth(edge), tree_depth(over)) == (MAX_AST_DEPTH, MAX_AST_DEPTH + 1)
    project = Project.open(make_project({"edge.py": edge, "over.py": over}))
    assert isinstance(project.module("edge.py"), SourceModule)
    assert isinstance(project.module("over.py"), ReadFailure)
    tree = ast.parse(edge)
    assert not exceeds_depth(tree) and exceeds_depth(tree, MAX_AST_DEPTH - 1)
    assert not exceeds_depth(ast.parse(""), 1)


def test_tree_overflow_counts_every_node_and_keeps_the_depth_rule() -> None:
    """노드 수는 `ast.walk`가 내는 모든 노드(문맥 잎 포함)이고 상한과 같으면 넘지 않는다. 중첩 판정은 `exceeds_depth`와
    같고, 상한 층에 닿기 전에 노드 수가 넘으면 노드 쪽으로 멈춘다."""
    tree = ast.parse("DATA = [1, 2, 3, 4, 5]\nNAME = DATA\n")
    nodes = len(list(ast.walk(tree)))
    assert tree_overflow(tree, MAX_AST_DEPTH, nodes) is None
    assert tree_overflow(tree, MAX_AST_DEPTH, nodes - 1) == "nodes"
    deep = ast.parse("VALUE = " + " + ".join(["1"] * (MAX_AST_DEPTH - 1)) + "\n")
    assert tree_overflow(deep, MAX_AST_DEPTH, 10**9) == "depth"
    assert tree_overflow(deep, MAX_AST_DEPTH, 10) == "nodes"
    assert tree_overflow(ast.parse(""), 1, 1) is None


def test_extraction_commands_use_a_higher_nesting_limit(make_project: Callable[[dict[str, str]], Path]) -> None:
    """routes·schema가 여는 프로젝트(`MAX_EXTRACTION_AST_DEPTH`)는 graph 상한보다 깊은 모듈도 그 상한까지 파싱한다.

    graph 상한(128)을 모든 명령에 걸면 routes·schema·client가 문제없이 분석하던 모듈까지 빼고 문서 전체 한계를 달았다.
    """
    middle = "VALUE = " + " + ".join(["1"] * (MAX_AST_DEPTH + 50)) + "\n"
    edge = "VALUE = " + " + ".join(["1"] * (MAX_EXTRACTION_AST_DEPTH - 2)) + "\n"
    over = "VALUE = " + " + ".join(["1"] * (MAX_EXTRACTION_AST_DEPTH - 1)) + "\n"
    assert (tree_depth(edge), tree_depth(over)) == (MAX_EXTRACTION_AST_DEPTH, MAX_EXTRACTION_AST_DEPTH + 1)
    root = make_project({"middle.py": middle, "edge.py": edge, "over.py": over})
    graph_project = Project.open(root)
    assert isinstance(graph_project.module("middle.py"), ReadFailure)
    extraction_project = Project.open(root, MAX_EXTRACTION_AST_DEPTH)
    assert isinstance(extraction_project.module("middle.py"), SourceModule)
    assert isinstance(extraction_project.module("edge.py"), SourceModule)
    failure = extraction_project.module("over.py")
    assert isinstance(failure, ReadFailure)
    assert failure.reason == "source nesting too deep to parse"


def test_chain_guard_counts_one_logical_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """3.10 파서 보호는 논리 줄 하나의 사슬 토큰만 세고, 사슬 문자 총수가 상한 이하면 토큰으로 나누지 않는다."""
    many_lines = "x = a.b\n" * (MAX_CHAIN_TOKENS + 1)
    one_line = "x = (\n" + "a.b +\n" * (MAX_CHAIN_TOKENS // 2 + 1) + "a)\n"
    assert syntax._longest_chain(many_lines) == 1
    assert syntax._longest_chain(one_line) > MAX_CHAIN_TOKENS
    assert syntax._longest_chain("x = '...'\n") == 3
    assert syntax._longest_chain("x = (" + "a." * (MAX_CHAIN_TOKENS + 1)) > MAX_CHAIN_TOKENS
    assert syntax._longest_chain(many_lines + "y = (\n") == 1
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", False)
    assert not syntax.chain_too_long(one_line)
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    assert syntax.chain_too_long(one_line) and not syntax.chain_too_long(many_lines)


def test_chain_guard_counts_chains_inside_fstrings(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """f-string 치환 필드 안의 사슬도 그 논리 줄의 사슬 토큰으로 센다(바깥 사슬과 합친다).

    3.10의 tokenize는 f-string 전체를 STRING 토큰 하나로 내서 연산자 토큰만 세면 `f"{a+a+…}"`가 0이었다. 그래서
    20만 항짜리 f-string 모듈이나 그런 문자열 주석 하나로 `ast.parse`가 세그폴트(종료 139)했다.
    """
    half = MAX_CHAIN_TOKENS // 2 + 1
    assert syntax._longest_chain('x = f"{' + "a+" * (MAX_CHAIN_TOKENS + 1) + 'a}"\n') > MAX_CHAIN_TOKENS
    assert syntax._longest_chain("x = (" + "a+" * half + 'f"{' + "a+" * half + 'a}")\n') > MAX_CHAIN_TOKENS
    assert syntax._longest_chain('x = f"{a.b}"\n' * (MAX_CHAIN_TOKENS + 1)) == 1
    flat = 'f"{[' + "a.b, " * (MAX_CHAIN_TOKENS + 1) + ']}"'
    root = make_project({"flat.py": f"VALUE = {flat}\n"})
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", False)
    assert isinstance(Project.open(root).module("flat.py"), SourceModule)
    assert isinstance(parse_annotation(flat), ast.JoinedStr)
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    failure = Project.open(root).module("flat.py")
    assert isinstance(failure, ReadFailure)
    assert failure.reason == "source nesting too deep to parse"
    assert parse_annotation(flat) is None


def test_chain_guard_rejects_before_parsing(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.10에서는 사슬 토큰이 상한을 넘는 논리 줄이 있으면 파싱하지 않고 중첩 실패로 남긴다(얕은 목록도 포함)."""
    root = make_project({"flat.py": "VALUE = [" + "a.b, " * (MAX_CHAIN_TOKENS + 1) + "]\n"})
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", False)
    assert isinstance(Project.open(root).module("flat.py"), SourceModule)
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    failure = Project.open(root).module("flat.py")
    assert isinstance(failure, ReadFailure)
    assert failure.reason == "source nesting too deep to parse"


def test_parse_annotation_turns_every_failure_into_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """문자열 주석 파싱은 문법 오류·NUL(3.10은 ValueError)·너무 깊은 식·3.10 긴 사슬을 모두 None으로 돌린다."""
    assert isinstance(parse_annotation("Mapped[str]"), ast.Subscript)
    assert isinstance(parse_annotation("Client" + " | None" * 10), ast.BinOp)
    assert parse_annotation("Mapped[str]\x00") is None
    assert parse_annotation("Mapped[") is None
    assert parse_annotation(" + ".join(["str"] * 50_000)) is None
    assert parse_annotation("Client" + " | None" * 600) is None
    flat = "Literal[" + "a.b, " * (MAX_CHAIN_TOKENS + 1) + "]"
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", False)
    assert isinstance(parse_annotation(flat), ast.Subscript)
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    assert parse_annotation(flat) is None


def test_contract_safe_text_rejects_every_forbidden_character() -> None:
    """교환 형식이 식별자에 금지하는 문자(C0·C1 제어 문자와 NEL, U+2028/2029, 짝 없는 서러게이트)를 모두 거부한다.

    같은 판정이 여러 곳에 따로 있어 서러게이트를 빠뜨린 사본이 있었다. 한 벌로 모은 판정이 모든 종류를 거르는지
    고정한다.
    """
    for character in ("\x00", "\x1b", "\x1f", "\x7f", "\x85", "\x9f", "\u2028", "\u2029", "\ud800", "\udfff"):
        assert not is_contract_safe(f"app/m{character}.py"), ascii(character)
    assert not is_contract_safe("")
    for text in ("app/views.py#index", "caf\u00e9\u00a0x", "emoji\U0001f600", "\ue000\u200b"):
        assert is_contract_safe(text), ascii(text)


def test_test_path_rules() -> None:
    """테스트 소스 경로 규칙."""
    assert is_test_path("tests/urls.py") and is_test_path("app/test_views.py") and is_test_path("a/x_test.py")
    assert is_test_path("app/tests.py") and is_test_path("conftest.py") and is_test_path("test/x.py")
    assert not is_test_path("app/views.py") and not is_test_path("contest.py")


def test_python_module_entry_point() -> None:
    """`python -m pythograph --version`이 콘솔 스크립트와 같게 동작한다."""
    result = subprocess.run(
        [sys.executable, "-m", "pythograph", "--version"], capture_output=True, text=True, check=False, cwd=REPOSITORY
    )
    assert result.returncode == 0 and result.stdout.strip()


def test_names_with_forbidden_characters_are_skipped(make_project: Callable[[dict[str, str]], Path]) -> None:
    """이름(디렉터리 성분 포함)에 계약 금지 문자가 든 파이썬 파일은 건너뛰고 센다. 소스가 아닌 파일은 세지 않는다.

    파일 경로는 모든 사실의 `location.path`와 심볼 id가 되므로, 그런 파일 하나로 isthmus가 문서 전체를 거부했다.
    """
    root = make_project({"ok.py": "", "pkg/__init__.py": ""})
    names = ["pkg/bel\x07.py", "pkg/nel\x85.py", "pkg/ls\u2028.py", "pkg/ev\x1b[31mil.py", "odd\x07dir/mod.py"]
    try:
        for name in [*names, "pkg/notes\x07.txt"]:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("", encoding="utf-8")
    except OSError:
        pytest.skip("the filesystem rejects control characters in file names")
    project = Project.open(root)
    assert project.python_files() == ["ok.py", "pkg/__init__.py"]
    assert (project.forbidden_names, project.unencodable_names) == (len(names), 0)


def test_unencodable_file_names_are_skipped(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UTF-8로 쓸 수 없는 파일 이름은 건너뛰고 센다(교환 형식이 쓸 수 없는 문자열이 새지 않게)."""
    assert project_module._is_encodable("\udcff") is False
    root = make_project({"ok.py": "", "odd.py": ""})
    monkeypatch.setattr(project_module, "_is_encodable", lambda text: not text.startswith("odd"))
    project = Project.open(root)
    assert project.python_files() == ["ok.py"] and project.unencodable_names == 1
