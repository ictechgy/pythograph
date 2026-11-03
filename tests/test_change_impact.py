"""실제 Git before/after 입력으로 변경 분석을 검증한다."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pythograph.cli.common import InputError, UsageError
from pythograph.cli.main import run
from pythograph.graph import change as change_module
from pythograph.graph.change import analyze_change, capture, changed_files, content_digest
from pythograph.graph.traversal import TraversalRequest


def git(root: Path, *args: str) -> str:
    """합성 fixture 저장소에서만 git을 실행한다."""
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """직접 호출과 연결된 테스트를 포함하는 합성 저장소다."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    (tmp_path / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "from value import value\ndef caller():\n    return value()\n", encoding="utf-8"
    )
    (tmp_path / "test_value.py").write_text(
        "from caller import caller\ndef test_value():\n    assert caller() == 1\n", encoding="utf-8"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def report(root: Path, **options: object) -> dict:
    """CLI의 실제 JSON 결과를 읽는다."""
    args = ["change-impact", "--project", str(root), "--base", "HEAD"]
    for key, value in options.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    return json.loads(run(args))


def test_deleted_caller_is_preserved_without_revision_mixing(repository: Path) -> None:
    (repository / "value.py").unlink()
    (repository / "caller.py").write_text("def caller():\n    return 2\n", encoding="utf-8")
    result = report(repository)
    old = result["before"]["traversal"]
    assert {"caller.py#caller", "test_value.py#test_value"} <= {row["symbol"]["usr"] for row in old["reached"]}
    assert "value.py#value" not in {row["id"] for row in result["after"]["traversal"]["roots"]}
    assert result["status"] == "incomplete"
    assert result["before"]["input"]["contentDigest"] != result["after"]["input"]["contentDigest"]
    assert any(row["testCandidate"] for row in result["before"]["reviewCandidates"])


def test_body_edit_and_rename_have_content_identity(repository: Path) -> None:
    before = capture(repository, "HEAD")
    (repository / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    assert before.digest != capture(repository).digest
    assert [row["path"] for row in changed_files(before, capture(repository))] == ["value.py"]
    git(repository, "checkout", "--", "value.py")
    (repository / "value.py").rename(repository / "renamed.py")
    result = report(repository)
    assert next(row for row in result["changes"] if row["path"] == "renamed.py")["renameCandidateFrom"] == "value.py"
    assert "value.py#value" in {row["id"] for row in result["before"]["seeds"]}
    assert "renamed.py#value" in {row["id"] for row in result["after"]["seeds"]}


def test_head_depth_output_and_empty_are_explicit(repository: Path) -> None:
    empty = report(repository, head="HEAD")
    assert empty["changes"] == [] and empty["status"] == "incomplete"
    (repository / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    depth = report(repository, max_depth=1)
    assert "depth" in depth["before"]["traversal"]["truncationReasons"]
    capped = report(repository, max_reached=1)
    assert "max-reached" in capped["before"]["traversal"]["truncationReasons"]
    assert capped["limits"]["maxReached"] == 1
    old = json.loads(run(["impact", "--project", str(repository), "value.py#value"]))
    assert old["format"] == "language-traversal"


def test_unmapped_settings_and_framework_unknown_are_not_safe(repository: Path) -> None:
    (repository / "config.json").write_text('{"middleware":"a"}', encoding="utf-8")
    result = report(repository)
    assert result["after"]["unmappedFiles"] == ["config.json"]
    assert any("framework-unknown" in item for item in result["limitations"])
    assert result["status"] == "incomplete"
    assert content_digest({"a": b"x"}) != content_digest({"b": b"x"})


@pytest.mark.parametrize("arguments", [[], ["--max-depth", "0"], ["--max-depth", "x"], ["--dispatch", "bad"]])
def test_usage_errors(arguments: list[str], repository: Path) -> None:
    prefix = [] if not arguments else ["--project", str(repository), "--base", "HEAD"]
    with pytest.raises(UsageError):
        run(["change-impact", *prefix, *arguments])


def test_help_input_failures_and_budget(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "before/after" in run(["change-impact", "--help"]).lower()
    assert "before/after" in run(["help", "change-impact"]).lower()
    with pytest.raises(InputError):
        capture(repository, "not-a-ref")
    outside = repository / "outside"
    outside.mkdir()
    with pytest.raises(InputError, match="repository root"):
        capture(outside)
    monkeypatch.setattr("pythograph.graph.change.MAX_FILE_BYTES", 1)
    with pytest.raises(InputError, match="byte budget"):
        capture(repository)


def test_interface_callback_patch_preserves_dispatch_limits(repository: Path) -> None:
    (repository / "value.py").write_text(
        "class Base:\n    def run(self): return 1\nclass Impl(Base):\n    def run(self): return 2\n"
        "def callback(fn): return fn()\ndef value(): return callback(Impl().run)\n",
        encoding="utf-8",
    )
    result = analyze_change(repository, "HEAD", None, TraversalRequest((), "dependents", 128, 100000, "candidates"))
    assert result["status"] == "incomplete"
    assert result["after"]["traversal"]["dispatch"] == "candidates"  # type: ignore[index]


def test_unsafe_filter_is_cli_input_failure(repository: Path) -> None:
    git(repository, "config", "filter.bad=name.clean", "unused-command")
    result = subprocess.run(
        [sys.executable, "-m", "pythograph", "change-impact", "--project", str(repository), "--base", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "Git capture refused" in result.stderr


def test_partial_clone_refusal_is_input_error(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythograph.graph.revision import _LazyFetchRefusedError

    def refuse(*args: object, output_limit: int) -> bytes:
        """새 캡처도 출력 예산을 전달한 뒤 지연 fetch 거부를 보존해야 한다."""
        assert output_limit > 0
        raise _LazyFetchRefusedError

    monkeypatch.setattr("pythograph.graph.change._git", refuse)
    with pytest.raises(InputError, match="Git capture refused"):
        capture(repository)


@pytest.mark.parametrize("staged", [False, True])
def test_staged_and_unstaged_delete_preserve_old_caller(repository: Path, staged: bool) -> None:
    (repository / "value.py").unlink()
    if staged:
        git(repository, "add", "-u")
    result = report(repository)
    assert {"path": "value.py", "status": "deleted"} in result["changes"]
    assert "caller.py#caller" in {row["id"] for row in result["before"]["reviewCandidates"]}
    assert not result["after"]["seeds"]


def test_staged_edit_reverted_in_worktree_uses_net_source_state(repository: Path) -> None:
    original = (repository / "value.py").read_bytes()
    (repository / "value.py").write_text("def value():\n    return 999\n", encoding="utf-8")
    git(repository, "add", "value.py")
    (repository / "value.py").write_bytes(original)
    result = report(repository)
    assert result["changes"] == []
    assert result["before"]["input"]["contentDigest"] == result["after"]["input"]["contentDigest"]
    assert result["status"] == "incomplete"


def test_non_python_files_use_blob_identity_outside_byte_budget(repository: Path) -> None:
    """그래프가 읽지 않는 큰 파일은 바이트 예산에 넣지 않고 git blob id로 바뀜만 비교한다."""
    blob = bytes(range(256)) * (5 * 4096 + 1)
    (repository / "data.bin").write_bytes(blob)
    (repository / "same.bin").write_bytes(b"unchanged")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "binary")
    committed = capture(repository, "HEAD")
    assert "data.bin" not in committed.files and "data.bin" in committed.others
    assert committed.others["data.bin"] == git(repository, "rev-parse", "HEAD:data.bin").strip()
    assert capture(repository).others == committed.others
    (repository / "data.bin").write_bytes(blob + b"!")
    (repository / "untracked.bin").write_bytes(blob)
    result = report(repository)
    rows = {row["path"]: row["status"] for row in result["changes"]}
    assert rows == {"data.bin": "modified", "untracked.bin": "added"}
    assert result["after"]["input"]["identityOnlyFiles"] == 3


def test_commit_capture_reads_blobs_in_one_batch(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """커밋 캡처는 파일 수와 무관하게 git을 몇 번만 실행한다(파일마다 cat-file 두 번이 아니다)."""
    for index in range(30):
        (repository / f"module_{index}.py").write_text(f"def f{index}():\n    return {index}\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "many")
    calls: list[tuple[str, ...]] = []
    original = change_module._git

    def counting(*args: object, **kwargs: object) -> bytes | None:
        calls.append(tuple(str(item) for item in args[2:]))
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(change_module, "_git", counting)
    captured = capture(repository, "HEAD")
    assert len(captured.files) == 33
    assert [call[0] for call in calls] == ["rev-parse", "ls-tree", "cat-file"]


def test_hash_budget_and_batch_output_are_checked(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """비분석 파일 해시 예산 초과와 예상과 다른 일괄 읽기 출력은 부분 분석 없이 입력 오류다."""
    (repository / "notes.txt").write_text("hello\n", encoding="utf-8")
    with monkeypatch.context() as patched:
        patched.setattr(change_module, "MAX_HASH_BYTES", 1)
        with pytest.raises(InputError, match="hash budget"):
            capture(repository)
    original = change_module._read_git

    def corrupt(root: Path, *args: str, **kwargs: object) -> bytes:
        if args and args[0] == "cat-file":
            return b"not a batch header\n"
        return original(root, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(change_module, "_read_git", corrupt)
    with pytest.raises(InputError, match="unexpected object data"):
        capture(repository, "HEAD")
