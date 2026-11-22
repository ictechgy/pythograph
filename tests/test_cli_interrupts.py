"""CLI 명령 스레드: 계산만 큰 스택 스레드에서 돌고, 출력·오류 쓰기와 Ctrl+C는 부른 스레드에서 처리한다."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pythograph.cli import main as cli_main
from pythograph.cli.common import InputError, UsageError
from pythograph.source import syntax
from pythograph.source.cancel import raise_if_cancelled
from pythograph.source.syntax import MAX_CHAIN_TOKENS
from tests.conftest import REPOSITORY
from tests.test_source import _record_start_confirmations, _record_thread_starts, _RefusedThread, _stacks

#: CLI 테스트가 쓰는 고정 생성 시각이다.
_GENERATED_AT = "2026-01-01T00:00:00.000Z"

#: POSIX 신호로 Ctrl+C를 흉내 내는 테스트의 건너뛰기 표시다. Windows의 Ctrl+C는 콘솔 이벤트라 이 방법으로 보낼 수 없고,
#: Windows에서 주 스레드가 0.1초마다 깨어나 Ctrl+C를 받는 대기는 이 테스트들이 확인하지 못한다.
POSIX_SIGNALS = pytest.mark.skipif(
    os.name != "posix", reason="POSIX 신호로 Ctrl+C를 흉내 낸다(Windows는 콘솔 이벤트다)"
)


def _child_environment(**extra: str) -> dict[str, str]:
    """자식 프로세스 환경이다. `PYTHONUNBUFFERED`를 빼야 표준 출력에 버퍼 잠금이 생겨 실제 사용과 같아진다."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
    environment.update(extra)
    return environment


#: `_interrupt_after_start`가 마지막으로 SIGINT를 보낸 시각(`time.monotonic`)이다.
_LAST_INTERRUPT = [0.0]


def _interrupt_after_start(process: subprocess.Popen[str], *, settle: float = 0.0) -> str:
    """자식이 표준 오류에 "started"를 쓰면(필요하면 조금 더 기다린 뒤) 프로세스 그룹에 SIGINT를 보내고 끝나기를
    기다린다(터미널의 Ctrl+C처럼). 남은 표준 오류를 돌려준다."""
    assert process.stderr is not None
    assert process.stderr.readline() == "started\n"
    time.sleep(settle)
    _LAST_INTERRUPT[0] = time.monotonic()
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
    return process.stderr.read()


def test_cli_commands_run_on_one_large_stack_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 명령의 계산은 어떤 허용 소스라도 그 자리에서 파싱할 크기의 스레드 하나에서 돌고, 그 안의 파싱은 스레드를 더
    만들지 않는다(파일마다 트리를 다른 코어에서 만들어 옮기던 비용이 없다). 출력은 부른 스레드가 쓴다. 3.14부터는
    스레드를 만들지 않는다."""
    started = _record_thread_starts(monkeypatch)
    padding = "#" * (syntax.INLINE_PARSE_CHARACTERS * 2)
    for index in range(3):
        (tmp_path / f"long{index}.py").write_bytes(f'x = f"{{{index}}}"  {padding}\n'.encode())
    writers: list[str] = []

    class RecordingOutput(io.StringIO):
        """쓴 스레드를 기록하는 출력이다."""

        def write(self, text: str) -> int:
            """쓴 스레드 이름을 기록하고 쓴다."""
            writers.append(threading.current_thread().name)
            return super().write(text)

    out = RecordingOutput()
    code = cli_main.main(["graph", "--project", str(tmp_path), "--revision", "r", "--generated-at", _GENERATED_AT], out)
    assert code == 0
    assert '"unparsedFiles":0' in out.getvalue().replace(" ", "")
    assert set(writers) == {threading.current_thread().name}
    largest = syntax.largest_parse_stack_bytes()
    assert _stacks(started) == ([largest] if largest else [])
    assert all(thread.daemon and not thread.is_alive() for thread in started)
    if sys.version_info < (3, 11):
        assert largest == syntax.parse_stack_bytes(MAX_CHAIN_TOKENS) + syntax.FSTRING_STACK_ALLOWANCE_BYTES
    elif sys.version_info < (3, 12):
        assert largest == syntax.MIN_PARSE_STACK_BYTES + syntax.FSTRING_STACK_ALLOWANCE_BYTES
    elif sys.version_info < (3, 14):
        assert largest == syntax.MIN_PARSE_STACK_BYTES
    else:
        assert largest == 0


def test_cli_maps_relayed_errors_to_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """계산 스레드에서 난 사용법 오류·입력 오류·MemoryError·다른 예외는 부른 스레드에서 전처럼 64·2·2·2가 되고,
    SystemExit는 그대로 나간다(9234b72의 SystemExit 전달 테스트를 되살렸다). 스레드를 만들 수 없으면(데몬 스레드를 막은
    하위 인터프리터 등) 계산은 부른 스레드에서 돈다."""
    monkeypatch.setattr(syntax, "largest_parse_stack_bytes", lambda: syntax.MIN_PARSE_STACK_BYTES)
    threads: list[str] = []

    def raising(error: BaseException) -> object:
        """계산 스레드를 기록하고 `error`를 낸다."""

        def run(_arguments: list[str], _stdin: object = None) -> str:
            """계산 스레드 이름을 기록하고 정한 예외를 낸다."""
            threads.append(threading.current_thread().name)
            raise error

        return run

    for error, code in (
        (UsageError("bad flag"), 64),
        (InputError("no project"), 2),
        (MemoryError(), 2),
        (KeyError(1), 2),
    ):
        monkeypatch.setattr(cli_main, "run", raising(error))
        err = io.StringIO()
        assert cli_main.main(["graph"], io.StringIO(), err) == code
        assert err.getvalue().startswith("pythograph: ")
    assert threads == ["pythograph-parse"] * 4
    monkeypatch.setattr(cli_main, "run", raising(SystemExit(64)))
    with pytest.raises(SystemExit) as stopped:
        cli_main.main(["graph"], io.StringIO(), io.StringIO())
    assert stopped.value.code == 64
    assert threads == ["pythograph-parse"] * 5
    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=_RefusedThread, stack_size=threading.stack_size))
    monkeypatch.setattr(cli_main, "run", lambda _arguments, _stdin=None: threading.current_thread().name)
    out = io.StringIO()
    assert cli_main.main(["graph"], out, io.StringIO()) == 0
    assert out.getvalue() == threading.current_thread().name


def test_errors_while_reporting_errors_still_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """오류를 알리다 다시 난 오류(메모리가 모자란 `ulimit -v`에서의 MemoryError)도 예약된 1이 아니라 2이고, 미리 만든
    고정 문구를 쓴다. 전에는 두 번째 MemoryError가 명령 스레드에서 주 스레드로 넘어와 파이썬이 1로 끝났다."""

    def failing(_arguments: list[str], _stdin: object = None) -> str:
        """계산이 메모리 부족으로 끝난 것처럼 한다."""
        raise MemoryError

    def failing_report(_err: object, _message: str) -> None:
        """오류를 알리는 중에도 메모리가 모자란 것처럼 한다."""
        raise MemoryError

    monkeypatch.setattr(cli_main, "run", failing)
    monkeypatch.setattr(cli_main, "_report", failing_report)
    err = io.StringIO()
    assert cli_main.main(["graph"], io.StringIO(), err) == 2
    assert err.getvalue() == cli_main._LAST_RESORT_MESSAGE


def test_closed_standard_streams_keep_the_exit_code_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """표준 출력·오류를 닫고 실행해도(`sys.stdout` 등이 None) 예약된 1이나 내부 오류가 되지 않는다: 출력을 못 쓰면
    2(표준 오류가 열려 있으면 원인 문구), 사용법 오류는 알릴 곳이 없어도 64다(닫힌 표준 입력은 다음 테스트)."""
    monkeypatch.setattr(cli_main, "run", lambda _arguments, _stdin=None: "{}\n")
    monkeypatch.setattr(sys, "stdout", None)
    err = io.StringIO()
    assert cli_main.main(["graph"], stderr=err) == 2
    assert err.getvalue() == (
        "pythograph: could not write the output (standard output is closed); check the output destination\n"
    )
    monkeypatch.setattr(sys, "stderr", None)
    assert cli_main.main(["graph"]) == 2
    monkeypatch.setattr(cli_main, "run", _usage_error)
    assert cli_main.main(["graph"]) == 64


def _usage_error(_arguments: list[str], _stdin: object = None) -> str:
    """언제나 사용법 오류를 낸다."""
    raise UsageError("bad flag")


def test_closed_stdin_roots_are_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """표준 입력을 닫고 `--roots-from -`을 주면 내부 오류(AttributeError, 2)가 아니라 roots 입력 오류(64)다.

    문구는 원인(닫힌 표준 입력)을 말한다. 전에는 깨진 입력과 같은 "UTF-8 text로 읽지 못했다" 문구였다.
    """
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", None)
    err = io.StringIO()
    code = cli_main.main(["reach", "--project", str(tmp_path), "--roots-from", "-"], io.StringIO(), err)
    assert code == 64
    assert err.getvalue() == (
        "pythograph: --roots-from - reads standard input, but standard input is closed; "
        "pipe the roots in or pass a file.\n"
    )


@pytest.mark.parametrize("inside_except", [False, True])
def test_interrupt_during_start_never_runs_the_command_twice(
    monkeypatch: pytest.MonkeyPatch, inside_except: bool
) -> None:
    """시작을 기다리던 중 Ctrl+C가 끊어 `Thread.start`가 "release unlocked lock"(앞 예외 KeyboardInterrupt)으로 끝나도,
    스레드가 이미 시작했으면 시작하지 못한 것으로 보지 않는다: 계산 스레드를 멈추게 하고 KeyboardInterrupt를 다시 낸다.
    전에는 부른 스레드에서 계산을 한 번 더 돌려 Ctrl+C를 잃고 출력이 두 번, 종료 코드 0이었다. 부른 쪽이 다른 예외를
    처리하는 중이어도 같다(시작 중에 난 예외만 끊김으로 본다)."""
    monkeypatch.setattr(syntax, "largest_parse_stack_bytes", lambda: syntax.MIN_PARSE_STACK_BYTES)
    calls: list[str] = []
    unwound: list[str] = []

    def counted(_arguments: list[str], _stdin: object = None) -> str:
        """실행한 스레드를 기록하고, 취소 확인 자리를 지나며 4초까지 돈다."""
        calls.append(threading.current_thread().name)
        try:
            for _ in range(400):
                time.sleep(0.01)
                raise_if_cancelled()
        finally:
            unwound.append(threading.current_thread().name)
        return "{}\n"

    class InterruptedStart(threading.Thread):
        """시작한 뒤 시작 대기가 Ctrl+C로 끊긴 것처럼 끝난다."""

        def start(self) -> None:
            """실제로 시작한 뒤 시작 대기가 끊긴 것처럼 실패를 보고한다."""
            super().start()
            time.sleep(0.05)
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise RuntimeError("release unlocked lock")  # noqa: B904 — 앞 예외(KeyboardInterrupt)를 남긴다

    monkeypatch.setattr(cli_main, "run", counted)
    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=InterruptedStart, stack_size=threading.stack_size))
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        if inside_except:
            try:
                raise KeyError("unrelated")
            except KeyError:
                cli_main.main(["graph"], out, io.StringIO())
        else:
            cli_main.main(["graph"], out, io.StringIO())
    assert calls == ["pythograph-parse"]
    assert unwound == ["pythograph-parse"]
    assert out.getvalue() == ""


def test_refused_threads_do_not_wait_once_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """스레드를 하나도 만들 수 없으면(실제 `ulimit -v`, 프로세스 수 한도, Windows 커밋 한도) 명령은 부른 스레드에서
    돌고, 파일마다 실패하는 스레드 시작이 시작 확인을 기다리지 않는다. 출력은 스레드가 있을 때와 바이트까지 같다. 전에는
    명령을 첫 시작 실패의 `except` 블록 안에서 돌려, 뒤의 시작 실패가 모두 "시작이 끊겼을 수 있다"로 보여 파일마다 1초를
    기다렸다(`ulimit -v 45000`에서 31개 파일 30.3초, 0.3.1은 0.26초)."""
    monkeypatch.setattr(syntax, "_PARSER_CHECKS_C_STACK", False)
    padding = "#" * (syntax.INLINE_PARSE_CHARACTERS * 2)
    for index in range(6):
        (tmp_path / f"long{index}.py").write_bytes(f"def f{index}():\n    return {index}  {padding}\n".encode())
    arguments = ["graph", "--project", str(tmp_path), "--revision", "r", "--generated-at", _GENERATED_AT]
    threaded = io.StringIO()
    assert cli_main.main(arguments, threaded) == 0
    confirmations = _record_start_confirmations(monkeypatch)
    attempts: list[str] = []

    class CountedRefusal(_RefusedThread):
        """시작 시도를 세고 언제나 실패한다."""

        def start(self) -> None:
            """시작을 시도한 스레드를 기록하고 실패한다."""
            attempts.append(threading.current_thread().name)
            super().start()

    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=CountedRefusal, stack_size=threading.stack_size))
    refused = io.StringIO()
    assert cli_main.main(arguments, refused) == 0
    assert refused.getvalue() == threaded.getvalue()
    assert '"unparsedFiles":0' in refused.getvalue().replace(" ", "")
    assert len(attempts) == 1 + 6  # 명령 스레드 하나와 파일마다 하나다.
    assert set(attempts) == {threading.current_thread().name}
    assert confirmations == []


@pytest.mark.skipif(os.name != "posix", reason="Windows의 `subprocess.communicate`는 0.3.1부터 읽기 스레드를 쓴다")
def test_refused_threads_still_record_the_git_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """스레드를 하나도 만들 수 없어도 git 작업 트리의 graph는 0.3.1처럼 revision을 담아 0으로 끝나고, 출력은 스레드가
    있을 때와 바이트까지 같다. 위 테스트는 `--revision`을 주어 git을 부르지 않는다. 전에는 revision 조회가 읽기 스레드
    둘을 시작하다 RuntimeError로 내부 오류(2)가 됐다(5차 리뷰 F1)."""
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for git_arguments in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *identity, *git_arguments], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True)
    arguments = ["graph", "--project", str(tmp_path), "--generated-at", _GENERATED_AT]
    threaded = io.StringIO()
    assert cli_main.main(arguments, threaded) == 0
    assert json.loads(threaded.getvalue())["revision"] == head.stdout.strip()

    def refuse(_thread: threading.Thread) -> None:
        """스레드 시작을 언제나 거부한다."""
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    refused = io.StringIO()
    assert cli_main.main(arguments, refused) == 0
    assert refused.getvalue() == threaded.getvalue()


#: 계산이 끝난 뒤 아무도 읽지 않는 파이프에 큰 출력을 쓰다 막힌 CLI다. 출력 쓰기가 명령 스레드에서 일어나던 때는
#: Ctrl+C 뒤 종료 처리가 표준 출력 버퍼 잠금을 얻지 못해 "Fatal Python error: _enter_buffered_busy"로 죽었다(SIGABRT).
_FULL_PIPE = r"""
import signal
import sys

signal.signal(signal.SIGINT, signal.default_int_handler)
from pythograph.cli import main as cli_main

cli_main.run = lambda _arguments, _stdin=None: "x" * 4_000_000
sys.stderr.write("started\n")
sys.stderr.flush()
raise SystemExit(cli_main.main(["graph"]))
"""


@POSIX_SIGNALS
def test_ctrl_c_while_output_is_blocked_on_a_pipe_exits_by_sigint() -> None:
    """`pythograph graph … | less`처럼 읽지 않는 파이프에 쓰다 Ctrl+C를 받으면 전처럼 KeyboardInterrupt로 끝난다(SIGINT
    종료 상태). 출력은 부른 스레드가 쓰므로 쓰기가 EINTR로 끊긴다."""
    process = subprocess.Popen(
        [sys.executable, "-c", _FULL_PIPE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=REPOSITORY,
        env=_child_environment(),
        start_new_session=True,
    )
    stderr = _interrupt_after_start(process, settle=1.0)
    assert process.returncode == -signal.SIGINT, stderr[-2000:]
    assert "Fatal Python error" not in stderr
    assert "KeyboardInterrupt" in stderr


#: 계산 중인 CLI다. 계산은 시작을 알리고 끝없이 돌며 취소 확인 자리(`raise_if_cancelled`, 실제 계산의 파일·디렉터리·git
#: 대기마다 있는 자리)를 지나고, 멈추면 `finally`에서 표시 파일을 쓴다(인자: 표시 파일).
_INTERRUPTED_COMPUTE = r"""
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, signal.default_int_handler)
from pythograph.cli import main as cli_main
from pythograph.source.cancel import raise_if_cancelled

marker = Path(sys.argv[1])


def endless(_arguments, _stdin=None):
    try:
        sys.stderr.write("started\n")
        sys.stderr.flush()
        while True:
            time.sleep(0.05)
            raise_if_cancelled()
    finally:
        marker.write_text("unwound", encoding="utf-8")


cli_main.run = endless
raise SystemExit(cli_main.main(["graph"]))
"""


@POSIX_SIGNALS
def test_ctrl_c_during_compute_unwinds_the_command_thread(tmp_path: Path) -> None:
    """계산 중 Ctrl+C를 받으면 계산 스레드의 취소 표시를 세워 다음 확인 자리에서 `finally`를 실행시킨 뒤 부른 스레드에서
    KeyboardInterrupt로 끝난다. 전에는 계산 스레드를 버려 `with`·`finally`가 돌지 않았다."""
    marker = tmp_path / "unwound"
    process = subprocess.Popen(
        [sys.executable, "-c", _INTERRUPTED_COMPUTE, str(marker)],
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=REPOSITORY,
        env=_child_environment(),
        start_new_session=True,
    )
    stderr = _interrupt_after_start(process)
    assert process.returncode == -signal.SIGINT, stderr[-2000:]
    assert "KeyboardInterrupt" in stderr
    assert marker.read_text(encoding="utf-8") == "unwound"


#: 비교 사본을 만드는 도중의 change-impact다. 사본 디렉터리에 파일을 하나 쓰고 시작을 알린 뒤 취소 확인 자리를 지나며
#: 끝없이 기다리고, 멈추면 `finally`에서 표시 파일을 쓴다(인자: 프로젝트, 표시 파일).
_INTERRUPTED_CHANGE = r"""
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, signal.default_int_handler)
from pythograph.cli import main as cli_main
from pythograph.graph import change
from pythograph.source.cancel import raise_if_cancelled

marker = Path(sys.argv[2])


def slow_analyze(_value, destination, *_arguments, **_options):
    destination.mkdir(parents=True, exist_ok=True)
    try:
        (destination / "copied.py").write_text("x = 1\n", encoding="utf-8")
        sys.stderr.write("started\n")
        sys.stderr.flush()
        while True:
            time.sleep(0.05)
            raise_if_cancelled()
    finally:
        marker.write_text("unwound", encoding="utf-8")


change._analyze = slow_analyze
raise SystemExit(cli_main.main(["change-impact", "--project", sys.argv[1], "--base", "HEAD"]))
"""


@POSIX_SIGNALS
def test_ctrl_c_during_change_impact_leaves_no_temporary_copy(tmp_path: Path) -> None:
    """change-impact가 비교 사본을 만드는 도중 Ctrl+C를 받으면 `with TemporaryDirectory`가 풀려 사본 디렉터리
    (`pythograph-change-*`)가 남지 않는다. 전에는 계산 스레드를 버려, 종료 처리의 rmtree가 아직 쓰는 스레드와 겨뤄
    프로젝트 소스 사본을 남기거나(ENOTEMPTY) `finally`를 건너뛰었다."""
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "f@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True)
    (repository / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    marker = tmp_path / "unwound"
    process = subprocess.Popen(
        [sys.executable, "-c", _INTERRUPTED_CHANGE, str(repository), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=REPOSITORY,
        env=_child_environment(TMPDIR=str(temporary)),
        start_new_session=True,
    )
    stderr = _interrupt_after_start(process)
    assert process.returncode == -signal.SIGINT, stderr[-2000:]
    assert marker.read_text(encoding="utf-8") == "unwound"
    assert sorted(path.name for path in temporary.iterdir()) == []
    assert "Directory not empty" not in stderr


#: 신호 처리기가 기다리는 부른 스레드에 예외를 내는 프로세스 안 호출이다(pytest-timeout 같은 SIGALRM 처리기).
_SIGNAL_HANDLER_EXCEPTION = r"""
import io
import signal
import threading
import time

from pythograph.cli import main as cli_main


def slow(_arguments, _stdin=None):
    for _ in range(40):
        time.sleep(0.05)
    return "late output"


def deadline(_signum, _frame):
    raise TimeoutError("deadline")


cli_main.run = slow
signal.signal(signal.SIGALRM, deadline)
signal.setitimer(signal.ITIMER_REAL, 0.3)
out, err = io.StringIO(), io.StringIO()
try:
    code = cli_main.main(["graph"], out, err)
except BaseException as error:
    code = type(error).__name__
time.sleep(3)
alive = [thread.name for thread in threading.enumerate() if thread.name.startswith("pythograph")]
print(code, len(out.getvalue()), alive, "internal error (TimeoutError)" in err.getvalue())
"""


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="SIGALRM 타이머가 없는 플랫폼이다")
def test_signal_handler_exception_stops_the_command() -> None:
    """프로세스 안에서 부른 `main`을 신호 처리기의 예외가 끊으면 계산 스레드도 멈추고, `main`이 돌아온 뒤 호출자의
    출력에 아무것도 쓰지 않는다(전처럼 내부 오류 2). 전에는 예외가 `main` 밖으로 나가고 계산은 계속돼 나중에 문서 전체를
    썼다."""
    result = subprocess.run(
        [sys.executable, "-c", _SIGNAL_HANDLER_EXCEPTION],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout == "2 0 [] True\n"


#: `--roots-from`(표준 입력 `-`이나 경로)을 기다리는 동안 Ctrl+C를 받는 reach다(인자: 프로젝트, `--roots-from` 값).
_BLOCKED_ROOTS = r"""
import signal
import sys

signal.signal(signal.SIGINT, signal.default_int_handler)
from pythograph.cli import main as cli_main

sys.stderr.write("started\n")
sys.stderr.flush()
raise SystemExit(cli_main.main(["reach", "--project", sys.argv[1], "--roots-from", sys.argv[2]]))
"""


@POSIX_SIGNALS
@pytest.mark.parametrize("source", ["stdin", "fifo"])
def test_ctrl_c_while_reading_roots_exits_promptly(tmp_path: Path, source: str) -> None:
    """`--roots-from`이 쓰는 쪽이 열어 둔 채 쓰지 않는 표준 입력이나 FIFO를 기다리는 동안 Ctrl+C를 받으면 바로
    KeyboardInterrupt로 끝난다. 열기와 읽기는 따로 된 데몬 스레드에서 해서 계산 스레드가 멈추기를 기다리지 않는다(C 읽기
    중에는 취소 표시를 볼 수 없다). 전에는 경로(FIFO·`/dev/stdin`)를 계산 스레드에서 읽어 20초를 기다렸다."""
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    argument = "-"
    writer = None
    if source == "fifo":
        argument = str(tmp_path / "roots.fifo")
        os.mkfifo(argument)
        keeper = os.open(argument, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(argument, os.O_WRONLY)
        os.close(keeper)
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _BLOCKED_ROOTS, str(tmp_path / "project"), argument],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            cwd=REPOSITORY,
            env=_child_environment(),
            start_new_session=True,
        )
        stderr = _interrupt_after_start(process, settle=1.0)
        stopped = time.monotonic()
    finally:
        if writer is not None:
            os.close(writer)
    assert process.returncode == -signal.SIGINT, stderr[-2000:]
    assert "Fatal Python error" not in stderr
    assert stopped - _LAST_INTERRUPT[0] < 5.0
