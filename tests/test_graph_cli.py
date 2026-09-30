"""`graph`·`reach`·`impact` 명령의 계약: 종료 코드, root 입력, root-not-found, revision, 문서 모양, 결정성."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.cli import graph_commands
from pythograph.cli.main import main
from pythograph.graph import revision as revision_module
from pythograph.graph.roots import RootsError, collect_roots, parse_roots_text
from tests.conftest import FIXTURES, GENERATED_AT, run_cli

#: 해석 fixture다.
RESOLUTION = str(FIXTURES / "graph" / "resolution")

#: 종단 검증 fixture다.
SHOP = str(FIXTURES / "e2e" / "shop-api")

#: 공통 인자다.
COMMON = ["--generated-at", GENERATED_AT, "--revision", "rev-1"]


def traversal(command: str, *arguments: str) -> tuple[int, dict[str, object], str]:
    """reach·impact를 실행해 문서를 돌려준다.

    Args:
        command: `reach` 또는 `impact`.
        arguments: 추가 인자.

    Returns:
        (종료 코드, 문서, 표준 오류).
    """
    code, out, err = run_cli([command, "--project", RESOLUTION, *COMMON, *arguments])
    return code, (json.loads(out) if out else {}), err


def reached_ids(document: dict[str, object]) -> list[str]:
    """도달 정점 id 목록이다.

    Args:
        document: 순회 문서.

    Returns:
        id 목록.
    """
    reached = document["reached"]
    assert isinstance(reached, list)
    return [entry["symbol"]["usr"] for entry in reached]


def test_help_topics() -> None:
    """도움말은 0이다."""
    for command in ("graph", "reach", "impact"):
        assert run_cli(["help", command])[1].startswith(f"Usage: pythograph {command}")
        assert run_cli([command, "--help"])[0] == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["graph"],
        ["graph", "--project", RESOLUTION, "extra"],
        ["graph", "--project", RESOLUTION, "--format", "yaml"],
        ["graph", "--project", RESOLUTION, "--revision", "a\nb"],
        ["graph", "--project", RESOLUTION, "--revision", "x" * 257],
        ["graph", "--project", RESOLUTION, "--generated-at", "yesterday"],
        ["graph", "--project", RESOLUTION, "--project", RESOLUTION],
        ["graph", "--project"],
        ["graph", "--nope"],
        ["reach", "--project", RESOLUTION],
        ["reach", "--project", RESOLUTION, "--dispatch", "maybe", "a#b"],
        ["reach", "--project", RESOLUTION, "--max-depth", "0", "a#b"],
        ["reach", "--project", RESOLUTION, "--max-depth", "129", "a#b"],
        ["reach", "--project", RESOLUTION, "--max-reached", "100001", "a#b"],
        ["reach", "--project", RESOLUTION, "--max-reached", "x", "a#b"],
        ["impact", "--project", RESOLUTION, "a\u0007b"],
        ["impact", "--project", RESOLUTION, "--", ""],
        ["impact", "a#b"],
    ],
)
def test_usage_errors_exit_64_without_output(arguments: list[str]) -> None:
    """사용법 오류는 64이고 표준 출력이 비어 있다."""
    code, out, err = run_cli(arguments)
    assert code == 64
    assert out == ""
    assert err.startswith("pythograph: ")


def test_unreadable_project_exits_2(tmp_path: Path) -> None:
    """디렉터리가 아닌 project는 2다."""
    assert run_cli(["graph", "--project", str(tmp_path / "missing")])[0] == 2
    assert run_cli(["reach", "--project", str(tmp_path / "missing"), "a#b"])[0] == 2


def test_graph_snapshot_shape() -> None:
    """스냅샷은 형식·정점·간선·집계·한계·graphRevision을 싣는다."""
    code, out, _ = run_cli(["graph", "--project", RESOLUTION, *COMMON])
    assert code == 0
    document = json.loads(out)
    assert document["format"] == "pythograph-graph"
    assert document["revision"] == "rev-1"
    assert document["graphRevision"].startswith("sha256:")
    node = next(item for item in document["nodes"] if item["id"] == "app/core/services.py#dynamic_calls")
    assert node["unresolvedCalls"] == {"bound": 8, "candidates": 8, "direct": 8}
    module = next(item for item in document["nodes"] if item["kind"] == "module")
    assert set(module["location"]) == {"path"}


def test_reach_document_contract() -> None:
    """reach 문서는 language-traversal v1 필드와 dispatch 선언(근거 등급·미해석 수 신고)을 싣는다."""
    code, document, _ = traversal("reach", "app/core/services.py#inexact_calls", "app/core/services.py#dynamic_calls")
    assert code == 0
    assert document["format"] == "language-traversal"
    assert document["platform"] == "python"
    assert document["direction"] == "dependencies"
    assert document["dispatch"] == "direct"
    assert document["truncated"] is False
    roots = document["roots"]
    assert isinstance(roots, list)
    assert roots[1] == {
        "id": "app/core/services.py#dynamic_calls",
        "symbol": {"qualifiedName": "app/core/services.py#dynamic_calls", "usr": "app/core/services.py#dynamic_calls"},
        "unresolvedCalls": 8,
    }
    reached = document["reached"]
    assert isinstance(reached, list)
    assert all(entry["evidence"] == "direct" for entry in reached)
    assert "app/core/models.py#Left.label" not in reached_ids(document)


def test_candidates_mode_adds_candidate_evidence() -> None:
    """candidates 모드는 재정의 후보를 candidate 등급으로 싣고 그 호출을 미해석에서 뺀다."""
    code, document, _ = traversal("reach", "--dispatch", "candidates", "app/core/services.py#inexact_calls")
    assert code == 0
    reached = {entry["symbol"]["usr"]: entry for entry in document["reached"]}  # type: ignore[union-attr]
    assert reached["app/core/models.py#Left.label"]["evidence"] == "candidate"
    assert reached["app/core/models.py#Base.describe"]["evidence"] == "direct"
    assert "unresolvedCalls" not in document["roots"][0]  # type: ignore[index]


def test_bound_mode_follows_bound_edges() -> None:
    """bound 모드는 닫힌 흐름의 bound 간선(`build()` 반환 값 → `Plain.label`)을 따라가고 그 호출을 미해석에서 뺀다."""
    _, bound, _ = traversal("reach", "--dispatch", "bound", "app/core/services.py#inexact_calls")
    _, direct, _ = traversal("reach", "app/core/services.py#inexact_calls")
    assert bound["dispatch"] == "bound"
    reached = {entry["symbol"]["usr"]: entry for entry in bound["reached"]}  # type: ignore[union-attr]
    assert reached["app/core/models.py#Plain.label"]["evidence"] == "bound"
    assert set(reached_ids(direct)) < set(reached)
    assert "app/core/models.py#Plain.label" not in reached_ids(direct)
    assert "unresolvedCalls" not in bound["roots"][0]  # type: ignore[index]
    assert direct["roots"][0]["unresolvedCalls"] == 1  # type: ignore[index]
    lines = [str(line) for line in bound["limitations"]]  # type: ignore[union-attr]
    assert any(line.startswith("bound-dispatch: 1 calls") for line in lines)
    assert not any(str(line).startswith("bound-") for line in direct["limitations"])  # type: ignore[union-attr]


def test_impact_is_reverse() -> None:
    """impact는 root에 기대는 정점(호출자)을 낸다."""
    code, document, _ = traversal("impact", "app/helpers.py#slugify")
    assert code == 0
    assert document["direction"] == "dependents"
    assert "app/plugins/extra.py#use_star" in reached_ids(document)


def test_root_not_found_writes_document_and_exits_64() -> None:
    """정점이 아닌 root는 symbol 없이 싣고 문서를 쓴 뒤 64로 끝난다."""
    code, document, err = traversal("reach", "nope#x", "app/core/services.py#nested")
    assert code == 64
    assert "not graph nodes" in err
    assert document["roots"][0] == {"id": "nope#x"}  # type: ignore[index]
    assert document["truncated"] is True
    assert document["truncationReasons"] == ["root-not-found"]
    assert any(str(line).startswith("root-not-found:") for line in document["limitations"])  # type: ignore[union-attr]
    nested = next(entry for entry in document["reached"] if entry["symbol"]["usr"].endswith("nested.inner"))  # type: ignore[union-attr]
    assert nested["roots"] == [1]


def test_only_missing_roots_give_empty_reached() -> None:
    """정점인 root가 없으면 reached가 비어 있다."""
    code, document, _ = traversal("impact", "nope#x")
    assert code == 64
    assert document["reached"] == []


def test_roots_from_file_and_stdin(tmp_path: Path) -> None:
    """`--roots-from`은 JSON 배열·bridge-facts 문서·표준 입력을 받고 위치 인자 뒤에 잇는다."""
    array = tmp_path / "roots.json"
    array.write_text(json.dumps(["app/core/services.py#nested", "app/helpers.py#slugify"]), encoding="utf-8")
    code, document, _ = traversal("reach", "--roots-from", str(array), "app/helpers.py#slugify")
    assert code == 0
    assert [root["id"] for root in document["roots"]] == ["app/helpers.py#slugify", "app/core/services.py#nested"]  # type: ignore[union-attr]
    facts = {"format": "bridge-facts", "facts": [{"symbol": {"usr": "app/helpers.py#dump"}}, {"symbol": {}}, 3]}
    stdin = io.StringIO(json.dumps(facts))
    out, err = io.StringIO(), io.StringIO()
    code = main(["impact", "--project", RESOLUTION, *COMMON, "--roots-from", "-"], stdout=out, stderr=err, stdin=stdin)
    assert code == 0, err.getvalue()
    assert json.loads(out.getvalue())["roots"][0]["id"] == "app/helpers.py#dump"


@pytest.mark.parametrize("text", ["{", "[1]", '{"format": "other"}', "3"])
def test_roots_from_rejects_other_json(text: str) -> None:
    """JSON 문자열 배열이나 bridge-facts가 아니면 사용법 오류다."""
    with pytest.raises(RootsError):
        parse_roots_text(text)


def test_roots_input_limits(tmp_path: Path) -> None:
    """읽지 못하는 파일·중복·root 상한을 다룬다."""
    with pytest.raises(RootsError):
        collect_roots([], str(tmp_path / "missing.json"), io.StringIO())
    assert collect_roots(["a#b", "a#b"], None, io.StringIO()) == ("a#b",)
    with pytest.raises(RootsError):
        collect_roots([f"a#{index}" for index in range(10_001)], None, io.StringIO())
    with pytest.raises(RootsError):
        collect_roots([], "-", io.StringIO("x" * (16 * 1024 * 1024 + 1)))


def test_depth_and_reached_limits_truncate() -> None:
    """깊이·도달 상한은 잘림 이유로 알린다."""
    _, shallow, _ = traversal("reach", "--max-depth", "1", "app/core/services.py#nested")
    assert shallow["truncationReasons"] == ["depth"]
    _, capped, _ = traversal("reach", "--max-reached", "1", "app/core/services.py#exact_calls")
    assert capped["truncationReasons"] == ["max-reached"]
    assert len(capped["reached"]) == 1  # type: ignore[arg-type]


def test_output_is_deterministic() -> None:
    """같은 입력이면 같은 바이트이고 graphRevision은 명령·모드와 무관하게 같다."""
    first = run_cli(["reach", "--project", SHOP, *COMMON, "store/views.py#OrderViewSet.retrieve"])
    second = run_cli(["reach", "--project", SHOP, *COMMON, "store/views.py#OrderViewSet.retrieve"])
    assert first == second
    graph = json.loads(run_cli(["graph", "--project", SHOP, *COMMON])[1])
    impact = json.loads(
        run_cli(["impact", "--project", SHOP, *COMMON, "--dispatch", "candidates", "store/audit.py#record"])[1]
    )
    assert json.loads(first[1])["graphRevision"] == graph["graphRevision"] == impact["graphRevision"]


def test_oversized_output_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """스냅샷 상한을 넘으면 부분 문서 없이 2다. 순회 문서는 isthmus 입력 상한(16 Mi)을 그대로 따른다."""
    monkeypatch.setattr("pythograph.graph.document.MAX_SNAPSHOT_LENGTH", 100)
    code, out, err = run_cli(["graph", "--project", RESOLUTION])
    assert (code, out) == (2, "")
    assert "exceed" in err
    assert "reach/impact" in err
    assert "isthmus rejects" not in err
    monkeypatch.setattr("pythograph.exchange.document.MAX_OUTPUT_LENGTH", 100)
    assert run_cli(["graph", "--project", RESOLUTION])[0] == 2
    monkeypatch.setattr("pythograph.graph.document.MAX_SNAPSHOT_LENGTH", 256 * 1024 * 1024)
    assert run_cli(["graph", "--project", RESOLUTION])[0] == 0
    code, out, err = traversal("reach", "app/core/services.py#inexact_calls")
    assert (code, out) == (2, {})
    assert "isthmus rejects" in err


def test_large_snapshot_exceeds_isthmus_cap_but_is_written() -> None:
    """합성 대형 그래프(16 Mi 문자 초과) 스냅샷도 쓴다. 보통 크기의 스냅샷은 순회 문서와 같은 직렬화로 바이트가 같다."""
    from datetime import datetime, timezone

    from pythograph.exchange.document import MAX_OUTPUT_LENGTH, DocumentLimitError, encode_document
    from pythograph.graph.document import GraphHeader, build_graph_document, encode_snapshot
    from pythograph.graph.model import CallGraph, GraphEdge, GraphNode, NodeLocation

    count = 34_000
    nodes = [
        GraphNode(
            f"pkg/module_{index // 40}.py#Service_{index}.handle_{index}",
            "method",
            NodeLocation(f"pkg/module_{index // 40}.py", index + 1, 5),
            {"bound": 1, "candidates": 0, "direct": 2},
            {"overridden-method": 1, "untyped-receiver": 1},
        )
        for index in range(count)
    ]
    edges = [
        GraphEdge(nodes[index % count].id, nodes[(index * 7 + 3) % count].id, ("call",), "direct")
        for index in range(2 * count)
    ]
    graph = CallGraph(nodes, edges, {"direct": [], "bound": [], "candidates": []}, {})
    header = GraphHeader("0", datetime(2026, 1, 1, tzinfo=timezone.utc), "/synthetic", None)
    document = build_graph_document(header, graph)
    text = encode_snapshot(document)
    assert len(text) > MAX_OUTPUT_LENGTH
    assert json.loads(text)["nodes"][-1]["id"] == nodes[-1].id
    with pytest.raises(DocumentLimitError):
        encode_document(document)
    small = build_graph_document(header, CallGraph(nodes[:50], edges[:50], graph.limitations, {}))
    assert encode_snapshot(small) == encode_document(small)


def _git(root: Path, *arguments: str) -> None:
    """테스트 저장소에서 git을 실행한다.

    Args:
        root: 저장소.
        arguments: git 인자.
    """
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_project(make_project: Callable[[dict[str, str]], Path]) -> Path:
    """커밋 하나가 있는 git 프로젝트다.

    Args:
        make_project: 합성 프로젝트 도우미.

    Returns:
        프로젝트 경로.
    """
    root = make_project({"app.py": "def f():\n    return 1\n"})
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_revision_is_head_only_when_clean(git_project: Path) -> None:
    """깨끗한 작업 트리면 HEAD, 변경·추적 안 하는 파일이 있으면 revision이 없다."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_project, capture_output=True, text=True, check=True)
    assert revision_module.git_revision(git_project) == head.stdout.strip()
    document = json.loads(run_cli(["graph", "--project", str(git_project), "--generated-at", GENERATED_AT])[1])
    assert document["revision"] == head.stdout.strip()
    (git_project / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert revision_module.git_revision(git_project) is None
    assert "revision" not in json.loads(run_cli(["graph", "--project", str(git_project)])[1])


def test_revision_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """저장소가 아니거나 git을 실행하지 못하면 revision이 없다."""
    assert revision_module.git_revision(tmp_path) is None
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(revision_module.subprocess, "run", _raise_os_error)
    assert revision_module.git_revision(tmp_path) is None


def _raise_os_error(*_arguments: object, **_keywords: object) -> object:
    """git 실행 실패를 흉내 낸다.

    Raises:
        OSError: 항상.
    """
    raise OSError("git missing")


def test_parse_command_line_separator() -> None:
    """`--` 뒤 인자는 `-`로 시작해도 root id다."""
    line = graph_commands.parse_command_line(["--project=x", "--", "--weird"], ("--project",), "", positional=True)
    assert line.values == {"--project": "x"}
    assert line.positional == ["--weird"]
