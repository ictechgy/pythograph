"""이름 해석과 실제 읽기에 동일한 프로젝트 경계와 링크 거부 규칙을 적용한다."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from pythograph.routes.versions import declared_majors
from pythograph.source import project as project_module
from pythograph.source.project import Project, ReadFailure, SourceModule
from tests.conftest import run_cli

MakeProject = Callable[[dict[str, str]], Path]


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("package", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_linked_parents_are_rejected_by_every_source_entry_point(
    make_project: MakeProject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    internal: bool,
    package: bool,
    fallback: bool,
) -> None:
    """대상이 내부여도 링크 부모를 거부하고 모듈·패키지·텍스트 읽기가 우회하지 못한다."""
    if fallback:
        monkeypatch.setattr(project_module, "_HAS_DIR_FD", False)
    root = make_project({"plain.py": "value = 1\n"})
    target = root / ".payload" if internal else tmp_path / "outside"
    target.mkdir()
    source_path = "app/__init__.py" if package else "app.py"
    source = target / source_path
    source.parent.mkdir(exist_ok=True)
    source.write_text("value = 'outside-only'\n")
    (root / "linked").symlink_to(target, target_is_directory=True)
    project = Project.open(root)

    assert project.python_files() == ["plain.py"]
    assert not project.is_directory("linked")
    assert not project.is_directory("linked/app")
    assert not project.is_file(f"linked/{source_path}")
    assert project.resolve_module("linked.app") is None
    assert project.read_text(f"linked/{source_path}") is None
    assert isinstance(project.module(f"linked/{source_path}"), ReadFailure)
    project.module_roots.insert(0, "linked")
    assert project.resolve_module("app") is None
    assert project.skipped_links == 1
    assert isinstance(project.module("plain.py"), SourceModule)


@pytest.mark.parametrize("fallback", [False, True])
def test_remembered_resolution_still_rechecks_links_when_reading(
    make_project: MakeProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    """이름 해석 결과를 기억해도 읽기는 열 때 다시 검사하고, 링크 아래 이름은 몇 번 풀어도 링크를 한 번만 센다.

    해석은 한 실행 안에서 이름별 결과를 기억한다. 해석 뒤 부모 디렉터리가 링크로 바뀌면 읽기가 성분별 lstat·O_NOFOLLOW로
    거부해야 한다(경계는 해석이 아니라 읽기가 지킨다).
    """
    if fallback:
        monkeypatch.setattr(project_module, "_HAS_DIR_FD", False)
    root = make_project({"pkg/__init__.py": "", "pkg/app.py": "value = 'inside'\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("value = 'outside-only'\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    project = Project.open(root)
    for _ in range(3):
        assert project.resolve_module("linked.app") is None
    assert project.skipped_links == 1
    assert project.resolve_module("pkg.app") == "pkg/app.py"
    (root / "pkg").rename(root / ".saved")
    (root / "pkg").symlink_to(outside, target_is_directory=True)
    assert isinstance(project.module("pkg/app.py"), ReadFailure)
    assert project.read_text("pkg/app.py") is None
    assert project.skipped_links == 2


@pytest.mark.parametrize("internal", [False, True])
def test_linked_files_and_module_roots_are_rejected(make_project: MakeProject, tmp_path: Path, internal: bool) -> None:
    """파일 자체 링크와 src 모듈 루트 링크, 끊어진 링크도 따라가지 않고 각각 한 번만 센다."""
    root = make_project({"plain.py": "value = 1\n"})
    target = root / ".payload" if internal else tmp_path / "outside"
    target.mkdir()
    (target / "app.py").write_text("value = 'outside-only'\n")
    (root / "linked.py").symlink_to(target / "app.py")
    (root / "src").symlink_to(target, target_is_directory=True)
    (root / "broken.py").symlink_to(target / "missing.py")
    project = Project.open(root)
    assert project.module_roots == [""]
    assert project.resolve_module("linked") is None
    assert project.resolve_module("app") is None
    for path in ("linked.py", "broken.py", "src/app.py"):
        assert project.read_text(path) is None
        assert isinstance(project.module(path), ReadFailure)
    assert project.skipped_links == 3


@pytest.mark.parametrize("kind", ["parent", "file"])
def test_linked_manifests_do_not_supply_framework_versions(
    make_project: MakeProject, tmp_path: Path, kind: str
) -> None:
    """설정·의존성 선언도 소스와 같은 경계 안에서만 읽는다."""
    root = make_project({"app.py": ""})
    target = tmp_path / "outside"
    target.mkdir()
    (target / "requirements.txt").write_text("Django==5.2.17\n")
    if kind == "parent":
        (root / "linked").symlink_to(target, target_is_directory=True)
        project = Project.open(root)
        project.module_roots.insert(0, "linked")
    else:
        (root / "requirements.txt").symlink_to(target / "requirements.txt")
        project = Project.open(root)
    assert declared_majors(project) == {}
    assert project.skipped_links == 1


def test_links_that_cannot_hide_sources_are_not_counted(make_project: MakeProject, tmp_path: Path) -> None:
    """따라갔어도 순회가 읽지 않았을 링크(소스가 아닌 파일, 점 이름·건너뛰는 디렉터리, 끊어진 비 `.py`)는 세지 않는다.

    `CLAUDE.md -> AGENTS.md`·`README.md -> docs/README.md`·`.venv` 같은 링크 하나가 문서 전체 커버리지 한계가 되어
    서버 측 진단을 경고로 묻고 닫힌 세계 `bound` 증명을 막았다.
    """
    root = make_project({"app.py": "x = 1\n", "AGENTS.md": "notes\n", "docs/README.md": "readme\n"})
    outside = tmp_path / "outside"
    (outside / "lib").mkdir(parents=True)
    (outside / "lib" / "mod.py").write_text("value = 'outside-only'\n")
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "README.md").symlink_to("docs/README.md")
    (root / ".venv").symlink_to(outside, target_is_directory=True)
    (root / "venv").symlink_to(outside, target_is_directory=True)
    (root / "notes.txt").symlink_to("missing.txt")
    project = Project.open(root)
    assert project.python_files() == ["app.py"]
    assert project.skipped_links == 0


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("pkg_link", "pkg"),
        ("mod.py", "other.py"),
        ("gone.py", "missing.py"),
        (".hidden.py", "other.py"),
        ("loop", "loop"),
    ],
)
def test_links_that_may_hide_sources_are_counted(make_project: MakeProject, link: str, target: str) -> None:
    """들어갔을 디렉터리 이름(확장자 없음)이거나 `.py` 이름(끊어진 링크 포함)인 링크는 따라가지 않고 한 번 센다.

    점으로 시작하는 `.py` 파일도 순회가 읽으므로 그 이름의 파일 링크는 센다. 순환 링크도 이름으로 센다.
    """
    root = make_project({"pkg/__init__.py": "", "other.py": "x = 1\n"})
    (root / link).symlink_to(target, target_is_directory=target == "pkg")
    project = Project.open(root)
    assert project.python_files() == ["other.py", "pkg/__init__.py"]
    assert project.skipped_links == 1


@pytest.mark.parametrize("kind", ["directory", "file", "missing"])
def test_link_count_does_not_depend_on_the_link_target(make_project: MakeProject, tmp_path: Path, kind: str) -> None:
    """링크를 셀지는 이름만으로 정하고 대상에는 접근하지 않는다. 프로젝트 밖 대상의 종류·존재와 무관하게 같다.

    대상을 stat해 종류를 골랐을 때는 `probe -> /usr/bin`이 1, `-> /nonexistent-dir`·`-> /etc/hosts`가 0이라, 출력이나
    CI 로그를 보는 저장소 작성자가 분석 머신에 어떤 경로가 있는지 링크 하나에 한 비트씩 알아낼 수 있었다.
    """
    outside = tmp_path / "outside"
    (outside / "lib").mkdir(parents=True)
    (outside / "lib" / "mod.py").write_text("value = 'outside-only'\n")
    (outside / "file.txt").write_text("text\n")
    target = {"directory": outside / "lib", "file": outside / "file.txt", "missing": outside / "missing"}[kind]
    root = make_project({"app.py": "x = 1\n"})
    for name in ("probe", "probe.txt", "probe.py"):
        (root / name).symlink_to(target, target_is_directory=kind == "directory")
    project = Project.open(root)
    assert project.python_files() == ["app.py"]
    assert project.skipped_links == 2
    document = json.loads(run_cli(["schema", "--project", str(root)])[1])
    assert [text for text in document["limitations"] if "symbolic links" in text] == [
        "skipped-symlinks: 2 symbolic links were not followed"
    ]


@pytest.mark.parametrize("hiding", [False, True])
def test_only_links_that_may_hide_sources_limit_documents(
    make_project: MakeProject, tmp_path: Path, hiding: bool
) -> None:
    """소스와 무관한 링크만 있으면 routes·schema·graph에 링크 한계가 없고 graph `scanIncomplete`도 거짓이다."""
    root = make_project(
        {
            "manage.py": "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shop.settings')\n",
            "shop/__init__.py": "",
            "shop/settings.py": "ROOT_URLCONF = 'shop.urls'\nINSTALLED_APPS = []\n",
            "shop/urls.py": (
                "from django.urls import path\nfrom shop.views import index\nurlpatterns = [path('ok/', index)]\n"
            ),
            "shop/views.py": "def index(request):\n    pass\n",
            "AGENTS.md": "notes\n",
            "docs/README.md": "readme\n",
        }
    )
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "README.md").symlink_to("docs/README.md")
    (root / ".venv").symlink_to(tmp_path, target_is_directory=True)
    if hiding:
        (root / "shop_link").symlink_to("shop", target_is_directory=True)
    documents = [
        json.loads(run_cli([*command, "--project", str(root)])[1])
        for command in (["routes", "--role", "server"], ["schema"], ["graph"])
    ]
    for document in documents:
        assert any("symbolic links" in text for text in document["limitations"]) is hiding, document["limitations"]
    assert documents[2]["statistics"]["boundDispatch"]["scanIncomplete"] is hiding


def _full_search(project: Project, name: str) -> str | None:
    """색인 없이 모든 모듈 루트·후보를 검사하던 옛 이름 찾기다(색인이 결과·링크 수를 바꾸지 않는지 대조한다).

    Args:
        project: 프로젝트.
        name: 모듈 이름.

    Returns:
        프로젝트 상대 경로, 없으면 None.
    """
    if not name or any(not part.isidentifier() for part in name.split(".")):
        return None
    relative_parts = name.replace(".", "/")
    for root in project.module_roots:
        base = f"{root}/{relative_parts}" if root else relative_parts
        for candidate in (f"{base}/__init__.py", f"{base}.py"):
            if project.is_file(candidate):
                return candidate
    return None


#: 색인 대조용 모듈 루트다(순서가 우선순위다). 링크 아래 루트, 없는 루트, 파일인 루트, 목록을 못 읽는 루트를 섞는다.
_INDEX_ROOTS = ["site_a", "site_b", "build/linked_root", "missing_root", "file_root", "locked", "", "src"]

#: 색인 대조용 이름이다. 대소문자·유니코드 정규화만 다른 철자, 순회가 세지 않는 링크 아래 이름, 외부 이름을 섞는다.
_INDEX_NAMES = [
    "shop",
    "Shop.models",
    "SHOP",
    "mod",
    "MOD",
    "caf\u00e9",
    "CAF\u00c9",
    "env.x",
    "env",
    "linkmod",
    "pkg",
    "plain.deep.leaf",
    "inner",
    "Inner",
    "build.linked_root.pkg",
    "missing.name",
    "django.db",
    "x",
    "",
]


@pytest.mark.parametrize("fallback", [False, True])
def test_module_root_index_keeps_resolution_and_link_counts(
    make_project: MakeProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    """루트 항목 색인으로 후보를 거른 해석이 모든 후보를 검사하던 해석과 결과·링크 수가 같다.

    대소문자·유니코드 정규화를 무시하는 파일시스템(macOS 기본 APFS)에서는 lstat이 철자가 다른 이름(`SHOP`·NFC `café`)도
    찾으므로 색인도 그 이름을 같은 이름으로 본다. 순회가 세지 않은 링크(건너뛰는 이름 `env`, `build/` 아래 루트)는
    해석이 처음 닿을 때 세고, 목록을 못 읽는 루트는 예전처럼 후보를 모두 검사한다.
    """
    if fallback:
        monkeypatch.setattr(project_module, "_HAS_DIR_FD", False)
    root = make_project(
        {
            "site_a/manage.py": "",
            "site_a/Shop/__init__.py": "",
            "site_a/Shop/models.py": "",
            "site_a/mod.py": "",
            "site_b/manage.py": "",
            "site_b/shop/__init__.py": "",
            "site_b/cafe\u0301.py": "",
            "plain/__init__.py": "",
            "plain/deep/leaf.py": "",
            "file_root": "not a directory\n",
            "locked/inner.py": "",
            "build/keep.txt": "",
        }
    )
    outside = tmp_path / "outside"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "__init__.py").write_text("")
    (root / "site_b" / "env").symlink_to(outside, target_is_directory=True)
    (root / "site_b" / "linkmod.py").symlink_to(outside / "pkg" / "__init__.py")
    (root / "build" / "linked_root").symlink_to(outside, target_is_directory=True)
    (root / "locked").chmod(0o311)
    try:
        reference, indexed = Project.open(root), Project.open(root)
        reference.module_roots = list(_INDEX_ROOTS)
        indexed.module_roots = list(_INDEX_ROOTS)
        for name in _INDEX_NAMES * 2:
            assert indexed.resolve_module(name) == _full_search(reference, name), name
            assert indexed.skipped_links == reference.skipped_links, name
    finally:
        (root / "locked").chmod(0o755)
    # 순회가 센 `linkmod.py` 하나에, 해석이 처음 닿을 때 센 `site_b/env`·`build/linked_root`가 더해진다.
    assert reference.skipped_links == 3


def test_module_root_with_too_many_entries_is_searched_without_an_index(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """항목이 순회 상한보다 많은 루트는 항목을 끝까지 읽지 않고, 색인 없이 예전처럼 후보를 모두 검사한다."""
    project = Project.open(make_project({"a.py": "", "b.py": "", "pkg/__init__.py": "", "pkg/mod.py": ""}))
    monkeypatch.setattr(project_module, "MAX_SCANNED_ENTRIES", 2)
    checked: list[str] = []
    checked_stat = project._checked_stat

    def counting(relative: str) -> os.stat_result:
        checked.append(relative)
        return checked_stat(relative)

    monkeypatch.setattr(project, "_checked_stat", counting)
    assert project.resolve_module("pkg.mod") == "pkg/mod.py"
    before = len(checked)
    assert project.resolve_module("missing") is None
    assert checked[before:] == ["missing/__init__.py", "missing.py"]


@pytest.mark.parametrize("skipped", [False, True])
def test_manage_directories_beyond_the_root_limit_limit_documents(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, skipped: bool
) -> None:
    """모듈 루트로 쓰지 않은 `manage.py` 디렉터리 수를 routes(server·client)·schema·graph 한계로 남기고 `bound`를 연다.

    상한 밖 디렉터리의 코드는 그 디렉터리 기준 import가 풀리지 않아 호출이 외부로 보이므로, 빈 결과를 완전하다고 읽지
    않게 알린다. 상한(256)을 작게 바꿔 디렉터리 둘로 확인한다.
    """
    monkeypatch.setattr(project_module, "MAX_MODULE_ROOTS", 1 if skipped else 2)
    root = make_project(
        {
            "sites/a/manage.py": "",
            "sites/b/manage.py": "",
            "sites/b/shop/__init__.py": "",
            "sites/b/shop/models.py": "def save():\n    pass\n",
            "sites/b/shop/views.py": "from shop.models import save\n\n\ndef index():\n    save()\n",
        }
    )
    commands = {
        "route-coverage:": ["routes", "--role", "server"],
        "route-call-coverage:": ["routes", "--role", "client"],
        "scan-truncated:": ["schema"],
        "scan-incomplete:": ["graph"],
    }
    documents = {
        prefix: json.loads(run_cli([*command, "--project", str(root)])[1]) for prefix, command in commands.items()
    }
    reason = (
        "1 directories containing manage.py are not used as module roots (only the 1 shallowest are), so imports "
        "that only they would resolve are treated as external; pass a narrower --project to analyze them"
    )
    for prefix, document in documents.items():
        lines = [text for text in document["limitations"] if "module roots" in text]
        assert lines == ([f"{prefix} {reason}"] if skipped else []), document["limitations"]
    graph = documents["scan-incomplete:"]
    assert graph["statistics"]["boundDispatch"]["scanIncomplete"] is skipped
    edges = {(edge["from"], edge["to"]) for edge in graph["edges"]}
    assert (("sites/b/shop/views.py#index", "sites/b/shop/models.py#save") in edges) is not skipped


@pytest.mark.parametrize(
    "relative", ["../outside/app.py", "/app.py", "C:/app.py", "C:app.py", "a\\app.py", "bad\x00.py"]
)
def test_non_relative_paths_fail_before_open(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """상위·절대·Windows 경로·NUL을 각 진입점에서 일관되게 거부한다."""
    project = Project.open(make_project({"app.py": ""}))

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        """경로 문법에서 거부할 입력은 파일 열기에 도달하면 안 된다."""
        raise AssertionError("invalid path must not open a file")

    monkeypatch.setattr(project_module.os, "open", unexpected_open)
    assert not project.is_directory(relative)
    assert not project.is_file(relative)
    assert project.read_text(relative) is None
    assert isinstance(project.module(relative), ReadFailure)


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("package", [False, True])
@pytest.mark.parametrize("entry", ["settings", "urls"])
def test_django_cli_cannot_extract_through_linked_settings_or_urlconf(
    make_project: MakeProject, tmp_path: Path, internal: bool, package: bool, entry: str
) -> None:
    """실제 Django CLI에서 settings 문자열이나 ROOT_URLCONF가 링크 아래 사실을 가져오지 못한다."""
    settings_module = "linked.settings" if entry == "settings" else "settings"
    root = make_project(
        {
            "manage.py": f"import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', '{settings_module}')\n",
            "settings.py": "ROOT_URLCONF = 'linked.urls'\nINSTALLED_APPS = []\n",
        }
    )
    target = root / ".payload" if internal else tmp_path / "outside"
    target.mkdir()
    (target / "settings.py").write_text("ROOT_URLCONF = 'linked.urls'\nINSTALLED_APPS = []\n")
    source = target / "urls" / "__init__.py" if package else target / "urls.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text(
        "from django.urls import path\ndef outside_view(request): pass\n"
        "urlpatterns = [path('outside-only/', outside_view)]\n"
    )
    (root / "linked").symlink_to(target, target_is_directory=True)
    code, output, error = run_cli(["routes", "--role", "server", "--project", str(root)])
    assert code == 0, error
    document = json.loads(output)
    assert document["facts"] == []
    assert "outside-only" not in output
    assert any("1 symbolic links were not followed" in gap for gap in document["limitations"])
    assert str(target) not in json.dumps(document["limitations"])


@pytest.mark.parametrize("method", ["module", "read_text"])
@pytest.mark.parametrize("swap", ["file", "parent", "regular"])
@pytest.mark.parametrize("fallback", [False, True])
def test_swapping_paths_at_file_open_never_reads_replacement(
    make_project: MakeProject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    swap: str,
    fallback: bool,
) -> None:
    """검사 직후 파일·부모 링크 교체나 일반 파일 교체를 주입해 읽기 전에 거부되는지 확인한다."""
    if fallback:
        monkeypatch.setattr(project_module, "_HAS_DIR_FD", False)
    root = make_project({"pkg/app.py": "value = 'inside'\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("value = 'outside-only'\n")
    project = Project.open(root)
    original = os.open
    swapped = False

    def replacing_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """실제 open 직전에 대상 이름을 바꾸되 파일 핸들 검사를 생략하지 않는다."""
        nonlocal swapped
        if Path(path).name == "app.py" and not swapped:
            swapped = True
            if swap == "parent":
                (root / "pkg").rename(root / ".saved")
                (root / "pkg").symlink_to(outside, target_is_directory=True)
            else:
                (root / "pkg/app.py").rename(root / "pkg/saved.py")
                if swap == "file":
                    (root / "pkg/app.py").symlink_to(outside / "app.py")
                else:
                    (root / "pkg/app.py").write_text("value = 'replacement'\n")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(project_module.os, "open", replacing_open)
    result = getattr(project, method)("pkg/app.py")
    assert swapped
    assert isinstance(result, ReadFailure) if method == "module" else result is None


@pytest.mark.skipif(not project_module._HAS_DIR_FD, reason="descriptor-relative open is unavailable")
def test_parent_swap_before_directory_open_is_refused(
    make_project: MakeProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lstat와 디렉터리 open 사이 교체도 O_NOFOLLOW로 막는다."""
    root = make_project({"pkg/app.py": "value = 'inside'\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("value = 'outside-only'\n")
    project = Project.open(root)
    original = os.open
    swapped = False

    def replacing_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """부모 핸들을 여는 순간 링크로 교체한다."""
        nonlocal swapped
        if path == "pkg" and not swapped:
            swapped = True
            (root / "pkg").rename(root / ".saved")
            (root / "pkg").symlink_to(outside, target_is_directory=True)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(project_module.os, "open", replacing_open)
    assert isinstance(project.module("pkg/app.py"), ReadFailure)
    assert swapped


def test_queued_directory_replaced_by_link_is_not_scanned(
    make_project: MakeProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발견 후 방문 전에 바뀐 디렉터리를 건너뛰고 불완전한 순회를 보고한다."""
    root = make_project({"pkg/app.py": ""})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("")
    original = Project._visit

    def visit(
        self: Project,
        entry: os.DirEntry[str],
        directory: str,
        depth: int,
        pending: list[tuple[str, int]],
        found: list[str],
    ) -> None:
        """정상 디렉터리를 대기열에 넣은 뒤 교체한다."""
        original(self, entry, directory, depth, pending, found)
        if entry.name == "pkg":
            (root / "pkg").rename(root / ".saved")
            (root / "pkg").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(Project, "_visit", visit)
    project = Project.open(root)
    assert project.python_files() == []
    assert project.skipped_links == 1
    assert project.scan_errors == 1


def test_windows_directory_reparse_point_is_a_link() -> None:
    """junction은 디렉터리 모드만으로 일반 폴더라고 판정하지 않는다."""
    info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
    assert project_module._is_link(info)  # type: ignore[arg-type]
