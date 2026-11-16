"""소스 계층(프로젝트 읽기·상수 평가·이름 해석) 테스트."""

from __future__ import annotations

import ast
import io
import itertools
import os
import subprocess
import sys
import threading
import tokenize
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

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


#: 1 MiB 스레드에서 긴 사슬 주석·모듈과 넓지만 얕은 모듈을 읽고 결과를 한 줄로 내는 별도 프로세스 스크립트다(인자:
#: 프로젝트). Windows의 python.exe 주 스레드 스택(2,000,000 B)보다 작은 스택으로 Windows 조건을 POSIX에서도 만든다.
_SMALL_STACK_PARSES = r"""
import sys
import threading
from pathlib import Path

from pythograph.source.project import Project
from pythograph.source.syntax import parse_annotation

results = []


def work():
    project = Project.open(Path(sys.argv[1]))
    results.append(str(parse_annotation(" + ".join(["Author"] * 50_000)) is None))
    for name in ("deep.py", "wide.py"):
        module = project.module(name)
        results.append(getattr(module, "reason", type(module).__name__))


threading.stack_size(1024 * 1024)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print("|".join(results))
"""


def test_long_chains_parse_without_overflowing_a_small_stack(make_project: Callable[[dict[str, str]], Path]) -> None:
    """사슬 상한(5만 토큰) 안의 긴 사슬도 작은 스택(1 MiB 스레드, Windows 주 스레드 2 MB 미만)에서 프로세스를 죽이지
    않는다.

    3.10의 `ast.parse`는 C 트리를 파이썬 객체로 바꾸는 재귀(`ast2obj`)에 깊이 검사가 없어 한 단계에 약 64 B의 C 스택을
    쓴다(3.10.20 실측: 1 MiB 스레드는 약 1.6만, 2,000,000 B는 약 3.2만 단계에서 죽는다). 사슬 상한은 Linux 주
    스레드(8 MiB)로 정했으므로 Windows 3.10 CI에서 4만 9천 항 문자열 주석이 "Windows fatal exception: stack overflow"로
    프로세스를 끝냈다.
    이제 긴 사슬은 넉넉한 전용 스택에서 파싱해 깊은 모듈은 지금처럼 중첩 실패(한계)로 남고, 넓지만 얕은 모듈(음수 3만
    개 목록)은 그대로 분석한다. 3.11부터는 파서가 RecursionError를 내므로 같은 결과다.
    """
    root = make_project(
        {
            "deep.py": "VALUE = " + " + ".join(["a"] * 30_000) + "\n",
            "wide.py": "DATA = [" + "-1, " * 30_000 + "]\n",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _SMALL_STACK_PARSES, str(root)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout == "True|source nesting too deep to parse|SourceModule\n"


@pytest.fixture
def fresh_parse_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """재사용하는 전용 파싱 스레드를 비워, 이 테스트가 만든 스레드만 보이게 한다(테스트가 끝나면 원래 것으로 돌린다)."""
    monkeypatch.setattr(syntax, "_parse_workers", syntax._ParseWorkers())


def _record_thread_starts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """`syntax`가 시작하는 스레드마다 그때의 스택 설정을 기록하게 하고 그 목록을 돌려준다."""
    started: list[int] = []

    class RecordingThread(threading.Thread):
        """시작할 때의 스택 설정을 기록하는 스레드다."""

        def start(self) -> None:
            started.append(threading.stack_size())
            super().start()

    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=RecordingThread, stack_size=threading.stack_size))
    return started


@pytest.mark.usefixtures("fresh_parse_workers")
def test_long_chains_use_a_dedicated_stack_and_count_its_failures(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.10에서 작은 스택 상한을 넘는 사슬은 전용 스택 스레드에서 같은 결과로 파싱하고, 그 스레드를 만들 수 없으면
    그 이유("could not start a parser thread …")의 읽지 못한 소스·모르는 주석으로 센다. 조용히 버리지 않고, 파서의
    깊이 한도("source nesting too deep to parse")로 가장하지도 않는다."""
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    monkeypatch.setattr(syntax, "SMALL_STACK_CHAIN_TOKENS", 10)
    chain = " + ".join(["a"] * 40)
    root = make_project({"chain.py": f"VALUE = {chain}\n", "broken.py": f"VALUE = ({chain}\n", "short.py": "x = a.b\n"})
    submitted: list[int] = []
    submit = syntax._ParseWorker.run

    def recording_submit(worker: syntax._ParseWorker, work: Callable[[], object]) -> object:
        """전용 스레드에 넘긴 작업마다 그 스레드의 스택 크기를 기록한다."""
        submitted.append(worker.stack)
        return submit(worker, work)

    monkeypatch.setattr(syntax._ParseWorker, "run", recording_submit)
    unstarted = syntax._ParseWorkers()
    started = _record_thread_starts(monkeypatch)
    before = threading.stack_size()
    assert isinstance(Project.open(root).module("chain.py"), SourceModule)
    failure = Project.open(root).module("broken.py")
    assert isinstance(failure, ReadFailure)
    assert failure.reason == "syntax error or syntax newer than the running Python"
    assert isinstance(parse_annotation(chain), ast.BinOp)
    assert isinstance(Project.open(root).module("short.py"), SourceModule)
    assert submitted == [syntax.MIN_PARSE_STACK_BYTES] * 3
    assert started == [syntax.MIN_PARSE_STACK_BYTES]
    assert threading.stack_size() == before

    class RefusedThread:
        """스레드를 만들 수 없는 환경을 흉내 낸다."""

        def __init__(self, **_keywords: object) -> None:
            """인자를 받기만 한다."""

        def start(self) -> None:
            """언제나 실패한다."""
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(syntax, "_parse_workers", unstarted)
    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=RefusedThread, stack_size=threading.stack_size))
    failure = Project.open(root).module("chain.py")
    assert isinstance(failure, ReadFailure)
    assert failure.reason == syntax.PARSE_STACK_UNAVAILABLE_REASON
    assert parse_annotation(chain) is None
    assert isinstance(Project.open(root).module("short.py"), SourceModule)
    assert threading.stack_size() == before


#: 1 MiB 스레드에서 f-string으로 파서 셋을 겹친 오른쪽 재귀 식을 주석이나 모듈로 읽고 결과를 내는 별도 프로세스
#: 스크립트다(인자: 경로 종류, 프로젝트). 3.12 전 파서는 f-string 치환 필드마다 새 파서를 만들고 깊이 한도(MAXSTACK, 약
#: 6,000단)를 처음부터 다시 세므로 `~` 5,900개짜리 파서 셋이 쌓인다.
_SMALL_STACK_FSTRINGS = r"""
import sys
import threading
from pathlib import Path

from pythograph.source.project import Project
from pythograph.source.syntax import parse_annotation

expression = "~" * 5900 + 'f"{' + "~" * 5900 + "f'{" + "~" * 5900 + "x}'}" + '"'
results = []


def work():
    if sys.argv[1] == "annotation":
        results.append(str(parse_annotation(expression) is None))
    else:
        root = Path(sys.argv[2])
        (root / "stacked.py").write_text("X = " + expression + "\n", encoding="utf-8")
        module = Project.open(root).module("stacked.py")
        results.append(getattr(module, "reason", type(module).__name__))


threading.stack_size(1024 * 1024)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print("|".join(results))
"""


@pytest.mark.parametrize(("path", "expected"), [("annotation", "True"), ("module", "source nesting too deep to parse")])
def test_stacked_fstring_parsers_do_not_overflow_a_small_stack(tmp_path: Path, path: str, expected: str) -> None:
    """f-string으로 겹친 파서들이 작은 스택(1 MiB 스레드, Windows 주 스레드 2,000,000 B 미만)을 넘기지 않는다.

    3.10·3.11은 f-string 치환 필드를 따로 만든 파서로 다시 파싱하고 그 파서의 깊이 한도를 처음부터 센다. 사슬 수로
    걸리지 않는 오른쪽 재귀(`~`·`not`·`-`·`lambda`·조건식·`**`)를 f-string 안에 겹치면 파서 하나마다 약 0.9 MiB
    (3.10.20 실측)가 쌓여, 17 KB 한 줄 소스로 프로세스가 죽었다(1 MiB·2,000,000 B 스레드 모두). 3.13은 파서 하나가
    MemoryError로 끝내지만 macOS 3.13의 1 MiB 스레드는 그 전에 죽었다(SIGBUS, 종료 -10). 이제 3.13까지 긴 소스는
    전용 스택에서 파싱해 모든 버전에서 중첩 실패(모듈)·모르는 주석으로 남는다.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SMALL_STACK_FSTRINGS, path, str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout == expected + "\n"


#: 1 MiB 스레드에서 괄호 199단을 f-string 4단(파서 다섯)에 겹친 2,319자 식을 주석이나 모듈로 읽는 별도 프로세스
#: 스크립트다(인자: 경로 종류, 프로젝트). 괄호 하나는 문법 규칙 여럿을 지나 약 1 KiB의 C 스택을 쓰고, 토크나이저의 괄호
#: 200단 한도는 파서마다 다시 센다(3.10·3.11).
_SMALL_STACK_BRACKETS = r"""
import sys
import threading
from pathlib import Path

from pythograph.source.project import Project
from pythograph.source.syntax import parse_annotation

expression = "x"
for quote in ("", '"' * 3, "'" * 3, '"', "'")[::-1]:
    core = "[" * 199 + "~" * 60 + expression + "]" * 199
    expression = core if not quote else "f" + quote + "{" + core + "}" + quote
results = []


def work():
    if sys.argv[1] == "annotation":
        results.append(str(parse_annotation(expression) is None))
    else:
        root = Path(sys.argv[2])
        (root / "brackets.py").write_bytes(("X = " + expression + "\n").encode())
        module = Project.open(root).module("brackets.py")
        results.append(getattr(module, "reason", type(module).__name__))


threading.stack_size(1024 * 1024)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print(len(expression), "|".join(results))
"""


@pytest.mark.parametrize("path", ["annotation", "module"])
def test_bracket_nested_fstrings_do_not_overflow_a_small_stack(tmp_path: Path, path: str) -> None:
    """괄호를 겹친 짧은 f-string 식(2,319자)도 작은 스택(1 MiB 스레드)을 넘기지 않는다.

    괄호는 한 글자에 약 1 KiB(3.10.20 실측)를 써서 "한 단계에 한 글자" 전제가 틀렸다. 3.10·3.11은 f-string마다 괄호
    200단 한도를 다시 세므로 파서 다섯이 괄호 199단씩을 쌓아 약 2 MiB가 들었고, 전에는 4,000자 미만이라 부른 스레드에서
    파싱해 죽었다. 결과는 파이썬 버전의 파서가 정한다: 3.12 이상은 f-string 괄호도 한 한도로 세어 문법 오류다.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SMALL_STACK_BRACKETS, path, str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    if path == "annotation":
        expected = "True"
    elif sys.version_info < (3, 12):
        expected = "source nesting too deep to parse"
    else:
        expected = "syntax error or syntax newer than the running Python"
    assert result.stdout == f"2319 {expected}\n"


#: 1 MiB 스레드에서 파서 하나의 깊이 한도 가까운 오른쪽 재귀 식(`~` 5,960개)을 주석이나 모듈로 읽는 별도 프로세스
#: 스크립트다(인자: 경로 종류, 프로젝트).
_SMALL_STACK_UNARY = r"""
import sys
import threading
from pathlib import Path

from pythograph.source.project import Project
from pythograph.source.syntax import parse_annotation

expression = "~" * 5960 + "x"
results = []


def work():
    if sys.argv[1] == "annotation":
        results.append(str(parse_annotation(expression) is None))
    else:
        root = Path(sys.argv[2])
        (root / "unary.py").write_bytes(("X = " + expression + "\n").encode())
        module = Project.open(root).module("unary.py")
        results.append(getattr(module, "reason", type(module).__name__))


threading.stack_size(1024 * 1024)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print("|".join(results))
"""


@pytest.mark.parametrize(("path", "expected"), [("annotation", "True"), ("module", "source nesting too deep to parse")])
def test_deep_unary_expressions_do_not_overflow_a_small_stack(tmp_path: Path, path: str, expected: str) -> None:
    """파서 하나의 깊이 한도(MAXSTACK) 안의 6 KB 식도 작은 스택(1 MiB 스레드)을 넘기지 않는다.

    3.13까지 파서는 깊이를 수로만 막아, `~` 5,967개 식 하나에 3.13.18은 1,280 KiB(Linux)가 들었다. macOS 3.13은 더
    짧은 입력에서 1 MiB 스레드가 죽었다(SIGBUS). 3.13은 깊은 트리를 해제할 때도 trashcan이 약 1만 단까지 미뤄지지 않아,
    전용 스택에서 파싱하고 부른 스레드에서 버리면 해제 재귀로 죽었다. 이제 3.13까지 긴 소스는 전용 스택에서 파싱하고
    상한을 넘는 트리는 그 스레드에서 버린다.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SMALL_STACK_UNARY, path, str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout == expected + "\n"


#: 임계(`INLINE_PARSE_CHARACTERS`) 이하에서 C 스택을 가장 많이 쓰는 식을 512 KiB 스레드에서 부른 스레드 파싱으로
#: 읽는 별도 프로세스 스크립트다(인자: 프로젝트). 식마다 "이름:소스 길이:전용 스택 크기들"을 쓴다.
_INLINE_WORST_CASES = r"""
import sys
import threading
from pathlib import Path

from pythograph.source import syntax
from pythograph.source.project import Project
from pythograph.source.syntax import parse_annotation

budget = syntax.INLINE_PARSE_CHARACTERS - 3


def nested(opening, closing, unary=""):
    depth = min((budget - 1) // 2, 199)
    return opening * depth + unary * (budget - 1 - 2 * depth) + "x" + closing * depth


def fstrings():
    for per_level in range(budget, 0, -1):
        expression = "x"
        for quote in ("", '"' * 3, "'" * 3, '"', "'")[::-1]:
            core = "[" * per_level + expression + "]" * per_level
            expression = core if not quote else "f" + quote + "{" + core + "}" + quote
        if len(expression) <= budget:
            return expression


shapes = {
    "square": nested("[", "]"),
    "paren": nested("(", ")"),
    "brace": nested("{", "}"),
    "mixed": nested("[", "]", "~"),
    "fstrings": fstrings(),
    "tilde": "~" * (budget - 1) + "x",
}
results = []


def work():
    root = Path(sys.argv[1])
    for name, expression in shapes.items():
        source = "X=" + expression + "\n"
        (root / f"{name}.py").write_bytes(source.encode())
        stacks = sorted({syntax.guarded_stack_bytes(text, syntax.chain_bound(text)) for text in (expression, source)})
        parse_annotation(expression)
        Project.open(root).module(f"{name}.py")
        results.append(f"{name}:{len(source)}:{stacks}")


threading.stack_size(512 * 1024)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print("\n".join(results))
"""


def test_short_sources_parse_inline_with_half_a_small_stack(tmp_path: Path) -> None:
    """임계 이하 소스는 부른 스레드에서 파싱하고, 그 최악도 1 MiB의 절반(512 KiB) 스레드에서 끝난다(2배 여유).
    3.10·3.11에서 f-string이 든 식은 길이와 무관하게 전용 스택이다.

    가장 비싼 글자는 괄호(한 글자에 약 1.0–1.1 KiB)다. Linux 실측 최악은 256자 괄호 식의 272 KiB(3.10.20·3.13.18),
    f-string 4단(파서 다섯)에 나눠 겹친 괄호는 256 KiB 이하였다. 각 CI 판(macOS arm64·MSVC 프레임 포함)에서 이 여유가
    맞는지 이 테스트가 본다. Windows는 `threading.stack_size`가 커밋 크기라 실제 예약은 기본값(2,000,000 B)이다.
    """
    result = subprocess.run(
        [sys.executable, "-c", _INLINE_WORST_CASES, str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    lines = result.stdout.splitlines()
    assert [line.split(":")[0] for line in lines] == ["square", "paren", "brace", "mixed", "fstrings", "tilde"]
    fstring_stack = syntax.MIN_PARSE_STACK_BYTES + syntax.FSTRING_STACK_ALLOWANCE_BYTES
    for line in lines:
        name, length, stacks = line.split(":")
        assert int(length) <= syntax.INLINE_PARSE_CHARACTERS
        assert int(length) >= syntax.INLINE_PARSE_CHARACTERS - 8
        dedicated = name == "fstrings" and sys.version_info < (3, 12)
        assert stacks == (f"[{fstring_stack}]" if dedicated else "[0]")


def _has_fstring_token(text: str) -> bool:
    """토크나이저가 f-string으로 읽는 토큰이 있는지 본다(3.12 이상 `FSTRING_START`, 그 전에는 접두사에 `f`가 든 문자열).

    Args:
        text: 소스.

    Returns:
        f-string이 있으면 True.
    """
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == fstring_start:
            return True
        if token.type == tokenize.STRING and "f" in syntax._string_prefix(token.string).lower():
            return True
    return False


#: f-string 접두사의 모든 철자(`f`·`fr`·`rf`의 대소문자 조합 10가지)다.
FSTRING_PREFIXES = sorted(
    {"".join(letters) for base in ("f", "fr", "rf") for letters in itertools.product(*((c, c.upper()) for c in base))}
)


def test_fstring_detector_misses_no_prefix_spelling() -> None:
    """f-string 감지는 모든 접두사 철자·따옴표에서 놓치지 않고, f-string이 아닌 문자열 접두사에는 걸리지 않는다."""
    assert len(FSTRING_PREFIXES) == 10
    for prefix in FSTRING_PREFIXES:
        for quote in ('"', "'", '"""', "'''"):
            text = f"x = {prefix}{quote}{{a}}{quote}\n"
            assert _has_fstring_token(text), text
            assert syntax.may_contain_fstring(text), text
    for prefix in ("", "b", "B", "r", "R", "u", "U", "rb", "bR", "Rb", "BR"):
        assert not syntax.may_contain_fstring(f"x = {prefix}'{{a}}'\n"), prefix


def test_fstring_detector_finds_every_fstring_in_the_repository() -> None:
    """저장소의 모든 파이썬 소스에서 토크나이저가 f-string을 찾은 파일은 감지도 찾는다(놓침 없음)."""
    checked = 0
    for path in sorted(REPOSITORY.glob("*/**/*.py")):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            found = _has_fstring_token(text)
        except (tokenize.TokenError, SyntaxError):
            continue
        if found:
            checked += 1
            assert syntax.may_contain_fstring(text), path
    assert checked > 50


@pytest.mark.usefixtures("fresh_parse_workers")
def test_long_sources_use_a_dedicated_stack_until_the_parser_checks_its_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.13까지는 `INLINE_PARSE_CHARACTERS`보다 긴 소스를 전용 스택에서 파싱하고 결과는 같다. 3.10·3.11 파서에서
    f-string이 있을 수 있는 소스는 길이와 무관하게 겹친 파서 몫을 더한 전용 스택이다(델타 재리뷰 S1). 스택 경계를 직접
    보는 3.14부터는 긴 사슬이 아니면 부른 스레드다.

    파일은 바이트로 써서 길이가 플랫폼과 무관하다(Windows의 텍스트 모드는 줄바꿈에 CR을 더해 임계를 넘겼다).
    """
    monkeypatch.setattr(syntax, "_PARSER_CHECKS_C_STACK", False)
    monkeypatch.setattr(syntax, "_FSTRING_PARSERS_STACK", True)
    limit = syntax.INLINE_PARSE_CHARACTERS
    base = syntax.MIN_PARSE_STACK_BYTES
    allowance = syntax.FSTRING_STACK_ALLOWANCE_BYTES
    plain_edge = "x = 1  #" + "#" * (limit - 9) + "\n"
    plain_over = plain_edge + "\n"
    fstring_edge = 'x = f"{a}"' + "#" * (limit - 11) + "\n"
    fstring_over = fstring_edge + "\n"
    assert [len(text) for text in (plain_edge, plain_over, fstring_edge, fstring_over)] == [limit, limit + 1] * 2
    assert syntax.guarded_stack_bytes(plain_edge, 0) == 0
    assert syntax.guarded_stack_bytes(plain_over, 0) == base
    assert syntax.guarded_stack_bytes(fstring_edge, 0) == base + allowance
    assert syntax.guarded_stack_bytes('f"{a}"', 0) == base + allowance
    assert syntax.guarded_stack_bytes(fstring_over, 0) == base + allowance
    bound = syntax.SMALL_STACK_CHAIN_TOKENS + 1
    assert syntax.guarded_stack_bytes(fstring_over, bound) == syntax.parse_stack_bytes(bound) + allowance
    assert syntax.guarded_stack_bytes(fstring_over, bound) % syntax.PARSE_STACK_ALIGNMENT_BYTES == 0
    started = _record_thread_starts(monkeypatch)
    names = ("plain_edge", "plain_over", "fstring_edge", "fstring_over")
    for name, text in zip(names, (plain_edge, plain_over, fstring_edge, fstring_over), strict=True):
        (tmp_path / f"{name}.py").write_bytes(text.encode())
    project = Project.open(tmp_path)
    assert all(isinstance(project.module(f"{name}.py"), SourceModule) for name in names)
    assert started == [base, base + allowance]
    monkeypatch.setattr(syntax, "_FSTRING_PARSERS_STACK", False)
    assert syntax.guarded_stack_bytes(fstring_edge, 0) == 0
    assert syntax.guarded_stack_bytes(fstring_over, 0) == base
    monkeypatch.setattr(syntax, "_PARSER_CHECKS_C_STACK", True)
    assert syntax.guarded_stack_bytes(fstring_over, 0) == 0
    assert syntax.guarded_stack_bytes(fstring_over, bound) == syntax.parse_stack_bytes(bound)


@pytest.mark.usefixtures("fresh_parse_workers")
def test_parser_thread_relays_base_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """작업 스레드의 `BaseException`도 부른 스레드에서 그대로 다시 낸다. 전에는 `Exception`만 담아 결과 목록이 비고
    IndexError(내부 오류 2)가 됐다. 예외를 낸 작업 뒤에도 같은 스레드가 다음 작업을 받는다."""

    class Interrupted(BaseException):
        """`Exception`이 아닌 예외다."""

    def interrupted() -> int:
        """언제나 `Interrupted`를 낸다."""
        raise Interrupted

    started = _record_thread_starts(monkeypatch)
    with pytest.raises(Interrupted):
        syntax._run_on_large_stack(interrupted, syntax.MIN_PARSE_STACK_BYTES)
    assert syntax._run_on_large_stack(lambda: 7, syntax.MIN_PARSE_STACK_BYTES) == 7
    assert started == [syntax.MIN_PARSE_STACK_BYTES]


@pytest.mark.usefixtures("fresh_parse_workers")
def test_dedicated_parse_threads_are_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """전용 스택 파싱은 스택 크기마다 스레드 하나를 만들어 다시 쓴다.

    3.13까지 256자를 넘는 소스를 모두 전용 스택에서 파싱하므로 파싱마다 스레드를 만들면 시작·종료와 스케줄링 대기가 긴
    파일마다 생긴다(4코어 실측: 파일 하나에 CPU 약 0.2–0.6 ms, 재사용하면 약 0.04–0.2 ms). 하한과 f-string 몫을
    더한 크기 사이의 요청은 위 크기의 스레드로 올리고, 그보다 큰 3.10의 아주 긴 사슬만 파싱마다 스레드를 만든다.
    """
    monkeypatch.setattr(syntax, "_PARSER_CHECKS_C_STACK", False)
    monkeypatch.setattr(syntax, "_FSTRING_PARSERS_STACK", False)
    base = syntax.MIN_PARSE_STACK_BYTES
    raised = base + syntax.FSTRING_STACK_ALLOWANCE_BYTES
    larger = raised + syntax.PARSE_STACK_ALIGNMENT_BYTES
    started = _record_thread_starts(monkeypatch)
    padding = "#" * syntax.INLINE_PARSE_CHARACTERS
    for index in range(3):
        (tmp_path / f"long{index}.py").write_bytes(f"x = {index}  {padding}\n".encode())
    for _ in range(2):
        project = Project.open(tmp_path)
        assert all(isinstance(project.module(f"long{index}.py"), SourceModule) for index in range(3))
    assert started == [base]
    assert syntax._run_on_large_stack(lambda: 1, base + syntax.PARSE_STACK_ALIGNMENT_BYTES) == 1
    assert syntax._run_on_large_stack(lambda: 2, raised) == 2
    assert started == [base, raised]
    assert syntax._run_on_large_stack(lambda: 3, larger) == 3
    assert syntax._run_on_large_stack(lambda: 4, larger) == 4
    assert started == [base, raised, larger, larger]


#: 전용 파싱 스레드를 만든 뒤 fork한 자식이 자기 스레드로 파싱하는지 보는 별도 프로세스 스크립트다. 부모의 스레드는
#: 자식에 없으므로 그 스레드에 작업을 넘기면 자식이 영원히 기다린다(30초 안에 끝나지 않으면 죽이고 "hung"을 쓴다).
_FORKED_PARSE = r"""
import os
import time
import warnings

from pythograph.source import syntax

warnings.simplefilter("ignore", DeprecationWarning)
assert syntax._run_on_large_stack(lambda: 1, syntax.MIN_PARSE_STACK_BYTES) == 1
child = os.fork()
if child == 0:
    os._exit(0 if syntax._run_on_large_stack(lambda: 7, syntax.MIN_PARSE_STACK_BYTES) == 7 else 1)
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    pid, status = os.waitpid(child, os.WNOHANG)
    if pid:
        print(os.waitstatus_to_exitcode(status))
        break
    time.sleep(0.05)
else:
    os.kill(child, 9)
    os.waitpid(child, 0)
    print("hung")
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork가 없는 플랫폼이다")
def test_forked_children_parse_on_their_own_thread() -> None:
    """fork한 자식은 부모의 전용 파싱 스레드를 쓰지 않고 자기 스레드를 만든다(멈추지 않는다)."""
    result = subprocess.run(
        [sys.executable, "-c", _FORKED_PARSE],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=60,
    )
    assert (result.returncode, result.stdout) == (0, "0\n"), result.stderr[-2000:]


#: arm64 macOS의 페이지 크기(바이트)다. 그 `pthread_attr_setstacksize`는 이 배수가 아닌 스택을 거부한다.
_MACOS_ARM64_PAGE_BYTES = 16 * 1024


def _page_strict_stack_size(*size: int) -> int:
    """macOS처럼 페이지 배수가 아닌 크기를 ValueError로 거부하는 `threading.stack_size`다.

    Args:
        size: 비우면 현재 값을 읽고, 하나면 그 크기로 바꾼다.

    Returns:
        바꾸기 전의 스택 크기 설정.

    Raises:
        ValueError: 0이 아니면서 페이지 배수가 아닌 크기일 때(CPython이 EINVAL을 바꾸는 예외).
    """
    if size and size[0] % _MACOS_ARM64_PAGE_BYTES != 0:
        raise ValueError(f"size not valid: {size[0]} bytes")
    return threading.stack_size(*size)


def test_parse_stack_sizes_are_whole_mebibytes() -> None:
    """전용 스택 크기는 토큰당 바이트·하한을 채우는 가장 작은 1 MiB 배수다. 페이지 배수가 아닌 크기는 macOS에서
    스레드를 만들지 못해(ValueError) 긴 사슬 모듈이 중첩 실패로 잘못 남았다(macOS 3.10 CI)."""
    mebibyte = 1024 * 1024
    for bound in (syntax.SMALL_STACK_CHAIN_TOKENS + 1, 8_193, 8_200, 30_001, 49_999, MAX_CHAIN_TOKENS):
        stack = syntax.parse_stack_bytes(bound)
        wanted = max(syntax.MIN_PARSE_STACK_BYTES, bound * syntax.PARSE_STACK_BYTES_PER_TOKEN)
        assert stack % mebibyte == 0
        assert stack % _MACOS_ARM64_PAGE_BYTES == 0
        assert wanted <= stack < wanted + mebibyte


@pytest.mark.usefixtures("fresh_parse_workers")
def test_long_chains_parse_where_stacks_must_be_whole_pages(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """페이지 배수만 받는 플랫폼(macOS)에서도 긴 사슬은 전용 스택 스레드에서 파싱된다. 하한을 1바이트로 낮춰 토큰당
    바이트만으로 정한 크기가 페이지 배수가 아니게 만든다(고치기 전에는 중첩 실패였다)."""
    monkeypatch.setattr(syntax, "_PARSER_OVERFLOWS_ON_CHAINS", True)
    monkeypatch.setattr(syntax, "SMALL_STACK_CHAIN_TOKENS", 10)
    monkeypatch.setattr(syntax, "MIN_PARSE_STACK_BYTES", 1)
    monkeypatch.setattr(
        syntax, "threading", SimpleNamespace(Thread=threading.Thread, stack_size=_page_strict_stack_size)
    )
    chain = " + ".join(["a"] * 41)
    root = make_project({"chain.py": f"VALUE = {chain}\n"})
    before = threading.stack_size()
    assert isinstance(Project.open(root).module("chain.py"), SourceModule)
    assert isinstance(parse_annotation(chain), ast.BinOp)
    assert threading.stack_size() == before


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
        [sys.executable, "-m", "pythograph", "--version"],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
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
