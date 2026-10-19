"""명령줄로 받은 입력 파일(`--wrappers`, `--roots-from`)을 일반 파일이나 파이프일 때만 연다.

분석 대상 소스(`Project`)와 달리 사용자가 경로를 직접 준 파일이라 마지막 링크는 따라간다(모노레포가 공유 선언을 링크로
둘 수 있다). 대신 일반 파일과 파이프(FIFO, 셸 프로세스 치환 `<(…)`이 넘기는 `/dev/fd/N`)만 받는다. 장치(`/dev/zero`)는
끝이 없어 읽기 상한까지 메모리를 쓰고 디렉터리는 읽을 내용이 없다. O_NONBLOCK으로 열어 쓰는 쪽이 없는 FIFO도 열기가
바로 돌아오게 하고, FIFO는 연 뒤 블로킹 읽기로 되돌려 쓰는 쪽이 끝낼 때까지 읽는다(쓰는 쪽이 없으면 곧바로 끝이라
멈추지 않는다). 경로를 먼저 stat하지 않고 연 핸들의 fstat으로 판정하므로 판정과 읽기 사이에 대상이 바뀔 틈이 없다.
읽는 양은 호출한 쪽이 `read(상한 + 1)`로 제한한다. 여러 계층(graph·routes)이 함께 쓰므로 다른 패키지를 import하지 않는
맨 아래 계층에 둔다.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

#: 열기를 블로킹하지 않는 플래그다(없는 플랫폼은 0). FIFO 열기가 쓰는 쪽을 기다리지 않게 한다. 일반 파일 읽기에는 영향이
#: 없다.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

#: 입력 파일을 여는 플래그다. Windows에서 줄바꿈 변환을 끄도록 O_BINARY를 더한다.
_READ_FLAGS = os.O_RDONLY | _NONBLOCK | getattr(os, "O_BINARY", 0)


@contextmanager
def open_input_file(path: Path) -> Iterator[BinaryIO]:
    """경로가 일반 파일이나 파이프(FIFO)일 때만 읽기용 이진 핸들을 연다. 블록을 벗어나면 닫는다.

    파이프는 O_NONBLOCK으로 열었으므로 블로킹 읽기로 되돌린다. 그대로 두면 쓰는 쪽이 아직 쓰지 않은 순간의 읽기가
    빈 결과(EAGAIN)로 끝나 내용이 잘린다. 파이프를 블로킹하지 않고 열 수 없는 플랫폼(O_NONBLOCK 없음)에서는 FIFO도
    거부한다.

    Args:
        path: 사용자가 준 파일 경로(마지막 링크는 따라간다).

    Yields:
        버퍼가 있는 이진 읽기 핸들.

    Raises:
        OSError: 열지 못하거나 일반 파일·파이프가 아닐 때(디렉터리·장치).
    """
    with os.fdopen(os.open(path, _READ_FLAGS), "rb") as handle:
        mode = os.fstat(handle.fileno()).st_mode
        if stat.S_ISFIFO(mode) and _NONBLOCK:
            os.set_blocking(handle.fileno(), True)
        elif not stat.S_ISREG(mode):
            raise OSError("not a regular file or a pipe")
        yield handle
