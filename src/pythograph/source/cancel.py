"""명령 스레드·파싱 스레드의 협조적 취소와, 신호가 와도 멈춰 서지 않는 대기.

CLI 명령의 계산은 큰 스택 스레드에서 돈다(`source.syntax.run_on_parse_stack`). Ctrl+C(신호)는 주 스레드에만 닿으므로 주
스레드가 그 스레드의 취소 표시(`CancelToken`)를 세우고, 계산은 정해 둔 자리(`raise_if_cancelled`: 파일 하나를 파싱하기
전, 디렉터리 하나를 열거하기 전, 나눠 기다리는 git·입력 대기의 조각마다)에서 표시를 보고 KeyboardInterrupt를 내 `with`·
`finally`를 푼다. 다른 스레드에 비동기 예외를 넣던 방식(`PyThreadState_SetAsyncExc`)은 쓰지 않는다: 그 예외는 아무
바이트코드에서나, `threading`의 Condition·Event 안에서도 떨어져 잠금을 쥔 채 남겼고(4차 리뷰 I-B1·I-S1), C
호출(파싱·막힌 읽기·자식 대기) 중에는 닿지 않아 어차피 정해 둔 자리가 필요했다. 취소 표시가 없는 스레드(주 스레드,
라이브러리 호출자)에서 `raise_if_cancelled`는 아무것도 하지 않는다.

신호를 받을 수 있는 스레드는 `threading.Event`·`Condition`으로 기다리지 않는다. 그 대기는 파이썬 코드라 신호 처리기의
예외가 Condition의 잠금을 얻은 직후에 떨어지면 잠금이 남고, 같은 Event를 다시 기다리면 자기가 쥔 잠금에서 영원히 멈췄다.
대신 `Completion`이 C 수준 잠금 하나(`threading.Lock`)의 시간 제한 획득으로 기다린다. 획득이 신호로 끊기면 잠금을 쥐지
않은 채 예외가 나고, 획득한 직후에 예외가 나도 완료 표시(`done`)를 먼저 보므로 다시 기다리지 않는다.
"""

from __future__ import annotations

import _thread
import sys
import threading
from collections.abc import Callable
from typing import TypeVar, cast

#: 나눠 기다리는 대기의 한 조각(초)이다. 조각마다 취소 표시를 보고, Windows에서는 Ctrl+C도 이때 받는다(Windows의 잠금
#: 대기는 Ctrl+C를 받지 못한다. Windows에서는 테스트하지 못했다).
WAIT_SLICE_SECONDS = 0.1

#: 작업 결과 타입이다.
_Result = TypeVar("_Result")


class CancelToken:
    """한 작업 스레드의 취소 표시다. 세우는 쪽은 주 스레드, 보는 쪽은 그 작업 스레드다."""

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        """세우지 않은 표시를 만든다."""
        self.cancelled = False


#: 스레드마다 지금 작업의 취소 표시를 둔다(`bind_token`).
_current = threading.local()


def bind_token(token: CancelToken | None) -> None:
    """지금 스레드의 취소 표시를 정한다(작업 스레드가 작업에 들어갈 때).

    Args:
        token: 이 스레드가 볼 표시(없으면 None).
    """
    _current.token = token


def raise_if_cancelled() -> None:
    """지금 스레드의 작업을 그만두라고 했으면 KeyboardInterrupt를 낸다. 표시가 없는 스레드에서는 아무것도 하지 않는다.

    Raises:
        KeyboardInterrupt: 주 스레드가 Ctrl+C 등으로 이 작업의 취소를 요청했다.
    """
    token = getattr(_current, "token", None)
    if token is not None and token.cancelled:
        raise KeyboardInterrupt


class Completion:
    """작업 스레드가 한 번 알리고, 신호를 받을 수 있는 스레드가 기다리는 완료 표시다.

    잠금은 만들 때 잡아 두고 `set`이 푼다(`threading.Lock`은 다른 스레드가 풀 수 있다). `set`은 신호를 받지 않는 작업
    스레드만 부른다. 같은 표시를 여러 스레드가 알려도(`set` 여러 번) 한 번만 푼다. `set`이 쓰는 메서드는 미리 묶어 두어
    메모리가 모자란 `finally`에서도 새 객체를 만들지 않는다. 그래도 알리지 못하고 죽은 스레드는 기다리는 쪽이
    `is_alive`로 알아챈다.
    """

    def __init__(self) -> None:
        """아직 끝나지 않은 표시를 만든다."""
        #: 끝났는지다. 잠금을 풀기 전에 세운다.
        self.done = False
        self._lock = threading.Lock()
        self._lock.acquire()
        self._release = self._lock.release
        guard = threading.Lock()
        self._enter = guard.acquire
        self._leave = guard.release

    def set(self) -> None:
        """끝났다고 알린다(두 번째부터는 아무것도 하지 않는다)."""
        self._enter()
        try:
            if not self.done:
                self.done = True
                self._release()
        finally:
            self._leave()

    def wait(self, timeout: float) -> bool:
        """`timeout`초까지 기다린다. 신호 처리기의 예외는 그대로 나가고, 그 뒤 다시 기다려도 멈추지 않는다.

        Args:
            timeout: 최대 대기(초, 0 이상).

        Returns:
            끝났으면 True.
        """
        if self.done:
            return True
        if self._lock.acquire(timeout=max(0.0, timeout)):
            self._lock.release()
            return True
        return self.done


def daemon_threads_allowed() -> bool:
    """지금 인터프리터가 데몬 스레드를 허락하는지 본다(3.11까지는 그런 설정이 없어 늘 True).

    Returns:
        데몬 스레드를 만들 수 있으면 True.
    """
    allowed = getattr(_thread, "daemon_threads_allowed", None)
    return True if allowed is None else bool(allowed())


def forget_finished_thread() -> None:
    """끝나 가는 지금 스레드를 `threading`의 끝난 스레드 모음(`_dangling`, 약한 참조 집합)에서 뺀다(작업 스레드에서
    부른다).

    그 집합의 약한 참조 콜백은 스레드 객체를 마지막으로 놓는 스레드, 곧 부른 스레드에서 파이썬 코드로 돈다. 그 사이에
    닿은 Ctrl+C는 CPython이 약한 참조 콜백의 예외를 버리므로("Exception ignored in") 사라지고 분석이 계속됐다(4차 리뷰
    I-S4는 파싱 스레드, 5차 리뷰 F3은 입력·git 읽기 스레드). 작업 스레드에서 미리 빼면 콜백이 남지 않는다. 그 집합은
    fork한 자식에서 누가 합류할지 모르는 끝난 스레드의 잠금을 다시 만들 때만 쓰는데, 이 스레드들에는 부른 스레드만
    합류하고 fork한 자식에는 합류하는 중인 부른 스레드가 없다. 비공개 속성이라 없거나 실패하면 그대로 둔다.
    """
    dangling = getattr(threading, "_dangling", None)
    if dangling is not None:
        try:
            dangling.discard(threading.current_thread())
        except Exception:  # 콜백이 남을 뿐이다(메모리 부족 등에서도 끝을 알리는 일을 막지 않는다).
            return


def call_interruptibly(function: Callable[[], _Result], name: str) -> _Result:
    """막히는 호출(파이프·표준 입력 읽기)을 데몬 스레드에서 돌리고, 부른 스레드는 조각으로 나눠 기다린다.

    C 수준 읽기는 취소 표시를 볼 수 없으므로 따로 돌린다. 기다리는 조각마다 취소 표시를 보고(`raise_if_cancelled`), 부른
    스레드가 주 스레드면 Ctrl+C가 대기를 끊는다. 그렇게 그만두면 읽던 데몬 스레드는 버려진다(입력만 쥐고 있어 종료
    처리가 기다리지 않는다). 함수의 결과나 예외는 부른 스레드에서 그대로 돌려주거나 다시 낸다. 담긴 예외의 traceback은
    `f_back`으로 데몬 스레드 프레임과 결과 자리에 닿으므로, 꺼내며 비우고 그만둔 뒤 담긴 결과는 데몬 스레드가 비워 순환
    참조를 남기지 않는다. 데몬 스레드가 결과를 알리지 못하고 죽으면 RuntimeError다.

    스레드 시작이 실패하면 `source.syntax._ParseThread`와 같이 가린다: 시작 도중에 난 예외(Ctrl+C가 끊은 시작)면 그
    예외를 다시 내고(`start_interruption`, 부른 쪽이 시작 전부터 처리하던 예외는 해당하지 않는다), 아니면 그 자리에서
    부른다. 시작이 실패했다고 보고됐는데 스레드가 실제로 시작한 경우에도 함수는 한 번만 돈다: 먼저 차지한 쪽(`claims`의
    첫 항목, `list.append`는 한 번의 C 호출이다)만 부른다. 데몬 스레드를 허락하지 않는 인터프리터(3.12 이상의 격리 하위
    인터프리터)에서는 0.3.1처럼 그 자리에서 부른다(그때 막힌 읽기는 Ctrl+C로 끊기지 않는다, 5차 리뷰 F2).

    Args:
        function: 돌릴 함수.
        name: 데몬 스레드 이름.

    Returns:
        함수의 결과.

    Raises:
        RuntimeError: 데몬 스레드가 결과도 예외도 남기지 못하고 끝났을 때.
    """
    outcome: list[object] = [NO_OUTCOME]
    claims: list[str] = []
    abandoned: list[bool] = []
    finished = Completion()
    signal_finished = finished.set

    def target() -> None:
        """먼저 차지했으면 부르고 결과나 예외를 담는다."""
        claims.append("worker")
        try:
            if claims[0] == "worker":
                outcome[0] = function()
        except BaseException as error:  # 부른 스레드에서 같은 예외로 다시 낸다.
            outcome[0] = error
        finally:
            forget_finished_thread()
            signal_finished()
            if abandoned:
                outcome[0] = NO_OUTCOME

    if not daemon_threads_allowed():
        return function()
    handled = sys.exc_info()[1]
    reader = threading.Thread(target=target, name=name, daemon=True)
    interrupted: BaseException | None = None
    try:
        try:
            reader.start()
        except Exception as error:
            interrupted = start_interruption(error, handled)
            if interrupted is None and not isinstance(error, RuntimeError):
                raise
            claims.append("caller")
        if interrupted is not None:
            raise interrupted
    finally:
        interrupted = handled = None
    if claims and claims[0] == "caller":
        return function()
    try:
        while not finished.wait(WAIT_SLICE_SECONDS):
            raise_if_cancelled()
            if not reader.is_alive() and not finished.done:
                raise RuntimeError(f"the {name} thread ended without recording a result or an exception")
    except BaseException:
        # 아직 시작하지 않은 스레드가 함수를 부르지 않게 차지한다(5차 리뷰 N3). 그만둔다고 먼저 표시하고 끝났는지
        # 본다(데몬 스레드는 끝을 알린 뒤 이 표시를 본다): 늦게 보는 쪽이 비운다.
        claims.append("caller")
        abandoned.append(True)
        if finished.done:
            outcome[0] = NO_OUTCOME
        raise
    return cast(_Result, take_outcome(outcome))


#: 결과 자리가 비었다는 표시다.
NO_OUTCOME = object()


def take_outcome(outcome: list[object]) -> object:
    """작업 스레드가 담은 결과를 꺼내 자리를 비우고 돌려주거나, 예외면 다시 낸다(예외를 가리키는 변수는 비운다).

    담긴 예외의 traceback 프레임은 `f_back`으로 작업 스레드 프레임과 이 자리에 닿으므로, 꺼내며 비우지 않으면 순환
    참조가 되어 작업이 쥐던 자료 전체가 순환 GC 전까지 풀리지 않는다(4차 리뷰 CLI-1).

    Args:
        outcome: 결과 자리 하나짜리 목록.

    Returns:
        결과.

    Raises:
        AssertionError: 자리가 비었을 때(작업 스레드가 결과도 예외도 남기지 못하고 끝났다).
    """
    result = outcome[0]
    outcome[0] = NO_OUTCOME
    if result is NO_OUTCOME:
        raise AssertionError("the worker thread ended without recording a result or an exception")
    if isinstance(result, BaseException):
        try:
            raise result
        finally:
            result = None
    return result


def start_interruption(failure: BaseException, handled: BaseException | None) -> BaseException | None:
    """스레드 시작이 낸 예외가 시작 도중에 난 다른 예외를 처리하다 난 것이면 그 예외를 돌려준다.

    시작을 기다리던 중 Ctrl+C나 신호 처리기의 예외가 끊으면 표준 라이브러리 안에서 "release unlocked lock" 같은
    RuntimeError가 되고(`Exception`인 신호 예외는 `Thread.start`의 정리와 새 스레드가 겨뤄 KeyError가 되기도 한다), 원래
    예외는 앞 예외(`__context__`)로 남는다. 부른 쪽이 시작 전부터 처리하던 예외(`handled`, `except` 블록 안에서 부른
    경우)는 시작 도중에 난 것이 아니다(4차 리뷰 B1).

    Args:
        failure: 시작이 낸 예외.
        handled: 시작 전에 부른 쪽이 처리하던 예외(`sys.exc_info()[1]`).

    Returns:
        끊은 예외, 끊긴 시작이 아니면 None.
    """
    interrupted = failure.__context__
    return None if interrupted is None or interrupted is handled else interrupted
