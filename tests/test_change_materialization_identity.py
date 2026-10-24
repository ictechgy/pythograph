"""Git이 허용한 raw 경로를 실제 대상 파일 시스템에서 합치지 않는 캡처 회귀다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from pythograph.cli.common import InputError
from pythograph.graph import change
from pythograph.graph.traversal import TraversalRequest
from pythograph.source import project as project_module
from pythograph.source.project import Project


def git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    """합성 객체만 다루며 사용자 Git 설정·hook·원문 출력을 사용하지 않는다."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments], input=data, env=environment
    )


def committed_paths(root: Path, files: dict[str, bytes]) -> str:
    """경로를 checkout하지 않고 mktree로 만든 뒤 strict fsck로 실제 유효성을 확인한다."""
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    entries: dict = {}
    for relative, content in files.items():
        parts, directory = relative.split("/"), entries
        for part in parts[:-1]:
            directory = directory.setdefault(part, {})
        directory[parts[-1]] = content

    def write_tree(directory: dict) -> bytes:
        """별칭 후보 이름을 bytes로 보존하며 유효한 하위 tree부터 조립한다."""
        rows = []
        for name, value in directory.items():
            if isinstance(value, dict):
                kind, oid = b"040000 tree ", write_tree(value)
            else:
                kind, oid = b"100644 blob ", git(root, "hash-object", "-w", "--stdin", data=value).strip()
            rows.append(kind + oid + b"\t" + name.encode("utf-8") + b"\0")
        return git(root, "mktree", "-z", data=b"".join(rows)).strip()

    tree = write_tree(entries).decode("ascii")
    commit = git(root, "commit-tree", tree, "-m", "synthetic path identity fixture").decode("ascii").strip()
    git(root, "fsck", "--strict", "--no-reflogs", "--no-dangling", commit)
    return commit


def alias_on_filesystem(root: Path, first: str, second: str) -> bool:
    """소유한 probe directory에서만 이름 별칭 여부를 실제 inode로 확인한다."""
    root.mkdir()
    (root / first).mkdir()
    return (root / second).exists() and (root / first).samefile(root / second)


@pytest.mark.parametrize(
    ("first", "second", "directory"),
    [
        ("Src", "src", True),
        ("caf\u00e9", "cafe\u0301", True),
        ("Value.py", "value.py", False),
        ("valu\u00e9.py", "value\u0301.py", False),
    ],
)
def test_actual_git_capture_refuses_only_physical_path_aliases(
    tmp_path: Path, first: str, second: str, directory: bool
) -> None:
    """Mac 별칭은 코드2·빈 stdout, case-sensitive 파일 시스템의 별개 경로는 그대로 분석한다."""
    aliases = alias_on_filesystem(tmp_path / "probe", first, second)
    names = (f"{first}/a.py", f"{second}/b.py") if directory else (first, second)
    files = {names[0]: b"def one(): return 1\n", names[1]: b"def two(): return 2\n"}
    root = tmp_path / "repository"
    commit = committed_paths(root, files)
    captured = change.capture(root, commit)
    assert captured.files == files
    assert captured.digest == change.content_digest(files)
    assert captured.omitted == 0
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pythograph",
            "change-impact",
            "--project",
            str(root),
            "--base",
            commit,
            "--head",
            commit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if aliases:
        assert result.returncode == 2
        assert result.stdout == ""
        assert "capture" in result.stderr and "Traceback" not in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "incomplete"
        document = change._analyze(
            captured,
            tmp_path / "materialized",
            set(files),
            TraversalRequest((), "dependents", 128, 100_000, "bound"),
            "synthetic-project",
        )
        assert {seed["path"] for seed in document["seeds"]} == set(files)


def test_reusing_the_same_logical_directory_preserves_source_paths(tmp_path: Path) -> None:
    """동일 raw prefix 재사용은 collision이 아니며 일반 direct caller를 유지한다."""
    root = tmp_path / "repository"
    files = {
        "pkg/a.py": b"def value(): return 1\n",
        "pkg/b.py": b"from .a import value\ndef caller(): return value()\n",
    }
    commit = committed_paths(root, files)
    captured = change.capture(root, commit)
    document = change._analyze(
        captured,
        tmp_path / "materialized",
        {"pkg/a.py"},
        TraversalRequest((), "dependents", 128, 100_000, "bound"),
        "synthetic-project",
    )
    assert "pkg/b.py#caller" in {row["id"] for row in document["reviewCandidates"]}


def test_path_backend_preserves_shared_prefix_and_refuses_actual_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dir-fd 미지원 경로도 exclusive mkdir/file과 실제 directory 신원 검사를 사용한다."""
    monkeypatch.setattr(project_module, "_HAS_DIR_FD", False)
    root = tmp_path / "repository"
    files = {"pkg/a.py": b"def one(): return 1\n", "pkg/b.py": b"def two(): return 2\n"}
    commit = committed_paths(root, files)
    value = change.capture(root, commit)
    change._materialize_capture(value, tmp_path / "ordinary")
    assert (tmp_path / "ordinary/pkg/a.py").read_bytes() == files["pkg/a.py"]
    aliases = alias_on_filesystem(tmp_path / "probe", "Src", "src")
    files = {"Src/a.py": b"def one(): return 1\n", "src/b.py": b"def two(): return 2\n"}
    root = tmp_path / "alias-repository"
    commit = committed_paths(root, files)
    value = change.capture(root, commit)
    if aliases:
        with pytest.raises(InputError, match="preserve paths"):
            change._materialize_capture(value, tmp_path / "aliased")
    else:
        change._materialize_capture(value, tmp_path / "aliased")
        assert {(tmp_path / "aliased" / path).read_bytes() for path in files} == set(files.values())


@pytest.mark.skipif(not project_module._HAS_DIR_FD, reason="directory handles unavailable")
def test_materialization_does_not_follow_a_parent_swapped_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """열린 own parent는 따라가되 이후 경로 교체는 실패하고 별도 own sentinel은 쓰지 않는다."""
    destination, sentinel = tmp_path / "materialized", tmp_path / "owned-sentinel"
    sentinel.mkdir()
    (sentinel / "target.py").write_bytes(b"unchanged")
    files = {"pkg/target.py": b"def value(): return 1\n"}
    value = change.Capture(files, change.content_digest(files), None, ())
    original = Project._open_directory
    swapped = False

    @contextmanager
    def replace_parent(project: Project, relative: str) -> Iterator[int | Path]:
        """원래 nofollow handle을 연 뒤 부모 이름만 한 번 바꾼다."""
        nonlocal swapped
        with original(project, relative) as directory:
            if relative == "pkg" and not swapped:
                (destination / "pkg").rename(destination / "owned-moved-pkg")
                (destination / "pkg").symlink_to(sentinel, target_is_directory=True)
                swapped = True
            yield directory

    monkeypatch.setattr(Project, "_open_directory", replace_parent)
    with pytest.raises(InputError, match=r"preserve paths|destination changed"):
        change._materialize_capture(value, destination)
    assert swapped
    assert (sentinel / "target.py").read_bytes() == b"unchanged"
