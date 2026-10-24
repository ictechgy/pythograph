"""실제 작은 소유 자식으로 Git 캡처의 stdout·stderr 예산과 정리를 검증한다."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pythograph.cli.common import InputError
from pythograph.graph import change, revision


@unittest.skipUnless(os.name == "posix", "executable synthetic child requires POSIX")
class ChangeGitBudget(unittest.TestCase):
    """프로젝트 코드 대신 독립 sentinel 프로그램만 실행한다."""

    def setUp(self) -> None:
        """각 사례의 실행 파일·프로젝트를 일회용 디렉터리에 한정한다."""
        self.temporary = tempfile.TemporaryDirectory(prefix="pythograph-git-budget-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program = self.root / "synthetic-git"

    def child(self, body: str) -> None:
        """주어진 고정 문장만 실행하는 합성 자식이다. 대상 소스를 import하지 않는다."""
        self.program.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n", encoding="utf-8")
        self.program.chmod(0o700)

    def test_stdout_stderr_and_combined_limits(self) -> None:
        """출력 초과가 어느 파이프에서 나와도 일반 성공을 돌려주지 않는다."""
        for body in (
            "os.write(1, b'x' * 8192)",
            "os.write(2, b'x' * 8192)",
            "os.write(1, b'x' * 20); os.write(2, b'y' * 20)",
        ):
            with self.subTest(body=body):
                self.child(body)
                self.assertIsNone(revision._run_git(str(self.program), self.root, "ls-files", output_limit=32))

    def test_exact_budget_preserves_bytes_and_actual_exit(self) -> None:
        """상한과 정확히 같으면 raw 바이트와 비0 종료 코드를 유지한다."""
        self.child("os.write(1, b'\\x00\\xffok'); os.write(2, b'warn'); sys.exit(7)")
        result = revision._run_git(str(self.program), self.root, "ls-files", output_limit=8)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b"\x00\xffok", b"warn"))

    def test_reader_failure_never_becomes_partial_success(self) -> None:
        """reader의 파일·핸들·메모리 실패도 빈 stdout 성공으로 승격하지 않는다."""
        self.child("os.write(1, b'ok')")
        read = os.read
        for failure in (OSError, ValueError, MemoryError):

            def failed_read(descriptor: int, size: int, error: type[Exception] = failure) -> bytes:
                """Popen 자신의 exec 오류 파이프는 유지하고 출력 reader에서만 실패한다."""
                if threading.current_thread() is threading.main_thread():
                    return read(descriptor, size)
                raise error

            with self.subTest(failure=failure.__name__), patch.object(revision.os, "read", side_effect=failed_read):
                self.assertIsNone(revision._run_git(str(self.program), self.root, "ls-files", output_limit=32))

    def test_timeout_is_failure_and_reaps_owned_child(self) -> None:
        """출력 없는 소유 자식도 시간 초과로 실패하고 즉시 수거한다."""
        self.child("time.sleep(10)")
        started = time.monotonic()
        with patch.object(revision, "GIT_TIMEOUT_SECONDS", 0.05):
            self.assertIsNone(revision._run_git(str(self.program), self.root, "ls-files", output_limit=32))
        self.assertLess(time.monotonic() - started, 2)

    def test_configuration_probe_is_bounded_before_main_command(self) -> None:
        """설정 출력도 본 명령보다 먼저 같은 예산으로 차단한다."""
        self.child("os.write(1, b'x' * 8192)")
        self.assertIsNone(revision._git(str(self.program), self.root, "ls-files", output_limit=32))

    def test_read_git_maps_overflow_to_input_failure(self) -> None:
        """실제 격리 Git seam의 초과가 빈 성공이나 traceback 대신 입력 오류가 된다."""
        self.child("sys.exit(1) if 'config' in sys.argv else os.write(1, b'x' * 8192)")
        with (
            patch.object(change, "MAX_TOTAL_BYTES", 32),
            patch.object(change, "_git_executable", return_value=str(self.program)),
            self.assertRaises(InputError),
        ):
            change._read_git(self.root, "ls-files", "-z")


if __name__ == "__main__":
    unittest.main()
