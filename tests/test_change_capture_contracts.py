"""캡처 전후에 경로·스캔 누락·읽기 경계를 보존하는 실제 Git 합성 회귀다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pythograph.cli.common import InputError
from pythograph.graph import change
from pythograph.graph.build import build_graph
from pythograph.graph.traversal import TraversalRequest
from pythograph.source.project import Project, is_test_path


class ChangeCaptureContracts(unittest.TestCase):
    """다른 프로젝트·프로세스에 접근하지 않는 일회용 저장소다."""

    def setUp(self) -> None:
        """분석에 필요한 파일과 Git identity만 로컬 fixture에 만든다."""
        self.temporary = tempfile.TemporaryDirectory(prefix="pythograph-capture-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("value.py", "def value():\n    return 1\n")
        self.write("caller.py", "from value import value\ndef caller():\n    return value()\n")

    def git(self, *arguments: str) -> str:
        """소유한 임시 저장소에서만 Git을 실행한다."""
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=", "-C", str(self.root), *arguments], text=True
        ).strip()

    def write(self, path: str, text: str) -> None:
        """fixture의 프로젝트 상대 파일을 쓴다."""
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self) -> None:
        """실제 before 입력을 로컬 객체로 고정한다."""
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def report(self, dispatch: str = "direct") -> dict:
        """실제 두 캡처와 기존 그래프/순회 엔진을 실행한다."""
        return change.analyze_change(
            self.root, "HEAD", None, TraversalRequest((), "dependents", 128, 100_000, dispatch)
        )

    def test_test_candidates_share_the_source_path_policy(self) -> None:
        """suffix·conftest 양성과 testimonials 음성을 공통 정책과 함께 검증한다."""
        paths = ("value_test.py", "conftest.py", "tests.py", "test_value.py", "tests/check.py", "testimonials.py")
        for path in paths:
            self.write(path, "from value import value\ndef review():\n    return value()\n")
        self.commit()
        self.write("value.py", "def value():\n    return 2\n")
        candidates = {row["path"]: row["testCandidate"] for row in self.report()["after"]["reviewCandidates"]}
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(candidates[path], is_test_path(path))

    def test_nested_traversals_keep_original_project_identity(self) -> None:
        """두 순회 문서는 임시 materialization 경로 대신 원 프로젝트 realpath를 쓴다."""
        self.commit()
        result = self.report()
        for side in ("before", "after"):
            self.assertEqual(result[side]["traversal"]["project"], self.root.as_posix())

    def bound_fixture(self) -> None:
        """모듈 함수와 함수 안 지역 함수의 bound 양성을 함께 만든다."""
        self.write("repos.py", "class Repo:\n    def save(self):\n        return 1\n")
        self.write(
            "app.py",
            "from repos import Repo\ndef store(repo):\n    return repo.save()\n"
            "def main():\n    def inner(repo):\n        return repo.save()\n"
            "    inner(Repo())\n    return store(Repo())\n",
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_source_link_omission_preserves_open_program_evidence(self) -> None:
        """원래 스캔이 여는 모듈 함수를 캡처 후 bound로 승격하지 않는다."""
        self.bound_fixture()
        (self.root / "hidden.py").symlink_to("absent-fixture.py")
        self.commit()
        original = build_graph(Project.open(self.root), include_tests=True)
        self.assertTrue(original.statistics["boundDispatch"]["scanIncomplete"])
        self.write("repos.py", "class Repo:\n    def save(self):\n        return 2\n")
        result = self.report("bound")
        rows = {row["id"]: row for row in result["after"]["reviewCandidates"]}
        self.assertNotIn("app.py#store", rows)
        self.assertEqual(rows["app.py#main.inner"]["evidence"], "bound")
        self.assertIn("caller.py#caller", {node.id for node in original.nodes})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_working_capture_rechecks_a_link_swap(self) -> None:
        """사전 검사 뒤 교체된 링크의 별도 소유 sentinel을 읽지 않는다."""
        self.commit()
        sentinel = self.root / ".owned-sentinel"
        sentinel.write_bytes(b"outside-capture")
        original = change._regular_file

        def replace_after_check(root: Path, relative: str) -> bool:
            """최종 파일의 기존 precheck 바로 뒤에서 한 번만 교체한다."""
            regular = original(root, relative)
            if relative == "value.py":
                (root / relative).unlink()
                (root / relative).symlink_to(sentinel)
            return regular

        with (
            patch.object(change, "_regular_file", side_effect=replace_after_check),
            self.assertRaises((InputError, OSError)),
        ):
            change.capture(self.root)
        self.assertEqual(sentinel.read_bytes(), b"outside-capture")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_non_source_link_does_not_erase_closed_program_facts(self) -> None:
        """문서 링크만 빠진 경우 정상 모듈 함수의 bound 양성을 보존한다."""
        self.bound_fixture()
        (self.root / "README.md").symlink_to("absent-owned-document")
        self.commit()
        self.write("repos.py", "class Repo:\n    def save(self):\n        return 2\n")
        result = self.report("bound")
        rows = {row["id"]: row for row in result["after"]["reviewCandidates"]}
        self.assertEqual(rows["app.py#store"]["evidence"], "bound")
        self.assertEqual(
            result["after"]["input"]["omissions"],
            [{"path": "README.md", "reason": "symlink", "sourceMayBeHidden": False}],
        )

    def test_submodule_omission_and_real_deletion_are_distinct(self) -> None:
        """gitlink 소스 경계는 미확인으로, 확인한 파일 삭제는 실제 부재로 남긴다."""
        self.bound_fixture()
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},hidden")
        self.git("commit", "-qm", "synthetic gitlink")
        before = change.capture(self.root, "HEAD")
        self.assertEqual(
            [(item.path, item.reason, item.may_hide_source) for item in before.omissions],
            [("hidden", "submodule", True)],
        )
        (self.root / "value.py").unlink()
        current = change.capture(self.root)
        deleted = next(item for item in current.omissions if item.path == "value.py")
        self.assertEqual((deleted.reason, deleted.may_hide_source), ("missing", False))

    def test_cli_reports_canonical_project_and_test_candidates(self) -> None:
        """설치 콘솔과 같은 실제 -m CLI가 두 계약을 함께 유지한다."""
        self.write("value_test.py", "from value import value\ndef check():\n    return value()\n")
        self.commit()
        self.write("value.py", "def value():\n    return 2\n")
        result = subprocess.run(
            [sys.executable, "-m", "pythograph", "change-impact", "--project", str(self.root), "--base", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["after"]["traversal"]["project"], self.root.as_posix())
        row = next(item for item in document["after"]["reviewCandidates"] if item["path"] == "value_test.py")
        self.assertIs(row["testCandidate"], True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_cli_fifo_swap_is_a_bounded_input_failure(self) -> None:
        """사전 검사 뒤 FIFO로 바뀐 파일을 실제 CLI가 열어 기다리지 않는다."""
        self.commit()
        program = """
import os, sys
from pythograph.graph import change
from pythograph.cli.main import main
original = change._regular_file
def swap(root, path):
    result = original(root, path)
    if path == 'value.py':
        (root / path).unlink()
        os.mkfifo(root / path)
    return result
change._regular_file = swap
raise SystemExit(main())
"""
        result = subprocess.run(
            [sys.executable, "-c", program, "change-impact", "--project", str(self.root), "--base", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_non_utf8_names_without_partial_output(self) -> None:
        """파일시스템 지원과 무관하게 Git의 raw 이름 바이트를 주입해 CLI 입력 오류를 검증한다."""
        self.commit()
        program = """
from pythograph.graph import change
from pythograph.cli.main import main
original = change._read_git
def listing(root, *arguments):
    if arguments[0] == 'ls-files':
        return b'bad-\\xff.py\\x00'
    return original(root, *arguments)
change._read_git = listing
raise SystemExit(main())
"""
        result = subprocess.run(
            [sys.executable, "-c", program, "change-impact", "--project", str(self.root), "--base", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
