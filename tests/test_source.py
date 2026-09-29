"""소스 계층(프로젝트 읽기·상수 평가·이름 해석) 테스트."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.source import project as project_module
from pythograph.source.evaluate import UNKNOWN, Evaluator
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
from tests.conftest import REPOSITORY


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


def test_unencodable_file_names_are_skipped(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UTF-8로 쓸 수 없는 파일 이름은 건너뛰고 센다(교환 형식이 쓸 수 없는 문자열이 새지 않게)."""
    assert project_module._is_encodable("\udcff") is False
    root = make_project({"ok.py": "", "odd.py": ""})
    monkeypatch.setattr(project_module, "_is_encodable", lambda text: not text.startswith("odd"))
    project = Project.open(root)
    assert project.python_files() == ["ok.py"] and project.unencodable_names == 1
