"""소스 계층(프로젝트 읽기·상수 평가·이름 해석) 테스트."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.source import project as project_module
from pythograph.source import syntax
from pythograph.source.evaluate import MAX_EVALUATED_LENGTH, UNKNOWN, Evaluator, value_size
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
from pythograph.source.syntax import MAX_AST_DEPTH, MAX_CHAIN_TOKENS, exceeds_depth, parse_annotation
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
