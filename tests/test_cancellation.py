"""협조적 취소와 신호에 안전한 대기: 전용 스레드의 시작·기다림·멈춤, git 대기, `--roots-from` 읽기."""

from __future__ import annotations

import contextlib
import gc
import io
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from pythograph.cli import main as cli_main
from pythograph.graph import revision
from pythograph.graph.roots import collect_roots
from pythograph.source import syntax
from pythograph.source.cancel import CancelToken, Completion, bind_token, raise_if_cancelled
from tests.conftest import REPOSITORY

#: POSIX 전용 도구(실행 권한 셸 스크립트, FIFO, SIGALRM 타이머)를 쓰는 테스트의 건너뛰기 표시다.
POSIX_ONLY = pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "setitimer"), reason="POSIX 전용 도구를 쓴다")

#: 취소를 요청한 뒤 돌아와야 하는 시간(초)이다. 조각은 0.1초라 부하가 있어도 넉넉하다.
_PROMPT_SECONDS = 5.0


def _cancelled_call(function: Callable[[], object], delay: float = 0.3) -> tuple[BaseException | None, float]:
    """`function`을 취소 표시를 둔 도우미 스레드에서 부르고 `delay`초 뒤 표시를 세운다.

    막힌 호출이 취소를 보지 못해도 테스트가 멈추지 않도록 `_PROMPT_SECONDS`까지만 기다린다(그때도 끝나지 않으면
    실패다).

    Args:
        function: 부를 함수.
        delay: 표시를 세우기까지의 시간(초).

    Returns:
        (함수가 낸 예외 또는 None, 표시를 세운 뒤 끝날 때까지 걸린 시간).
    """
    token = CancelToken()
    outcome: list[BaseException | None] = []
    finished: list[float] = []

    def target() -> None:
        """표시를 두고 부른다."""
        bind_token(token)
        try:
            function()
        except BaseException as error:  # 부른 쪽에서 본다.
            outcome.append(error)
        else:
            outcome.append(None)
        finally:
            finished.append(time.monotonic())

    helper = threading.Thread(target=target, daemon=True)
    helper.start()
    time.sleep(delay)
    cancelled = time.monotonic()
    token.cancelled = True
    helper.join(_PROMPT_SECONDS)
    assert not helper.is_alive(), "the call did not stop after cancellation"
    return outcome[0], finished[0] - cancelled


def test_checkpoints_raise_only_for_a_cancelled_token() -> None:
    """확인 자리는 표시가 없거나 세우지 않았으면 아무것도 하지 않고, 세웠으면 KeyboardInterrupt를 낸다."""
    raise_if_cancelled()
    token = CancelToken()
    bind_token(token)
    try:
        raise_if_cancelled()
        token.cancelled = True
        with pytest.raises(KeyboardInterrupt):
            raise_if_cancelled()
    finally:
        bind_token(None)
    raise_if_cancelled()


def test_completion_is_set_once_and_never_rewaits() -> None:
    """완료 표시는 여러 번 알려도 한 번만 풀리고, 끝난 뒤에는 기다리지 않고 True다."""
    finished = Completion()
    assert not finished.wait(0.0)
    finished.set()
    finished.set()
    assert finished.done
    assert finished.wait(0.0)
    assert finished.wait(10.0)


#: 부른 스레드의 기다림·멈춤 도중 아무 곳에서나 신호 처리기의 예외를 받는 프로세스 안 스트레스다(인자: 반복 수, 스위치
#: 간격, 시드). 짧은 작업(0.2–2 ms)이 끝나는 순간 근처에 SIGALRM을 맞추고, 호출 하나가 5초를 넘거나 종료가 멈추면
#: 실패한다. 기다림이 `threading.Event`이던 때(3d333ff·3b7343e)는 Condition의 잠금이 부른 스레드에 남아 멈춤을 다시
#: 기다리다 수십–수천 번째에서 멈췄다(3.10–3.14). 처리기는 호출을 감싼 `try` 안(`armed`)에서만 예외를 낸다: 끄기
#: 직전에 만료된 타이머의 신호를 다른 스레드가 받으면(macOS는 프로세스로 온 신호를 아무 스레드에나 줄 수 있다) C
#: 처리기가 그 스레드에서 늦게 돌아, 주 스레드가 `try`를 벗어난 뒤 하네스 코드에서 처리기가 돌 수 있다(CI macOS
#: 3.10·3.13에서 실제로 났다).
_CALLER_WAIT_STRESS = r"""
import collections, faulthandler, os, random, signal, sys, threading, time
faulthandler.dump_traceback_later(90, exit=True)
from pythograph.source import syntax

iterations, switch, seed = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
sys.setswitchinterval(switch)
random.seed(seed)
syntax.largest_parse_stack_bytes = lambda: syntax.MIN_PARSE_STACK_BYTES
crashed = collections.Counter()
threading.excepthook = lambda args: crashed.update([args.exc_type.__name__])


class Boom(KeyboardInterrupt):
    pass


def handler(_signum, _frame):
    if state["armed"]:
        raise Boom


signal.signal(signal.SIGALRM, handler)
state = {"started": None, "i": 0, "armed": False}


def watchdog():
    while True:
        time.sleep(0.5)
        started = state["started"]
        if started is not None and time.perf_counter() - started > 5:
            print(f"STUCK at {state['i']}", flush=True)
            os._exit(3)


threading.Thread(target=watchdog, daemon=True).start()
booms = 0
other = collections.Counter()
for index in range(iterations):
    state["i"] = index
    duration = random.uniform(0.0002, 0.002)

    def work(duration=duration):
        end = time.perf_counter() + duration
        while time.perf_counter() < end:
            pass
        return 1

    if index % 2:
        call = syntax.run_on_parse_stack
    else:
        call = lambda w: syntax._run_on_large_stack(w, syntax.MIN_PARSE_STACK_BYTES)
    state["started"] = time.perf_counter()
    try:
        try:
            state["armed"] = True
            signal.setitimer(signal.ITIMER_REAL, duration * random.uniform(0.7, 1.6))
            call(work)
        finally:
            state["armed"] = False
            signal.setitimer(signal.ITIMER_REAL, 0)
    except Boom:
        booms += 1
    except BaseException as error:
        other[type(error).__name__] += 1
    state["started"] = None
time.sleep(0.3)
print("ok", booms > 0, dict(other), dict(crashed))
"""


@POSIX_ONLY
def test_caller_waits_survive_signal_exceptions_at_any_point() -> None:
    """부른 스레드가 기다리거나 멈추게 하는 도중 신호 처리기의 예외를 어디서 받아도 멈춰 서지 않고, 그 예외만 나가며
    (RuntimeError로 바뀌지 않는다) 작업 스레드는 예외로 죽지 않는다. 6,000번 중 멈춘 호출·종료가 하나도 없어야 한다."""
    result = subprocess.run(
        [sys.executable, "-c", _CALLER_WAIT_STRESS, "6000", "0.0001", "3"],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=150,
    )
    assert result.returncode == 0, (result.returncode, result.stdout[-500:], result.stderr[-2000:])
    assert result.stdout.splitlines()[-1] == "ok True {} {}"


class _LateThread:
    """`Thread.start`가 실패했다고 보고하지만 대상은 따로 된 실제 스레드에서 나중에 돈다(시작 경합을 흉내 낸다)."""

    #: 마지막으로 만든 대역이다.
    latest: _LateThread | None = None

    def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
        """대상만 받는다."""
        del name, daemon
        self._target = target
        self.real: threading.Thread | None = None
        type(self).latest = self

    def start(self) -> None:
        """대상을 늦게 시작하고 실패를 보고한다(하위 클래스가 시점과 실패를 정한다)."""
        self.real = threading.Thread(target=self._later, daemon=True)
        self.real.start()
        self.fail()

    def _later(self) -> None:
        """하위 클래스가 정한 때에 대상을 돌린다."""
        self.hold()
        self._target()

    def hold(self) -> None:
        """대상을 돌리기 전에 기다린다."""

    def fail(self) -> None:
        """시작 실패를 보고한다."""
        raise RuntimeError("can't start new thread")

    def is_alive(self) -> bool:
        """실제 스레드가 살아 있는지다."""
        return self.real is not None and self.real.is_alive()

    def join(self, timeout: float | None = None) -> None:
        """실제 스레드에 합류한다."""
        assert self.real is not None
        self.real.join(timeout)


def _install(monkeypatch: pytest.MonkeyPatch, thread_class: type) -> None:
    """`syntax`가 만드는 스레드를 대역으로 바꾼다."""
    monkeypatch.setattr(syntax, "threading", SimpleNamespace(Thread=thread_class, stack_size=threading.stack_size))


def test_a_thread_that_starts_after_the_caller_gave_up_never_runs_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """시작이 실패했다고 보고된 뒤 늦게 시작한 스레드는 작업을 돌리지 않는다: 부른 쪽이 먼저 작업을 차지했다. 그래서
    CLI 명령은 부른 스레드에서 한 번만 돈다. 차지 확인(`_claims`의 첫 항목)을 빼면 늦은 스레드가 한 번 더 돌린다."""
    gate = threading.Event()

    class RefusedButLate(_LateThread):
        def hold(self) -> None:
            gate.wait(5)

    _install(monkeypatch, RefusedButLate)
    calls: list[str] = []

    def work() -> str:
        calls.append(threading.current_thread().name)
        return "done"

    with pytest.raises(syntax.ParseStackUnavailableError):
        syntax._run_on_large_stack(work, syntax.MIN_PARSE_STACK_BYTES)
    assert syntax.run_on_parse_stack(work) == "done"
    late = [RefusedButLate.latest]
    gate.set()
    for thread in late:
        assert thread is not None
        thread.join(5)
    assert calls == [threading.current_thread().name]


class _InterruptedLateStart(_LateThread):
    """시작 대기가 Ctrl+C로 끊겨 "release unlocked lock"이 된 것처럼 실패하고, 대상은 0.2초 뒤에 실제로 시작한다."""

    def hold(self) -> None:
        """대상을 늦게 시작한다."""
        time.sleep(0.2)

    def fail(self) -> None:
        """앞 예외로 KeyboardInterrupt를 남긴 RuntimeError를 낸다."""
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            raise RuntimeError("release unlocked lock")  # noqa: B904 — 앞 예외(KeyboardInterrupt)를 남긴다


def test_interrupted_starts_raise_the_interruption_and_never_run_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """시작 대기가 Ctrl+C로 끊긴 시작은 스레드가 늦게 시작하든 끝내 시작하지 않든 그 Ctrl+C(KeyboardInterrupt)를
    다시 내고, 작업은 어디서도 돌지 않는다: 부른 쪽이 먼저 차지해 늦게 시작한 스레드는 작업하지 않는다. 3b7343e는 1초
    안에 스레드가 들어오지 않으면 스레드 이유로 보고 작업을 부른 스레드에서 돌려 Ctrl+C를 잃었다. 끊긴 시작을 스레드
    이유로 가리면(`start_interruption`을 빼면) 이 테스트가 실패한다."""

    class InterruptedNeverStarts(_InterruptedLateStart):
        def start(self) -> None:
            """시작하지 않고 실패만 보고한다."""
            self.fail()

    calls: list[str] = []

    def work() -> str:
        """실행한 스레드를 기록한다."""
        calls.append(threading.current_thread().name)
        return "done"

    for thread_class in (_InterruptedLateStart, InterruptedNeverStarts):
        _install(monkeypatch, thread_class)
        for call in (
            lambda: syntax._run_on_large_stack(work, syntax.MIN_PARSE_STACK_BYTES),
            lambda: syntax.run_on_parse_stack(work),
        ):
            monkeypatch.setattr(syntax, "largest_parse_stack_bytes", lambda: syntax.MIN_PARSE_STACK_BYTES)
            with pytest.raises(KeyboardInterrupt):
                call()
            late = _LateThread.latest
            if late is not None and late.real is not None:
                late.real.join(5)
    assert calls == []


@pytest.fixture
def slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[subprocess.Popen[bytes]]]]:
    """30초 동안 아무것도 쓰지 않는 가짜 git과, 실행한 자식 목록을 준다. 끝나면 남은 자식을 죽인다."""
    program = tmp_path / "git"
    program.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    children: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    class RecordingPopen(original):  # type: ignore[valid-type, misc]
        def __init__(self, *arguments: object, **options: object) -> None:
            super().__init__(*arguments, **options)
            children.append(self)

    monkeypatch.setattr(revision.subprocess, "Popen", RecordingPopen)
    try:
        yield str(program), children
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


#: git 실행기의 두 경로다: revision 조회(상한 없음), change 캡처(출력 상한), 표준 입력을 쓰는 change 캡처.
_GIT_PATHS = {
    "revision": {},
    "bounded": {"output_limit": 4096},
    "bounded-input": {"output_limit": 4096, "input_data": b"0" * 40 + b"\n"},
}


@POSIX_ONLY
@pytest.mark.parametrize("options", list(_GIT_PATHS.values()), ids=list(_GIT_PATHS))
def test_git_waits_stop_on_cancel_and_reap_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_git: tuple[str, list[subprocess.Popen[bytes]]],
    options: dict[str, object],
) -> None:
    """git 대기는 조각마다 취소 표시를 보고, 그만두면 자식을 죽이고 거둔 뒤 KeyboardInterrupt를 낸다(두 경로 모두).
    revision 조회의 `subprocess.run`은 나누지 않아 Ctrl+C 뒤 20초를 기다리고 git을 남겼다. 대기를 한 번에 하거나 kill을
    빼면 늦거나 자식이 남는다."""
    program, children = slow_git
    monkeypatch.setattr(revision, "GIT_TIMEOUT_SECONDS", 15)
    error, elapsed = _cancelled_call(lambda: revision._run_git(program, tmp_path, "status", **options))  # type: ignore[arg-type]
    assert isinstance(error, KeyboardInterrupt)
    assert elapsed < 1.0
    assert len(children) == 1
    assert children[0].poll() is not None


@POSIX_ONLY
@pytest.mark.parametrize("options", list(_GIT_PATHS.values()), ids=list(_GIT_PATHS))
def test_git_timeouts_return_nothing_and_reap_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_git: tuple[str, list[subprocess.Popen[bytes]]],
    options: dict[str, object],
) -> None:
    """시간 제한(`GIT_TIMEOUT_SECONDS`)이 지나면 전처럼 None이고 자식은 죽여 거둔다."""
    program, children = slow_git
    monkeypatch.setattr(revision, "GIT_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    assert revision._run_git(program, tmp_path, "status", **options) is None  # type: ignore[arg-type]
    assert time.monotonic() - started < _PROMPT_SECONDS
    assert len(children) == 1
    assert children[0].poll() is not None


@POSIX_ONLY
def test_roots_reading_from_an_idle_fifo_stops_on_cancel(tmp_path: Path) -> None:
    """쓰는 쪽이 열어 둔 채 쓰지 않는 FIFO를 `--roots-from`으로 읽는 동안 취소하면 바로 KeyboardInterrupt다. 전에는
    경로 입력을 명령 스레드에서 읽어(C 읽기는 취소를 볼 수 없다) Ctrl+C 뒤 20초를 기다렸다."""
    fifo = tmp_path / "roots.fifo"
    os.mkfifo(fifo)
    keeper = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(fifo, os.O_WRONLY)
    os.close(keeper)
    try:
        error, elapsed = _cancelled_call(lambda: collect_roots([], str(fifo), None))
    finally:
        os.close(writer)
    assert isinstance(error, KeyboardInterrupt)
    assert elapsed < 1.0


@POSIX_ONLY
def test_roots_reading_from_idle_stdin_stops_on_cancel() -> None:
    """`--roots-from -`도 같다(쓰는 쪽이 열어 둔 파이프)."""
    read_end, write_end = os.pipe()
    stdin = io.TextIOWrapper(io.FileIO(read_end, "r"), encoding="utf-8")
    try:
        error, elapsed = _cancelled_call(lambda: collect_roots([], "-", stdin))
    finally:
        os.close(write_end)
    assert isinstance(error, KeyboardInterrupt)
    assert elapsed < 1.0


def test_roots_reading_keeps_its_results_and_errors(tmp_path: Path) -> None:
    """데몬 스레드에서 읽어도 결과와 오류 대응(사용법 오류 64가 되는 `RootsError`)은 전과 같다."""
    roots = tmp_path / "roots.json"
    roots.write_text('["a", "b", "a"]', encoding="utf-8")
    assert collect_roots(["c"], str(roots), None) == ("c", "a", "b")
    assert collect_roots([], "-", io.StringIO('["x"]')) == ("x",)
    (tmp_path / "bad.json").write_bytes(b"\xff\xfe")
    for argument, stdin in ((str(tmp_path / "bad.json"), None), (str(tmp_path / "missing.json"), None), ("-", None)):
        with pytest.raises(Exception) as caught:
            collect_roots([], argument, stdin)
        assert type(caught.value).__name__ == "RootsError"


#: 실제 분석(파일 2,000개짜리 프로젝트의 graph) 도중 SIGALRM 처리기가 KeyboardInterrupt를 내는 프로세스다(인자:
#: 프로젝트). 처리기가 낸 뒤 `main`이 끝날 때까지의 시간을 쓴다.
_REAL_ANALYSIS_INTERRUPT = r"""
import io, signal, sys, time
from pythograph.cli import main as cli_main
from pythograph.source import syntax

syntax.largest_parse_stack_bytes = lambda: syntax.MIN_PARSE_STACK_BYTES
fired = []


def handler(_signum, _frame):
    fired.append(time.monotonic())
    raise KeyboardInterrupt


signal.signal(signal.SIGALRM, handler)
signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    cli_main.main(["graph", "--project", sys.argv[1]], io.StringIO(), io.StringIO())
    print("finished")
except KeyboardInterrupt:
    print("interrupted", f"{time.monotonic() - fired[0]:.2f}")
"""


@POSIX_ONLY
def test_ctrl_c_during_real_analysis_stops_at_a_checkpoint(tmp_path: Path) -> None:
    """실제 graph 분석 도중 Ctrl+C를 받으면 계산 스레드가 다음 확인 자리(파일 파싱·디렉터리 열거 등)에서 풀려 곧 끝난다
    (멈추기 유예 20초를 다 쓰지 않는다)."""
    for index in range(2000):
        package = tmp_path / f"pkg{index % 40}"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / f"m{index}.py").write_text(
            f"import os\n\n\ndef f{index}(x):\n    return os.path.join(str(x), 'v{index}')  # {'#' * 160}\n",
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, "-c", _REAL_ANALYSIS_INTERRUPT, str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    outcome = result.stdout.split()
    assert outcome[0] == "interrupted", result.stdout
    assert float(outcome[1]) < 2.0


class _Marker:
    """약한 참조로 살아 있는지 볼 객체다(작업이 만든 분석 자료를 대신한다)."""


def _without_gc(check: Callable[[list[weakref.ref[_Marker]]], None]) -> None:
    """순환 GC를 끈 채 `check`를 부르고, 그 안에서 만든 표식이 참조 계수만으로 사라졌는지 본다.

    Args:
        check: 표식의 약한 참조를 목록에 담고 작업·전달을 끝내는 함수(예외는 그 안에서 잡아 버린다).
    """
    references: list[weakref.ref[_Marker]] = []
    enabled = gc.isenabled()
    gc.disable()
    try:
        check(references)
        assert references
        assert [reference() for reference in references] == [None] * len(references)
    finally:
        if enabled:
            gc.enable()


def _holding_work(references: list[weakref.ref[_Marker]], error: Callable[[], BaseException]) -> Callable[[], object]:
    """표식을 만들어 지역 변수로 쥔 채 `error()`가 만든 예외를 내는 작업을 만든다(예외는 작업 안에서 만든다: 닫힌
    변수에 둔 예외는 자기 traceback의 프레임과 순환을 이룬다)."""

    def work() -> object:
        """표식을 쥐고 예외를 낸다."""
        marker = _Marker()
        references.append(weakref.ref(marker))
        raise error()

    return work


def test_relayed_errors_leave_no_reference_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """작업 스레드에서 부른 쪽으로 넘긴 예외를 버리면 작업이 쥐던 자료가 순환 GC 없이 바로 풀린다(라이브러리 전용
    스레드, CLI 명령 스레드, 늦게 난 입력 오류의 종료 코드 2, `--roots-from` 읽기 스레드). 3d333ff는 담긴 예외 →
    traceback → `_run` 프레임 → `self` → 결과 목록의 순환이 분석 힙 전체를 붙잡아, 큰 프로젝트에서 Ctrl+C로 끝나는 데
    2–3배가 걸렸다."""
    from pythograph.cli.common import InputError
    from pythograph.source.cancel import call_interruptibly

    monkeypatch.setattr(syntax, "largest_parse_stack_bytes", lambda: syntax.MIN_PARSE_STACK_BYTES)

    def library(references: list[weakref.ref[_Marker]]) -> None:
        """라이브러리 전용 스레드의 예외."""
        with contextlib.suppress(ValueError):
            syntax._run_on_large_stack(_holding_work(references, lambda: ValueError("x")), syntax.MIN_PARSE_STACK_BYTES)

    def command(references: list[weakref.ref[_Marker]]) -> None:
        """CLI 명령 스레드의 늦은 입력 오류."""
        with contextlib.suppress(InputError):
            syntax.run_on_parse_stack(_holding_work(references, lambda: InputError("document too large")))

    def cli(references: list[weakref.ref[_Marker]]) -> None:
        """`main`이 늦은 입력 오류(너무 큰 문서)를 2로 바꾼다."""
        monkeypatch.setattr(
            cli_main, "run", lambda _a, _s=None: _holding_work(references, lambda: InputError("too large"))()
        )
        assert cli_main.main(["graph"], io.StringIO(), io.StringIO()) == 2

    def reader(references: list[weakref.ref[_Marker]]) -> None:
        """`--roots-from` 읽기 스레드의 예외."""
        with contextlib.suppress(OSError):
            call_interruptibly(_holding_work(references, lambda: OSError("unreadable")), "pythograph-test")

    for check in (library, command, cli, reader):
        _without_gc(check)


class _Boom(KeyboardInterrupt):
    """SIGALRM 처리기가 내는 예외다."""


def _raise_boom(_signum: int, _frame: object) -> None:
    """SIGALRM 처리기: `_Boom`을 낸다."""
    raise _Boom


def _interrupt_after(delay: float, call: Callable[[], object]) -> None:
    """`delay`초 뒤 SIGALRM 처리기가 `_Boom`을 내게 하고 `call`을 부른다(`_Boom`은 삼킨다)."""
    previous = signal.signal(signal.SIGALRM, _raise_boom)
    try:
        with contextlib.suppress(_Boom):
            signal.setitimer(signal.ITIMER_REAL, delay)
            call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _cancellable(references: list[weakref.ref[_Marker]]) -> Callable[[], object]:
    """표식을 쥐고 취소될 때까지 확인 자리를 지나는 작업을 만든다."""

    def work() -> object:
        """표식을 쥐고 돈다."""
        marker = _Marker()
        references.append(weakref.ref(marker))
        while True:
            time.sleep(0.01)
            raise_if_cancelled()

    return work


@POSIX_ONLY
def test_stopped_work_leaves_no_reference_cycle() -> None:
    """Ctrl+C로 멈춘 작업이 쥐던 자료는 순환 GC 없이 풀린다(멈춘 스레드가 담은 KeyboardInterrupt를 버린다)."""

    def stopped(references: list[weakref.ref[_Marker]]) -> None:
        """기다리던 부른 스레드가 신호 처리기의 예외를 받는다."""
        _interrupt_after(
            0.2, lambda: syntax._run_on_large_stack(_cancellable(references), syntax.MIN_PARSE_STACK_BYTES)
        )

    _without_gc(stopped)


def test_interrupted_starts_leave_no_reference_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """작업 스레드가 이미 차지한 끊긴 시작도 그 스레드를 멈추게 하고 작업이 쥐던 자료를 남기지 않는다."""

    class InterruptedAfterClaim(threading.Thread):
        """작업을 차지한 뒤 시작 대기가 끊긴 것처럼 실패를 보고한다."""

        def start(self) -> None:
            """실제로 시작하고 0.05초 뒤 끊긴 시작을 보고한다."""
            super().start()
            time.sleep(0.05)
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise RuntimeError("release unlocked lock")  # noqa: B904 — 앞 예외(KeyboardInterrupt)를 남긴다

    def interrupted(references: list[weakref.ref[_Marker]]) -> None:
        """끊긴 시작이 작업 스레드를 멈추게 한다."""
        with contextlib.suppress(KeyboardInterrupt):
            syntax._run_on_large_stack(_cancellable(references), syntax.MIN_PARSE_STACK_BYTES)

    monkeypatch.setattr(
        syntax, "threading", SimpleNamespace(Thread=InterruptedAfterClaim, stack_size=threading.stack_size)
    )
    _without_gc(interrupted)


@POSIX_ONLY
def test_abandoned_workers_leave_no_reference_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """유예가 지나 버려진 작업 스레드가 나중에 예외를 담아도 순환이 남지 않는다: 담긴 예외의 traceback 프레임은
    `f_back`으로 작업 스레드의 `_run` 프레임과 그 결과 자리에 닿으므로, 부른 쪽이 그만둔 뒤 담긴 결과는 작업 스레드가
    비운다(그만둔다는 표시와 끝 알림은 늦게 보는 쪽이 비운다)."""

    def late_failure_work(references: list[weakref.ref[_Marker]]) -> Callable[[], object]:
        """표식을 쥐고 취소를 0.5초 무시한 뒤 예외를 내는 작업을 만든다."""

        def late_failure() -> object:
            """표식을 쥐고 0.5초 뒤 예외를 낸다."""
            marker = _Marker()
            references.append(weakref.ref(marker))
            time.sleep(0.5)
            raise ValueError("late")

        return late_failure

    def abandoned(references: list[weakref.ref[_Marker]]) -> None:
        """0.1초 유예로 작업 스레드를 버리고 그 스레드가 끝날 때까지 기다린다."""
        work = late_failure_work(references)
        _interrupt_after(0.1, lambda: syntax._run_on_large_stack(work, syntax.MIN_PARSE_STACK_BYTES))
        deadline = time.monotonic() + 5
        while any(thread.name == "pythograph-parse" for thread in threading.enumerate()):
            assert time.monotonic() < deadline
            time.sleep(0.02)

    monkeypatch.setattr(syntax, "COMMAND_STOP_GRACE_SECONDS", 0.1)
    _without_gc(abandoned)


def _bounded(function: Callable[[], object], limit: float = 10.0) -> BaseException | None:
    """`function`을 도우미 스레드에서 부르고 `limit`초까지만 기다린다(멈추면 실패, 예외는 돌려준다)."""
    outcome: list[BaseException | None] = []

    def target() -> None:
        """부르고 예외를 담는다."""
        try:
            function()
        except BaseException as error:  # 부른 쪽에서 본다.
            outcome.append(error)
        else:
            outcome.append(None)

    helper = threading.Thread(target=target, daemon=True)
    helper.start()
    helper.join(limit)
    assert not helper.is_alive(), "the call hung"
    return outcome[0]


def test_a_worker_that_dies_without_signalling_is_an_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """작업 스레드가 끝을 알리지 못하고 죽으면(메모리가 모자란 `finally`) 부른 쪽은 영원히 기다리지 않고 내부 오류로
    끝난다(CLI는 2). 결과를 담은 뒤 알리기만 실패했으면 그 결과를 돌려준다. 3d333ff는 완료 표시만 기다려 `ulimit -v`
    119–120 MB의 graph가 6번 중 1–2번 끝나지 않았다."""

    def dies_silently(self: object) -> None:
        """작업도 알림도 없이 끝난다."""

    monkeypatch.setattr(syntax._ParseThread, "_run", dies_silently)
    error = _bounded(lambda: syntax._run_on_large_stack(lambda: 1, syntax.MIN_PARSE_STACK_BYTES))
    assert isinstance(error, AssertionError)
    monkeypatch.setattr(syntax, "largest_parse_stack_bytes", lambda: syntax.MIN_PARSE_STACK_BYTES)
    codes: list[int] = []
    assert _bounded(lambda: codes.append(cli_main.main(["graph"], io.StringIO(), io.StringIO()))) is None
    assert codes == [2]
    monkeypatch.undo()

    def failing_set(self: Completion) -> None:
        """끝을 알리다 메모리가 모자란 것처럼 한다."""
        raise MemoryError

    monkeypatch.setattr(Completion, "set", failing_set)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    results: list[object] = []
    assert _bounded(lambda: results.append(syntax._run_on_large_stack(lambda: 7, syntax.MIN_PARSE_STACK_BYTES))) is None
    assert results == [7]


def test_interruptible_reads_classify_failed_starts_like_parser_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--roots-from` 읽기 스레드의 시작 실패도 파싱 스레드와 같이 가린다: Ctrl+C가 끊은 시작은 그 Ctrl+C를 다시 내고
    읽지 않으며, 스레드를 만들 수 없는 실패는 그 자리에서 한 번 읽는다(부른 쪽이 `except` 블록 안이어도). 실패가
    보고됐는데 스레드가 이미 시작했으면 그 스레드가 한 번만 읽는다. 전에는 RuntimeError면 무엇이든 그 자리에서 읽어,
    표준 입력을 두 번 읽고 Ctrl+C를 잃을 수 있었다(4차 리뷰 CLI-4)."""
    from pythograph.source import cancel

    reads: list[str] = []

    def read() -> str:
        """읽은 스레드를 기록한다."""
        reads.append(threading.current_thread().name)
        return "[]"

    class Interrupted:
        """시작 대기가 Ctrl+C로 끊긴 것처럼 실패하고 시작하지 않는다."""

        def __init__(self, **_options: object) -> None:
            """인자를 받기만 한다."""

        def start(self) -> None:
            """앞 예외로 KeyboardInterrupt를 남긴 RuntimeError를 낸다."""
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise RuntimeError("release unlocked lock")  # noqa: B904 — 앞 예외(KeyboardInterrupt)를 남긴다

    class Refused(Interrupted):
        """스레드를 만들 수 없다."""

        def start(self) -> None:
            """언제나 실패한다."""
            raise RuntimeError("can't start new thread")

    class StartedThenRefused(threading.Thread):
        """실제로 시작한 뒤 실패를 보고한다."""

        def start(self) -> None:
            """시작하고 대상이 작업을 차지할 때까지 기다린 뒤 실패를 보고한다."""
            super().start()
            time.sleep(0.1)
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(cancel, "threading", SimpleNamespace(Thread=Interrupted, Lock=threading.Lock))
    with pytest.raises(KeyboardInterrupt):
        cancel.call_interruptibly(read, "pythograph-test")
    assert reads == []
    monkeypatch.setattr(cancel, "threading", SimpleNamespace(Thread=Refused, Lock=threading.Lock))
    assert cancel.call_interruptibly(read, "pythograph-test") == "[]"
    try:
        raise KeyError("unrelated")
    except KeyError:
        assert cancel.call_interruptibly(read, "pythograph-test") == "[]"
    assert reads == [threading.current_thread().name] * 2
    monkeypatch.setattr(cancel, "threading", SimpleNamespace(Thread=StartedThenRefused, Lock=threading.Lock))
    assert cancel.call_interruptibly(read, "pythograph-test") == "[]"
    assert reads == [threading.current_thread().name] * 2 + ["pythograph-test"]


def test_signal_exceptions_inside_thread_start_are_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Exception`인 신호 처리기의 예외(예: SIGALRM의 TimeoutError)가 `Thread.start` 안에서 표준 라이브러리 정리와 겨뤄
    KeyError로 바뀌어도, 부른 쪽은 원래 예외를 받고 작업은 돌지 않는다(4,000번 스트레스 중 수십 번 KeyError가 새던 것을
    4차 리뷰 반영 중 찾았다)."""
    calls: list[str] = []

    class RewrittenStart:
        """시작 도중 TimeoutError를 처리하다 KeyError로 끝난 것처럼 실패하고 시작하지 않는다."""

        def __init__(self, **_options: object) -> None:
            """인자를 받기만 한다."""

        def start(self) -> None:
            """앞 예외로 TimeoutError를 남긴 KeyError를 낸다."""
            try:
                raise TimeoutError("budget")
            except TimeoutError:
                raise KeyError("thread")  # noqa: B904 — 앞 예외(TimeoutError)를 남긴다

    _install(monkeypatch, RewrittenStart)
    with pytest.raises(TimeoutError):
        syntax._run_on_large_stack(lambda: calls.append("ran"), syntax.MIN_PARSE_STACK_BYTES)
    from pythograph.source import cancel

    monkeypatch.setattr(cancel, "threading", SimpleNamespace(Thread=RewrittenStart, Lock=threading.Lock))
    with pytest.raises(TimeoutError):
        cancel.call_interruptibly(lambda: calls.append("read"), "pythograph-test")
    assert calls == []
