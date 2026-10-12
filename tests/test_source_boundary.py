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
