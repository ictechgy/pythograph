"""분석 대상 소스를 `ast`로 읽을 때의 중첩 상한과 트리 크기 측정.

파싱한 트리는 여러 분석기가 재귀로 내려간다(`ast.NodeVisitor` 방문자, 이름·값 해석, URLconf·설정 해석). 트리가 너무
깊으면 파이썬 재귀 한도(기본 1000프레임)를 넘어 명령 전체가 내부 오류로 끝나므로, 상한보다 깊은 소스는 파싱 단계에서
읽지 못한 소스로 돌려 기존 한계(unparsed-files·route-coverage·unreadable-sources)로 보고한다. 명령마다 재귀가 쓰는
프레임이 달라 상한도 다르다(graph 계열 `MAX_AST_DEPTH`, routes·schema `MAX_EXTRACTION_AST_DEPTH`). 분석 도중
RecursionError를 잡아 계속하지 않는 것은 그때까지 채운 메모(별표 import 캐시의 자리표시, 해석기 메모)가 틀린 사실을
낼 수 있어서다. 같은 순회가 노드 수도 센다(`tree_overflow`, 상한은 `source.project.MAX_MODULE_SYNTAX_NODES`).
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import threading
import time
import tokenize
from collections.abc import Callable
from threading import Event, current_thread, main_thread
from typing import Generic, Literal, TypeVar, cast

#: 전용 스레드에서 돌린 작업의 결과 타입이다.
_Result = TypeVar("_Result")

#: 실행 중인 파서가 깊은 왼쪽 결합 사슬(`a + a + …`, `x.a.a…`, `f()()…`, `x[0][0]…`)에서 C 스택을 넘기는지다.
#: 3.10의 `ast.parse`는 그런 트리를 파이썬 객체로 바꾸다 프로세스가 신호로 죽고(종료 코드 139), 3.11부터는
#: RecursionError를 낸다.
_PARSER_OVERFLOWS_ON_CHAINS = sys.version_info < (3, 11)

#: 3.10에서 파싱 전에 허용하는 논리 줄 하나의 사슬 토큰(이항 연산자·`.`·`(`·`[`) 수다. 3.10.20 실측: 메서드 사슬
#: 8만 개(트리 깊이 16만)·`+` 15만 항에서 죽고, 메서드 사슬 5만 개·다른 사슬 10만 항은 안전했다. f-string 치환 필드
#: 안의 사슬도 임계가 같고(10만 항 안전, 15만 항에서 죽음), 바깥 사슬과 깊이가 더해진다.
MAX_CHAIN_TOKENS = 50_000

#: 3.10에서 부른 스레드의 스택으로 바로 파싱하는 사슬 추정치(`chain_bound`)의 상한이다. 추정치는 파일 전체의 사슬
#: 문자 수(문자열 안 포함)가 `MAX_CHAIN_TOKENS` 이하면 그 수이고, 넘으면 가장 긴 논리 줄의 사슬 토큰 수다. `ast.parse`는
#: C 트리를 파이썬 객체로 바꾸는 재귀(`ast2obj`)에 깊이 검사가 없어 트리 한 단계에 C 스택을 약 64 B 쓴다(3.10.20·clang
#: 실측: 1 MiB 스레드는 약 1.6만, Windows python.exe 주 스레드 크기인 2,000,000 B는 약 3.2만 단계에서 죽고, 메서드
#: 사슬은 고리 하나가 두 단계라 그 절반). `MAX_CHAIN_TOKENS`는 Linux 주 스레드(8 MiB)로 정한 값이라 Windows에서는
#: 상한 안의 사슬도 프로세스를 죽였다. 2,000은 1 MiB 임계의 1/8이라 MSVC의 더 큰 프레임과 호출자 프레임을 더해도
#: 남는다. 넘으면 전용 스택 스레드에서 파싱한다(`_run_on_large_stack`).
SMALL_STACK_CHAIN_TOKENS = 2_000

#: 긴 사슬을 파싱하는 전용 스레드가 사슬 토큰 하나에 잡는 스택(바이트)이다. 실측 한 단계(약 64 B)의 16배다. 토큰 수는
#: 트리 깊이의 상한이다(사슬 토큰 하나가 많아야 한 단계를 만든다).
PARSE_STACK_BYTES_PER_TOKEN = 1024

#: 전용 스레드 스택의 하한(Linux 주 스레드 기본값)이다. 상한은 `MAX_CHAIN_TOKENS` × 토큰당 바이트(약 49 MiB)로,
#: CPython이 Windows에서 받는 스레드 스택 상한(256 MiB 미만) 안이다.
MIN_PARSE_STACK_BYTES = 8 * 1024 * 1024

#: 전용 스레드 스택 크기를 맞추는 단위(1 MiB)다. macOS의 `pthread_attr_setstacksize`는 페이지 크기의 배수가 아니면
#: EINVAL을 돌려주고 CPython은 이를 ValueError로 낸다(arm64 macOS의 페이지는 16 KiB). 1 MiB는 4·16·64 KiB 페이지의
#: 공배수라 어느 플랫폼에서도 받는다.
PARSE_STACK_ALIGNMENT_BYTES = 1024 * 1024

#: 실행 중인 파서가 f-string 치환 필드를 따로 만든 파서로 다시 파싱하고, 그 파서의 깊이 한도(MAXSTACK, 약 6,000단)를
#: 처음부터 다시 세는지다(3.10·3.11). 사슬 수로 걸리지 않는 오른쪽 재귀(`~`·`not`·단항 `-`·`lambda`·조건식·`**`)를 겹친
#: f-string마다 파서 하나만큼의 C 스택이 더 쌓인다. 3.12부터는 f-string도 같은 파서가 읽어 한도를 넘으면 MemoryError다.
_FSTRING_PARSERS_STACK = sys.version_info < (3, 12)

#: f-string이 시작할 수 있는 곳(대소문자를 가리지 않는 `f`·`fr`·`rf` 바로 뒤의 따옴표)이다. 모든 접두사 철자(`f`·`F`·
#: `fr`·`fR`·`Fr`·`FR`·`rf`·`rF`·`Rf`·`RF`)가 따옴표 바로 앞에 `f`나 `fr`를 두므로 놓치는 f-string이 없다.
#: `if"x"`처럼 넉넉히 맞는 것은 그 소스를 f-string 몫을 더한 전용 스택에서 파싱하게 할 뿐이다.
_FSTRING_START = re.compile(r"[fF][rR]?['\"]")

#: 실행 중인 파서가 C 스택 경계를 직접 보는지다(3.14부터 문법 규칙마다 스택 포인터로 남은 스택을 확인한다). 3.13까지는
#: 깊이를 MAXSTACK(약 6,000단) 수로만 막아, 그 한도 안의 식도 작은 스택(1 MiB 스레드, 2,000,000 B인 Windows 주 스레드)을
#: 넘길 수 있다. macOS 3.13(arm64)은 1 MiB 스레드에서 `~` 5,900개를 f-string 셋에 겹친 식을 파싱하다 죽었다(SIGBUS,
#: 같은 식이 Linux 3.13.18에서는 768 KiB). Linux 3.13.18의 `~` 5,967개 식은 파싱에 768 KiB, 같은 스레드에서 트리까지
#: 해제하면 1,264 KiB가 든다(해제 재귀는 `parse_guarded` 참고).
_PARSER_CHECKS_C_STACK = sys.version_info >= (3, 14)

#: 3.13까지 전용 스택 밖(라이브러리로 부른 스레드)에서 바로 파싱하는 최대 길이(문자)다. 넘는 소스는 전용 스택에서
#: 파싱한다(3.10·3.11에서 f-string이 있을 수 있는 소스는 길이와 무관하게 전용 스택이다). 가장 비싼 모양은 닫지 않은
#: 여는 괄호다: 여는 괄호 하나가 문법 규칙 여럿을 지나 약 2 KiB(빌드에 따라 1.6–2.6 KiB)를 쓰고, 토크나이저의 괄호
#: 200단 한도에서 문법 오류로 끝나기 전까지 쌓인다(닫는 괄호는 거의 쓰지 않아 짝을 맞춘 식은 글자당 약 1 KiB).
#: 스레드 스택 16 KiB 단위 이분 탐색(닫지 않은·짝을 맞춘 `[`·`(`·`{`와 `~`, 식·모듈 문맥)의 128자 최악은 3.10.20(uv)
#: 272 KiB, 3.11.18(Ubuntu) 256 KiB, 3.11.15(uv 독립 빌드, CI가 쓰는 빌드) 348 KiB(`[`×126+`x=`, 4 KiB 단위),
#: 3.12.3(Ubuntu) 240 KiB, 3.12.13(uv) 304 KiB, 3.13.18·3.13.14(uv) 272 KiB다. 256자였다면 3.11.15에서 544 KiB였다.
#: 최악의 2배(696 KiB)가 1 MiB 스레드에 들어간다. macOS(arm64)는 Linux보다 프레임이 적어도 1.33배 크다(위 SIGBUS
#: 식으로 잰 하한). 임계 이하 최악 식을 512 KiB 스레드에서 읽는 테스트가 Linux·macOS CI 판마다 2배 여유를
#: 확인한다(Windows의 스레드 크기는 커밋 크기라 실제 예약은 python.exe 기본값 2,000,000 B이고, 그 테스트는 거기
#: 들어간다는 것만 보인다).
INLINE_PARSE_CHARACTERS = 128

#: 3.10·3.11에서 f-string이 있을 수 있는 소스를 파싱하는 전용 스레드에 더하는 스택(바이트)이다. 3.10·3.11의 f-string은
#: 따옴표 종류 넷(`"""`·`'''`·`"`·`'`)으로 4단까지 겹치고 파서마다 깊이 한도와 괄호 200단 한도를 다시 세므로 파서가 최대
#: 다섯 쌓인다. Linux 실측: 파서마다 한도 가까이 오른쪽 재귀를 두면 가장 비싼 조건식(`x if x else `)이 3.10.20(uv)
#: 4,656 KiB, 3.11.18(Ubuntu) 3,728 KiB, 3.11.15(uv 독립 빌드) 7,424 KiB(`not` 6,032, `~` 5,120)이고, 괄호 199단씩이면
#: 3.10.20 2,032 KiB(2,319자)다. 16 MiB는 최악의 약 2.2배다(전용 스택은 하한 8 MiB를 더해 24 MiB). 3.10의 `ast2obj`는
#: 파싱이 끝난 뒤 따로 돌아(합 약 3만 단, 약 1.9 MiB) 더하지 않는다.
FSTRING_STACK_ALLOWANCE_BYTES = 16 * 1024 * 1024

#: 3.11의 `ast2obj`(C 트리 → 파이썬 객체)가 허용하는 깊이가 재귀 한도에 비례하는지다(한도의 3배 단계, 3.12부터는 고정
#: 상수). 재귀 한도를 크게 올린 호출자에서는 그 깊이만큼의 C 스택이 필요하다.
_AST2OBJ_SCALES_WITH_RECURSION_LIMIT = (3, 11) <= sys.version_info < (3, 12)

#: 3.11 `ast2obj`가 트리 한 단계에 잡는 스택(바이트)이다. 실측(재귀 한도 1만·4만, 1 MiB 단위 이분 탐색): 3.11.18(Ubuntu)
#: 96–104 B, 3.11.15(uv) 87–104 B. 그 2.4배 이상이다.
AST2OBJ_STACK_BYTES_PER_LEVEL = 256

#: 3.13까지 PEG 파서 하나가 재귀할 수 있는 깊이(MAXSTACK)다. 왼쪽 결합 사슬(`_CHAIN_MARKS`)이 아닌 중첩은 파서 하나에서
#: 이 깊이를 넘지 못하고, 3.10·3.11의 f-string은 파서를 다섯까지 겹친다.
_PARSER_DEPTH_LEVELS = 6_000

#: 전용 스레드 스택의 상한(바이트)이다. CPython은 Windows에서 256 MiB(`THREAD_MAX_STACKSIZE`) 미만의 스레드 스택만
#: 받는다(`thread_nt.h`의 `size < THREAD_MAX_STACKSIZE`). 그보다 작은 가장 큰 1 MiB 배수다. 이보다 큰 스택이 필요하다고
#: 어림한 소스(재귀 한도를 아주 크게 올린 3.11에서 사슬 토큰이 약 100만 개 넘는 논리 줄)는 파싱하지 않고 중첩 이유로
#: 센다(3.10의 사슬 상한과 같다).
MAX_PARSE_STACK_BYTES = 255 * 1024 * 1024

#: 명령 스레드를 기다리는 주 스레드가 깨어나는 간격(초)이다. Windows에서는 시간 제한 없는 `Thread.join`이 Ctrl+C를 받지
#: 못해(잠금 대기가 경보를 받지 않는다), 그 간격마다 돌아와 KeyboardInterrupt를 받는다(Windows CI에는 이를 확인하는
#: 테스트가 없다).
_JOIN_POLL_SECONDS = 0.1

#: 주 스레드가 Ctrl+C 같은 예외를 받은 뒤 명령 스레드가 멈추기를 기다리는 시간(초)이다. 명령 스레드에는
#: KeyboardInterrupt를 넣지만(`PyThreadState_SetAsyncExc`), 파이썬 바이트코드를 실행할 때만 받으므로 C 호출 하나가 끝날
#: 때까지 늦는다. 가장 긴 C 호출 하나는 4 MiB 소스의 `ast.parse`로, 실측(Linux, 3.10.20·3.11.18·uv 3.11.15·3.12.3·
#: 3.13.18) 최대 12.1초였다(짧은 문장 100만 개. 3.13의 들여쓴 `json.dumps`도 C 호출이지만 108 MiB에 1.4초). 그 약
#: 1.7배다. 두 번째 Ctrl+C나 시간이 다 되면 주 스레드는 기다리지 않고 예외를 다시 낸다(명령 스레드는 데몬이고 표준
#: 입출력을 쓰지 않으므로 버려도 된다).
COMMAND_STOP_GRACE_SECONDS = 20.0

#: 스레드 시작이 시작 도중에 난 다른 예외(Ctrl+C 등)를 처리하던 중 실패했을 때, 스레드가 실제로 시작해 작업에 들어오는지
#: 기다리는 시간(초)이다. 시작한 스레드는 밀리초 안에 들어온다. 부른 쪽이 시작 전부터 처리하던 예외는 해당하지 않는다.
_START_CONFIRM_SECONDS = 1.0

#: 전용 스택 스레드를 만들지 못해 파싱하지 않은 모듈의 읽지 못한 소스 이유다(파서의 깊이 한도와 구분한다).
PARSE_STACK_UNAVAILABLE_REASON = "could not start a parser thread with a large enough stack"

#: 사슬을 잇는 연산자 토큰이다. `(`·`[`는 호출·첨자일 때만 잇지만 넉넉히 센다(상한 판정에서 더 세는 쪽은 안전하다).
_CHAIN_OPERATORS = frozenset({"+", "-", "*", "/", "//", "%", "@", "**", "<<", ">>", "&", "|", "^", ".", "(", "["})

#: 사슬 연산자 토큰마다 적어도 하나씩 든 문자다. 이 문자의 총수가 상한 이하면 어느 줄도 상한을 넘을 수 없다.
_CHAIN_MARKS = "+-*/%@<>&|^.(["

#: graph·reach·impact가 분석하는 구문 트리의 최대 중첩 깊이다(문자열 주석도 이 상한을 쓴다). 모듈 노드를 1로 세고
#: 문맥·연산자 잎 노드도 센다.
#:
#: 근거(3.10~3.14 실측): 재귀 방문자는 한 단계에 프레임 2개를 쓰고, 방문 중 이름 해석이 다른 범위의 binding 수집(같은
#: 방문자)을 처음 부르면 두 순회가 겹쳐 한 단계에 4프레임이다. 그 사이에 유한 사슬(64단 MRO, 값·import 사슬)이 최대
#: 약 265프레임 끼고, 3.10·3.11은 MRO 경로에서 약 60프레임을 더 세며, pytest 같은 호출자가 약 35프레임을 먼저 쓴다:
#: 4 × 128 + 265 + 60 + 35 ≈ 870 < 1000. 단순한 깊은 식은 깊이 약 247(첨자 사슬)부터 실패했다. 공개 패키지 약 6만
#: 파일에서 깊이가 100을 넘는 것은 sympy 파일 3개뿐이었다.
MAX_AST_DEPTH = 128

#: routes(server·client)·schema가 분석하는 구문 트리의 최대 중첩 깊이다. graph 상한을 그대로 쓰면 이 명령들이 문제없이
#: 분석하던 모듈(깊이 129~약 400)까지 빼고 문서 전체 한계를 달게 되므로 따로 둔다.
#:
#: 근거(상한 없는 a995956 실측, 3.10·3.13): 이 명령들은 graph의 정의·프로그램 방문자를 쓰지 않는다. URLconf·설정
#: 해석은 한 단계에 1프레임 남짓이고, 이름·값·상수 해석은 홉·깊이 상한(16·32·12단)에서 끊긴다. 가장 깊이 내려가는
#: 것은 client의 binding 수집(`_BindingCollector`, 한 단계 2프레임, 방문 중 다른 범위를 수집하지 않는다)이고, 그 앞에
#: 값 추적이 최대 약 160프레임 낀다: 2 × 256 + 160 + 50 ≈ 720 < 1000. 상한 없는 코드에서 한 파일 모양 8가지는 깊이
#: 약 400, 깊은 식 여러 개를 엮은 입력은 300까지 세 명령이 모두 0이었고, client는 함수 안 메서드 사슬 깊이
#: 465(3.10)까지 통과하고 525(3.13)에서 실패했다. 그래서 400은 여유가 없고 256을 쓴다.
MAX_EXTRACTION_AST_DEPTH = 256


class ParseStackUnavailableError(Exception):
    """긴 소스를 파싱할 큰 스택의 스레드를 만들 수 없었다. 호출자는 그 모듈·주석을 분석하지 못한 것으로 센다."""


def exceeds_depth(tree: ast.AST, limit: int = MAX_AST_DEPTH) -> bool:
    """구문 트리의 중첩이 상한을 넘는지 재귀 없이 잰다(노드 수는 보지 않는다).

    Args:
        tree: 구문 트리.
        limit: 허용하는 최대 깊이(뿌리가 1).

    Returns:
        상한보다 깊으면 True.
    """
    return tree_overflow(tree, limit, sys.maxsize) == "depth"


def tree_overflow(tree: ast.AST, depth_limit: int, node_limit: int) -> Literal["depth", "nodes"] | None:
    """구문 트리가 중첩 상한이나 노드 수 상한을 넘는지 재귀 없이 한 번에 잰다.

    한 층씩 내려가며 다음 층 노드를 모으고 지금까지의 노드 수를 더한다. 노드 수가 상한을 넘거나 상한 층을 넘어 노드가
    남으면 바로 멈춘다. 모듈마다 한 번 도는 순회 하나로 두 상한을 함께 본다(노드 수를 따로 세면 모든 모듈을 한 번 더
    돈다).

    Args:
        tree: 구문 트리.
        depth_limit: 허용하는 최대 깊이(뿌리가 1).
        node_limit: 허용하는 최대 노드 수(문맥·연산자 잎 노드 포함).

    Returns:
        노드 수 상한을 넘으면 `"nodes"`, 중첩 상한을 넘으면 `"depth"`, 둘 다 아니면 None.
    """
    level: list[ast.AST] = [tree]
    nodes = 1
    for _ in range(depth_limit):
        level = _next_level(level)
        if not level:
            return None
        nodes += len(level)
        if nodes > node_limit:
            return "nodes"
    return "depth"


def _next_level(level: list[ast.AST]) -> list[ast.AST]:
    """한 층 노드의 자식을 모두 모은다.

    모든 모듈을 한 번씩 도는 비용이라 `ast.iter_child_nodes` 생성기 대신 필드를 직접 읽고 지역 이름에 묶는다(Django
    907파일에서 0.19초 → 0.10초).

    Args:
        level: 한 층의 노드.

    Returns:
        다음 층의 노드.
    """
    node_type = ast.AST
    following: list[ast.AST] = []
    append = following.append
    for node in level:
        for name in node._fields:
            value = getattr(node, name, None)
            if isinstance(value, node_type):
                append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, node_type):
                        append(item)
    return following


def parse_annotation(text: str) -> ast.expr | None:
    """문자열 주석(전방 참조)을 식으로 파싱한다. 모듈과 같은 상한을 지킨다.

    graph·client·schema가 함께 쓴다. 문법 오류뿐 아니라 NUL(3.10은 ValueError), 파서의 깊이 한도(RecursionError·
    MemoryError), 상한보다 깊은 식, 3.10에서 프로세스를 죽이는 긴 사슬도 모두 "모르는 주석"으로 돌려 호출자가 그
    주석만 무시하게 한다. 파싱한 주석은 호출자가 재귀로 풀기 때문에 깊이 상한이 필요하다.

    Args:
        text: 주석 문자열.

    Returns:
        식, 파싱할 수 없거나 상한을 넘으면 None.
    """
    bound = chain_bound(text)
    if bound > MAX_CHAIN_TOKENS:
        return None
    try:
        tree, overflow = parse_guarded(text, "<unknown>", "eval", bound, MAX_AST_DEPTH, sys.maxsize)
    except (SyntaxError, ValueError, RecursionError, MemoryError, ParseStackUnavailableError):
        return None
    return None if overflow is not None else cast(ast.Expression, tree).body


def chain_too_long(text: str) -> bool:
    """실행 중인 파서가 C 스택을 넘길 만큼 긴 사슬이 한 논리 줄에 있는지 본다(3.10만 해당).

    파싱 전에 걸러야 하는 이유: 3.10은 파싱 도중 프로세스가 죽어 잡을 예외가 없다. 걸러진 소스는 파서가 거부한 깊은
    소스와 같은 실패로 남긴다. f-string 치환 필드 안의 사슬도 센다. 상한을 넘는 얕은 줄(사슬 토큰이 아주 많은 한 줄짜리
    목록, 사슬 문자가 아주 많은 f-string)도 3.10에서는 함께 걸러진다. 상한 안의 사슬도 작은 스택에서는 넘칠 수 있어
    `parse_guarded`가 전용 스택에서 파싱한다.

    Args:
        text: 파싱할 소스.

    Returns:
        파싱하면 안 되면 True. 3.11 이상은 파서가 스스로 RecursionError를 내므로 늘 False다.
    """
    return chain_bound(text) > MAX_CHAIN_TOKENS


def chain_bound(text: str) -> int:
    """3.10에서 논리 줄 하나의 사슬 토큰 수의 상한(`_longest_chain`)을 돌려준다. 3.11 이상은 볼 필요가 없어 0이다.

    Args:
        text: 파싱할 소스.

    Returns:
        사슬 토큰 수의 상한(3.11 이상은 0).
    """
    return _longest_chain(text) if _PARSER_OVERFLOWS_ON_CHAINS else 0


def parse_guarded(
    text: str, filename: str, mode: Literal["exec", "eval"], bound: int, depth_limit: int, node_limit: int
) -> tuple[ast.AST | None, Literal["depth", "nodes"] | None]:
    """`ast.parse`와 트리 상한 판정(`tree_overflow`)을 하되, 부른 스레드의 스택을 넘길 수 있는 소스는 C 스택이 넉넉한
    스레드에서 한다.

    필요한 스택(`guarded_stack_bytes`)이 0이거나 지금 스레드가 이미 그만한 전용 스택이면(`run_on_parse_stack`으로 돌린
    CLI 명령, 전용 스레드 안) 그 자리에서 파싱한다. 아니면 그 크기의 스레드를 만들어 파싱하고 바로 합류한다. 상한을
    넘는 트리는 파싱한 스레드에서 버리고 이유만 돌려준다: 3.13은 깊은 트리를 해제하는 재귀를 C 재귀 한도(Linux·macOS
    약 1만, Windows 3,000)에 거의 닿을 때까지 미루지 않아, 깊은 트리를 작은 스택에서 버리면 해제하다 죽는다.

    스레드를 만들 수 없으면(스레드 없는 빌드, 3.10·3.11의 격리 하위 인터프리터, 자원 부족) 부른 스레드에서 파싱해도
    되는 소스(`_parses_safely_inline`)만 그 자리에서 파싱하고 나머지는 `ParseStackUnavailableError`다. 0.3.1은 모든
    소스를 부른 스레드에서 파싱했다. 필요한 스택이 `MAX_PARSE_STACK_BYTES`를 넘는다고 어림한 소스(3.11)는 파싱하지 않고
    RecursionError로 센다. 결과와 예외는 같은 스레드에서 한 것과 같다.

    Args:
        text: 소스.
        filename: 파서에 줄 파일 이름.
        mode: `exec`(모듈)나 `eval`(식).
        bound: 사슬 추정치(`chain_bound`, `MAX_CHAIN_TOKENS` 이하로 확인한 값).
        depth_limit: 허용하는 트리 깊이(`tree_overflow`).
        node_limit: 허용하는 트리 노드 수(`tree_overflow`).

    Returns:
        (트리, None), 상한을 넘으면 (None, `"depth"`나 `"nodes"`).

    Raises:
        SyntaxError: 문법 오류.
        ValueError: NUL 등 파서가 받지 않는 입력.
        RecursionError: 파서의 깊이 한도, 또는 필요한 스택이 `MAX_PARSE_STACK_BYTES`를 넘는다고 어림했을 때.
        MemoryError: 파서의 메모리 부족·깊이 한도.
        ParseStackUnavailableError: 그 스레드를 만들 수 없고 부른 스레드에서 파싱하면 안전하지 않을 때.
    """

    def parse_and_measure() -> tuple[ast.AST | None, Literal["depth", "nodes"] | None]:
        """파싱하고 상한을 잰다. 넘는 트리는 이 스레드에서 해제되도록 돌려주지 않는다."""
        tree = ast.parse(text, filename=filename, mode=mode)
        overflow = tree_overflow(tree, depth_limit, node_limit)
        return (None, overflow) if overflow is not None else (tree, None)

    stack = guarded_stack_bytes(text, bound)
    if stack <= current_parse_stack_bytes():
        return parse_and_measure()
    if stack > MAX_PARSE_STACK_BYTES:
        raise RecursionError(f"the source may need a {stack}-byte parser stack (cap {MAX_PARSE_STACK_BYTES})")
    try:
        return _run_on_large_stack(parse_and_measure, stack)
    except ParseStackUnavailableError:
        if not _parses_safely_inline(text, bound):
            raise
    return parse_and_measure()


def guarded_stack_bytes(text: str, bound: int) -> int:
    """소스를 파싱할 스택의 크기를 돌려준다. 어느 스레드에서 파싱해도 되면 0이다.

    3.13까지 `INLINE_PARSE_CHARACTERS`보다 긴 소스, 3.10의 긴 사슬, 3.10·3.11에서 f-string이 있을 수 있는 소스(길이와
    무관, 델타 재리뷰 S1: 파서가 f-string마다 한도를 다시 세어 글자 수로 상한을 잡기 어렵다)가 전용 스택을 쓴다.
    기본은 `MIN_PARSE_STACK_BYTES`, 3.10의 긴 사슬은 토큰에 비례한 크기(`parse_stack_bytes`), 3.11은 재귀 한도에 비례한
    `ast2obj` 몫(`_ast2obj_stack_bytes`) 중 큰 것이다. f-string이 있을 수 있으면 겹친 파서 몫
    (`FSTRING_STACK_ALLOWANCE_BYTES`)을 더한다. 모두 1 MiB의 배수라 합도 플랫폼이 받는 크기다.

    Args:
        text: 소스.
        bound: 사슬 추정치(`chain_bound`).

    Returns:
        스택 크기(바이트), 필요 없으면 0.
    """
    chains = parse_stack_bytes(bound) if bound > SMALL_STACK_CHAIN_TOKENS else 0
    fstrings = _FSTRING_PARSERS_STACK and may_contain_fstring(text)
    if not chains and not fstrings and (_PARSER_CHECKS_C_STACK or len(text) <= INLINE_PARSE_CHARACTERS):
        return 0
    allowance = FSTRING_STACK_ALLOWANCE_BYTES if fstrings else 0
    return (
        max(chains or MIN_PARSE_STACK_BYTES, _ast2obj_stack_bytes(text, MAX_PARSE_STACK_BYTES - allowance)) + allowance
    )


def largest_parse_stack_bytes() -> int:
    """실행 중인 파이썬에서 어떤 허용 소스라도 그 자리에서 파싱할 수 있는 스택 크기를 돌려준다(필요 없으면 0).

    3.10은 가장 긴 허용 사슬(`MAX_CHAIN_TOKENS`)과 f-string 몫(65 MiB), 3.11은 f-string 몫과 재귀 한도에 비례한
    `ast2obj` 몫(기본 한도에서 24 MiB), 3.12·3.13은 `MIN_PARSE_STACK_BYTES`다. 3.14부터는 0이다.

    Returns:
        스택 크기(바이트, `MAX_PARSE_STACK_BYTES` 이하).
    """
    if _PARSER_CHECKS_C_STACK and not _PARSER_OVERFLOWS_ON_CHAINS:
        return 0
    stack = MIN_PARSE_STACK_BYTES
    if _PARSER_OVERFLOWS_ON_CHAINS:
        stack = max(stack, parse_stack_bytes(MAX_CHAIN_TOKENS))
    if _AST2OBJ_SCALES_WITH_RECURSION_LIMIT:
        stack = max(stack, _aligned_stack_bytes(3 * sys.getrecursionlimit() * AST2OBJ_STACK_BYTES_PER_LEVEL))
    if _FSTRING_PARSERS_STACK:
        stack += FSTRING_STACK_ALLOWANCE_BYTES
    return min(stack, MAX_PARSE_STACK_BYTES)


def current_parse_stack_bytes() -> int:
    """지금 스레드가 pythograph가 만든 전용 스택 스레드면 그 크기, 아니면 0을 돌려준다."""
    return cast(int, getattr(_parse_stack, "bytes", 0))


def may_contain_fstring(text: str) -> bool:
    """소스에 f-string이 있을 수 있는지 넉넉히 본다(`_FSTRING_START`, 문자열·주석 안의 것도 맞는다).

    Args:
        text: 소스.

    Returns:
        f-string 시작이 있을 수 있으면 True(놓치는 f-string은 없다).
    """
    return _FSTRING_START.search(text) is not None


def parse_stack_bytes(bound: int) -> int:
    """사슬 토큰 수 상한 `bound`를 파싱할 전용 스레드의 스택 크기(바이트)를 돌려준다.

    토큰당 `PARSE_STACK_BYTES_PER_TOKEN`을 잡되 `MIN_PARSE_STACK_BYTES` 이상이고, 플랫폼이 받는 크기가 되도록
    `PARSE_STACK_ALIGNMENT_BYTES`의 배수로 올린다.

    Args:
        bound: 사슬 토큰 수의 상한.

    Returns:
        스레드 스택 크기(바이트).
    """
    return _aligned_stack_bytes(max(MIN_PARSE_STACK_BYTES, bound * PARSE_STACK_BYTES_PER_TOKEN))


def run_on_parse_stack(work: Callable[[], _Result]) -> _Result:
    """`work`(CLI 명령의 계산 부분)를 `largest_parse_stack_bytes` 크기의 스레드에서 돌려, 그 안의 파싱이 모두 그
    자리에서 일어나게 한다.

    파일마다 스레드를 오가면 트리를 다른 코어에서 만들고 부른 코어에서 읽고 해제하는 비용이 든다. 명령 전체를 한
    스레드에서 돌리면 그런 이동이 없다. `work`는 표준 입출력에 쓰지 않아야 한다(결과는 부른 스레드가 쓴다). 그래야
    Ctrl+C가 출력 쓰기를 부른 스레드에서 끊고, 명령 스레드를 버려도 출력 버퍼 잠금을 쥔 채 남지 않는다.

    주 스레드는 `_JOIN_POLL_SECONDS`마다 깨어나 기다린다. 기다리는 동안 예외(Ctrl+C의 KeyboardInterrupt, 신호 처리기가
    낸 예외)를 받으면 명령 스레드에 KeyboardInterrupt를 넣어 `with`·`finally`(임시 디렉터리 지우기, git 자식 정리)를
    풀게 하고 `COMMAND_STOP_GRACE_SECONDS`까지 기다린 뒤 그 예외를 다시 낸다. 기다리는 중 두 번째 예외는 바로 나간다.
    `work`의 결과와 예외는 부른 스레드에서 그대로 돌려주거나 다시 낸다. `work`는 두 번 돌지 않는다. 스택이 필요
    없거나(3.14 이상) 이미 그만한 전용 스택이거나 스레드를 만들 수 없으면(데몬 스레드를 막은 하위 인터프리터, 스레드
    없는 빌드) 부른 스레드에서 돌린다(파싱마다 `parse_guarded`가 다시 판단한다).

    Args:
        work: 돌릴 작업.

    Returns:
        작업의 결과.
    """
    stack = largest_parse_stack_bytes()
    if stack <= current_parse_stack_bytes():
        return work()
    try:
        thread = _ParseThread(work, stack, daemon=True)
        thread.start()
    except ParseStackUnavailableError:
        unavailable = True
    else:
        unavailable = False
    if unavailable:
        # `except` 블록 밖에서 돌린다. 안에서 돌리면 명령 전체가 그 예외를 처리하는 중이 되어, 뒤에 실패한 스레드
        # 시작마다 그 예외가 앞 예외(`__context__`)로 붙는다.
        return work()
    return thread.wait()


def _run_on_large_stack(work: Callable[[], _Result], stack: int) -> _Result:
    """주어진 크기의 스택을 가진 스레드를 만들어 `work`를 돌리고, 바로 합류해 결과나 예외를 부른 스레드로 넘긴다.

    스레드는 데몬이 아니다(데몬 스레드를 막은 3.12·3.13 격리 하위 인터프리터에서도 만들 수 있고, 바로 합류하므로 종료를
    막지 않는다). 남아 있는 스레드가 없어 하위 인터프리터를 끝낼 수 있고 fork 경고도 없다. 작업 스레드에서 난 예외는
    `BaseException`까지 담아 부른 스레드에서 그대로 다시 낸다. 기다리는 중 부른 스레드가 예외를 받으면 그 스레드를
    멈추게 하고 다시 낸다(`_ParseThread.wait`).

    Args:
        work: 돌릴 작업.
        stack: 스레드 스택 크기(바이트, `PARSE_STACK_ALIGNMENT_BYTES`의 배수).

    Returns:
        작업의 결과.

    Raises:
        ParseStackUnavailableError: 그 크기의 스레드를 만들 수 없을 때(스레드가 시작하지 않은 것이 확실할 때만).
    """
    thread = _ParseThread(work, stack, daemon=False)
    thread.start()
    return thread.wait()


class _ParseThread(Generic[_Result]):
    """전용 스택 스레드 하나와 그 결과·취소 상태다."""

    def __init__(self, work: Callable[[], _Result], stack: int, *, daemon: bool) -> None:
        """스레드를 만든다(아직 시작하지 않는다).

        Args:
            work: 스레드에서 돌릴 작업.
            stack: 스레드 스택 크기(바이트).
            daemon: 데몬 스레드인지.

        Raises:
            ParseStackUnavailableError: 스레드를 만들 수 없을 때(데몬 스레드를 막은 하위 인터프리터 등).
        """
        self._work = work
        self._stack = stack
        self._outcome: list[_Result | BaseException] = []
        #: 스레드가 작업 함수에 들어왔는지다(시작이 중간에 끊겼을 때 스레드가 실제로 시작했는지 가린다).
        self._entered = Event()
        #: 부른 쪽이 그만두었는지다. 늦게 시작한 스레드는 작업을 돌리지 않는다.
        self._cancelled = Event()
        #: 작업 함수가 끝났는지다. 끝나기를 이것으로 기다린다: 3.12까지는 Ctrl+C가 끊은 `Thread.join`이 살아 있는
        #: 스레드를 끝난 것으로 표시해(bpo-45274 처리) 그 뒤 `is_alive`·`join`을 믿을 수 없다.
        self._done = Event()
        try:
            self._thread = threading.Thread(target=self._run, name="pythograph-parse", daemon=daemon)
        except (RuntimeError, ValueError) as error:
            raise ParseStackUnavailableError(f"could not create a {stack}-byte parser thread") from error

    def start(self) -> None:
        """스레드를 시작한다.

        `threading.stack_size`는 프로세스(인터프리터) 전체 설정이라 잠금(`_stack_size_guard`) 안에서 바꾸고 시작한 직후
        되돌린다. 그 순간 호스트 프로그램이 만든 스레드는 이 크기를 받을 수 있다. 시작이 RuntimeError·ValueError로
        끝나도 그 예외가 시작 도중에 난 다른 예외를 처리하다 난 것(시작을 기다리던 중 Ctrl+C가 끊어 "release unlocked
        lock"이 된 경우 등)이고 스레드가 실제로 작업에 들어왔으면, 시작하지 못한 것으로 보지 않고 스레드를 멈추게 한 뒤
        그 앞의 예외를 다시 낸다(작업을 부른 스레드에서 한 번 더 돌리지 않는다). 다른 예외(KeyboardInterrupt)도 스레드를
        멈추게 한 뒤 다시 낸다.

        시작 도중에 났는지는 앞 예외(`__context__`)가 시작 전에 부른 쪽이 처리하던 예외(`sys.exc_info()`)와 다른지로
        가린다. 부른 쪽이 `except` 블록 안에서 파싱하면 실패한 시작의 앞 예외가 그 예외이고, 그때 기다리면 실패마다
        `_START_CONFIRM_SECONDS`가 더 걸린다(4차 리뷰 B1: CLI가 명령 전체를 그런 블록 안에서 돌려 파일마다 1초).

        Raises:
            ParseStackUnavailableError: 스레드가 시작하지 않은 것이 확실할 때.
        """
        handled = sys.exc_info()[1]
        with _stack_size_guard.lock:
            try:
                previous = threading.stack_size()
            except (RuntimeError, ValueError) as error:
                raise ParseStackUnavailableError("thread stack sizes cannot be set") from error
            try:
                threading.stack_size(self._stack)
                self._thread.start()
            except BaseException as error:  # 잠금을 놓은 뒤 가린다(멈추기를 기다리는 동안 잠금을 쥐지 않는다).
                failure = error
            else:
                return
            finally:
                threading.stack_size(previous)
        if isinstance(failure, (RuntimeError, ValueError)):
            interrupted = failure.__context__
            if interrupted is None or interrupted is handled or not self._entered.wait(_START_CONFIRM_SECONDS):
                self._cancelled.set()
                raise ParseStackUnavailableError(f"could not start a {self._stack}-byte parser thread") from failure
            failure = interrupted
        self.stop()
        raise failure

    def wait(self) -> _Result:
        """스레드가 끝나기를 기다려 결과를 돌려주거나 예외를 다시 낸다.

        기다리는 동안 부른 스레드가 예외를 받으면 스레드를 멈추게 하고(`stop`) 그 예외를 다시 낸다.

        Returns:
            작업의 결과.
        """
        try:
            while not self._done.wait(_JOIN_POLL_SECONDS):
                pass
            self._thread.join()
        except BaseException:
            self.stop()
            raise
        if not self._outcome:  # 작업 함수는 결과나 예외를 언제나 담는다(담기마저 실패한 경우만 남는다).
            raise AssertionError("the parser thread ended without recording a result or an exception")
        result = self._outcome[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def stop(self) -> None:
        """스레드를 멈추게 하고 `COMMAND_STOP_GRACE_SECONDS`까지 끝나기를 기다린다.

        아직 작업에 들어오지 않았으면 들어오지 않게 하고, 작업 중이면 KeyboardInterrupt를 넣어 `with`·`finally`를 풀게
        한다. 기다리는 중 받은 예외는 그대로 나간다(두 번째 Ctrl+C는 기다리지 않는다).
        """
        self._cancelled.set()
        if self._entered.is_set() and not self._outcome:
            _raise_in_thread(self._thread.ident, KeyboardInterrupt)
        elif not self._entered.is_set() and self._thread.ident is None:
            return  # 아직(또는 끝내) 시작하지 않았다. 시작해도 취소 표시를 보고 작업하지 않는다.
        deadline = time.monotonic() + COMMAND_STOP_GRACE_SECONDS
        while not self._done.wait(min(_JOIN_POLL_SECONDS, max(0.0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                return

    def _run(self) -> None:
        """스레드에 스택 크기를 표시하고 작업의 결과나 예외를 담는다(넣은 KeyboardInterrupt가 어디서 닿아도 담는다)."""
        try:
            self._entered.set()
            if self._cancelled.is_set():
                return
            _parse_stack.bytes = self._stack
            self._outcome.append(self._work())
        except BaseException as error:  # 부른 스레드에서 같은 예외로 다시 낸다(`wait`).
            self._outcome.append(error)
        finally:
            self._done.set()


def _raise_in_thread(ident: int | None, exception: type[BaseException]) -> None:
    """다른 파이썬 스레드에 비동기 예외를 넣는다(`PyThreadState_SetAsyncExc`, 문서화된 C API).

    그 스레드는 다음에 바이트코드를 실행할 때 예외를 받는다. ctypes가 없는 빌드에서는 아무것도 하지 않는다(부른 쪽은
    기다리다 시간이 되면 스레드를 버린다).

    Args:
        ident: 스레드 id(`Thread.ident`).
        exception: 넣을 예외 클래스.
    """
    if ident is None:
        return
    try:
        import ctypes  # noqa: PLC0415 — 중단할 때만 필요하고, ctypes가 없는 빌드에서도 모듈을 읽을 수 있어야 한다
    except ImportError:
        return
    set_async_exc = getattr(getattr(ctypes, "pythonapi", None), "PyThreadState_SetAsyncExc", None)
    if set_async_exc is None:
        return
    if set_async_exc(ctypes.c_ulong(ident), ctypes.py_object(exception)) > 1:  # 문서대로, 여럿이면 되돌린다.
        set_async_exc(ctypes.c_ulong(ident), None)


def _parses_safely_inline(text: str, bound: int) -> bool:
    """전용 스레드를 만들 수 없을 때 이 소스를 부른 스레드에서 파싱해도 되는지 본다.

    다음 중 하나면 안 된다(0.3.1은 모두 부른 스레드에서 파싱했다): 3.10에서 사슬 토큰이
    2,000개(`SMALL_STACK_CHAIN_TOKENS`) 넘는 논리 줄이 있다, 3.10·3.11에서 치환 필드의 식 안에 f-string이
    있다(`_may_nest_fstrings`, 파서가 셋 이상 겹친다), 3.11에서 가장 긴 논리 줄로 어림한 `ast2obj` 몫이
    `MIN_PARSE_STACK_BYTES`를 넘는다(재귀 한도를 올린 호출자), 128자(`INLINE_PARSE_CHARACTERS`)를 넘는데 부른 스레드가
    주 스레드가 아니다(라이브러리 스레드의 스택은 알 수 없다). 토큰으로 나눌 수 없는 소스는 판단할 수 없어 안 된다고
    본다. 주 스레드는 Linux·macOS 기본 8 MiB로 본다: Windows python.exe의 주 스레드(2,000,000 B)나 `ulimit -s`를 줄인
    프로세스에서는 이 규칙 안의 소스도 0.3.1처럼 넘칠 수 있다(4차 리뷰 N3, 실측 없는 Windows 예산은 두지 않는다).

    Args:
        text: 소스.
        bound: 사슬 추정치(`chain_bound`).

    Returns:
        부른 스레드에서 파싱해도 되면 True.
    """
    if len(text) > INLINE_PARSE_CHARACTERS and current_thread() is not main_thread():
        return False
    if _ast2obj_stack_bytes(text, MIN_PARSE_STACK_BYTES) > MIN_PARSE_STACK_BYTES:
        return False
    if bound > SMALL_STACK_CHAIN_TOKENS:
        longest = _logical_line_chain(text, SMALL_STACK_CHAIN_TOKENS)
        if longest is None or longest > SMALL_STACK_CHAIN_TOKENS:
            return False
    return not (_FSTRING_PARSERS_STACK and may_contain_fstring(text) and _may_nest_fstrings(text))


def _ast2obj_stack_bytes(text: str, budget: int) -> int:
    """3.11 `ast2obj`가 이 소스에서 쓸 수 있는 스택(바이트, 1 MiB 배수)을 어림한다(다른 버전은 0).

    허용 깊이는 재귀 한도의 3배 단계다. 트리가 그만큼 깊어질 수 있는 것은 왼쪽 결합 사슬뿐이라(다른 중첩은 파서 하나에
    `_PARSER_DEPTH_LEVELS`, f-string이 있으면 파서 다섯) 깊이를 사슬 문자 수(`_count_marks`)와 그 몫의 합으로 묶는다. 그
    어림이 `budget`을 넘으면 논리 줄마다 사슬 토큰을 세어(가장 긴 줄) 다시 어림한다. 평평한 큰 모듈은 몇 MiB로 끝난다.
    `budget`은 부르는 쪽이 이 값을 비교할 한도다(전용 스택은 상한에서 f-string 몫을 뺀 값, 스레드를 만들 수 없을 때는
    `MIN_PARSE_STACK_BYTES`). 다른 한도와 비교하면 다시 세지 않은 파일 전체 어림이 한도를 넘어 평평한 모듈을
    거부했다(4차 리뷰 S2·N2).

    Args:
        text: 소스.
        budget: 파일 전체 어림이 이보다 크면 논리 줄마다 다시 센다(바이트).

    Returns:
        스택 크기(바이트).
    """
    if not _AST2OBJ_SCALES_WITH_RECURSION_LIMIT:
        return 0
    limit = 3 * sys.getrecursionlimit()
    nesting = _PARSER_DEPTH_LEVELS * (5 if may_contain_fstring(text) else 1)
    levels = min(limit, _count_marks(text) + nesting)
    if levels * AST2OBJ_STACK_BYTES_PER_LEVEL > budget:
        chain = _logical_line_chain(text, limit)
        if chain is not None:
            levels = min(limit, chain + nesting)
    return _aligned_stack_bytes(levels * AST2OBJ_STACK_BYTES_PER_LEVEL)


def _aligned_stack_bytes(wanted: int) -> int:
    """`wanted` 이상인 가장 작은 `PARSE_STACK_ALIGNMENT_BYTES`의 배수를 돌려준다."""
    return -(-wanted // PARSE_STACK_ALIGNMENT_BYTES) * PARSE_STACK_ALIGNMENT_BYTES


class _StackSizeGuard:
    """`threading.stack_size`를 바꾸고 스레드를 시작하고 되돌리는 동안 잡는 잠금이다."""

    def __init__(self) -> None:
        """잠금을 만든다."""
        self.lock = threading.Lock()

    def reset(self) -> None:
        """fork한 자식에서 잠금을 새로 만든다(부모의 다른 스레드가 잡은 채 복사됐을 수 있다)."""
        self.lock = threading.Lock()


#: 전용 스택 스레드가 자기 스택 크기를 표시하는 스레드별 저장소다(`current_parse_stack_bytes`).
_parse_stack = threading.local()

#: `threading.stack_size` 설정·스레드 시작·복원을 직렬화하는 잠금이다(fork한 자식은 새로 만든다).
_stack_size_guard = _StackSizeGuard()


def _reset_stack_size_guard_after_fork() -> None:
    """fork한 자식이 부모에서 잡힌 채 복사된 잠금을 기다리지 않게 한다."""
    _stack_size_guard.reset()


if hasattr(os, "register_at_fork"):  # Windows에는 fork가 없다.
    os.register_at_fork(after_in_child=_reset_stack_size_guard_after_fork)


def _longest_chain(text: str) -> int:
    """논리 줄 하나에 든 사슬 토큰 수의 최댓값을 재귀 없이 선형 시간에 센다.

    사슬 문자의 총수가 상한 이하면 토큰으로 나누지 않고 그 수를 돌려준다(어느 줄도 그보다 많을 수 없다). 상한을 넘는
    줄을 만나면 바로 멈춘다. f-string 안의 사슬도 그 줄의 수에 더한다(`_chain_weight`).

    Args:
        text: 소스.

    Returns:
        가장 긴 논리 줄의 사슬 토큰 수(총수가 상한 이하면 사슬 문자 총수).
    """
    marks = _count_marks(text)
    if marks <= MAX_CHAIN_TOKENS:
        return marks
    longest = _logical_line_chain(text, MAX_CHAIN_TOKENS, partial=True)
    return 0 if longest is None else longest


def _logical_line_chain(text: str, limit: int, *, partial: bool = False) -> int | None:
    """논리 줄 하나에 든 사슬 토큰 수의 최댓값을 토큰으로 나눠 센다. `limit`을 넘는 줄을 만나면 바로 멈춘다.

    Args:
        text: 소스.
        limit: 이 수를 넘으면 더 세지 않는다.
        partial: 토큰으로 나눌 수 없을 때 센 데까지 돌려줄지(아니면 None).

    Returns:
        가장 긴 논리 줄의 사슬 토큰 수(`limit`을 넘으면 넘은 수), 나눌 수 없으면 `partial`에 따라 센 데까지나 None.
    """
    longest = count = 0
    try:
        for token in tokenize.generate_tokens(_parser_lines(text)):
            if token.type == tokenize.NEWLINE:
                count = 0
                continue
            count += _chain_weight(token)
            longest = max(longest, count)
            if longest > limit:
                return longest
    except (tokenize.TokenError, SyntaxError):
        # 토큰으로 나눌 수 없는 소스는 파서도 문법 오류로 거부해 트리를 만들지 않으므로 센 데까지만 볼 수 있다.
        return longest if partial else None
    return longest


def _parser_lines(text: str) -> Callable[[], str]:
    """파서와 같은 줄로 소스를 내는 `readline`을 돌려준다.

    3.10·3.11의 C 토크나이저는 문자열 소스의 `\\r\\n`과 홀로 선 `\\r`를 `\\n`으로 바꾼 뒤 읽는다. 순수 파이썬
    토크나이저에 `io.StringIO(text).readline`을 그대로 주면 `\\n`에서만 줄이 나뉘어, CR만 쓰는 파일 전체가 한 논리 줄이
    된다(4차 리뷰 N1). 열 위치는 쓰지 않으므로 바꿔도 된다.

    Args:
        text: 소스.

    Returns:
        줄마다 부를 함수.
    """
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return io.StringIO(text).readline


def _may_nest_fstrings(text: str) -> bool:
    """3.10·3.11에서 f-string 치환 필드의 식 안에 다른 f-string이 있을 수 있는지 본다(`_fstring_nests`).

    3.12 전 토크나이저는 f-string 전체를 STRING 토큰 하나로 낸다. 접두사에 `f`가 든 토큰마다 치환 필드를 훑는다.
    토큰으로 나눌 수 없으면 판단할 수 없어 True다(그런 소스는 대개 문법 오류지만, 순수 파이썬 토크나이저가 거부한 지점
    뒤를 C 토크나이저가 받아들이지 않는다고 확인하지 않았다).

    Args:
        text: 소스.

    Returns:
        겹친 f-string이 있을 수 있으면 True.
    """
    try:
        for token in tokenize.generate_tokens(_parser_lines(text)):
            prefix = _string_prefix(token.string) if token.type == tokenize.STRING else ""
            if "f" in prefix.lower() and _fstring_nests(token.string):
                return True
    except (tokenize.TokenError, SyntaxError):
        return True
    return False


def _fstring_nests(literal: str) -> bool:
    """f-string 리터럴 토큰 하나의 치환 필드 식 안에 f-string이 있을 수 있는지 본다(3.10·3.11 파서가 셋 이상 겹친다).

    3.10·3.11 `Parser/string_parser.c`(`fstring_find_literal`·`fstring_find_expr`)가 토큰을 훑는 순서를 그대로 따른다:
    리터럴 부분(`{{`·`}}`, 원시가 아니면 `\\N{…}`와 백슬래시 짝)은 건너뛰고, 치환 필드의 식에서 따옴표가 여는 문자열
    리터럴에 f 접두사가 있는지 본다(`_opens_fstring`). 형식 지정 안의 필드(`{x:{y}}`)도 같은 식으로 본다. 형식 지정
    필드는 파서를 차례로 만들 뿐 겹치지 않으므로 그것만으로는 겹친 것이 아니다. CPython이 문법 오류로 멈추는 필드를
    만나면 그 필드부터 끝까지 f-string 시작(`_FSTRING_START`)이 있는지로 넉넉히 판단한다.

    Args:
        literal: STRING 토큰 문자열(접두사에 `f`가 있다).

    Returns:
        겹친 f-string이 있을 수 있으면 True.
    """
    prefix = _string_prefix(literal)
    quote = literal[len(prefix)]
    width = 3 if literal.startswith(quote * 3, len(prefix)) and len(literal) >= len(prefix) + 6 else 1
    body = literal[len(prefix) + width : len(literal) - width]
    raw = "r" in prefix.lower()
    index = 0
    while True:
        found = _fstring_literal_end(body, index, raw, 0)
        if found is None:
            return _FSTRING_START.search(body, index) is not None
        if found >= len(body):
            return False
        nests, after = _fstring_field_nests(body, found, raw, 0)
        if nests:
            return True
        if after is None:
            return _FSTRING_START.search(body, found) is not None
        index = after


def _fstring_literal_end(body: str, start: int, raw: bool, level: int) -> int | None:
    """f-string 리터럴 부분을 `start`부터 훑어 치환 필드를 여는 `{`나 형식 지정을 닫는 `}`의 위치를 돌려준다.

    `fstring_find_literal`과 같다: 원시가 아니면 백슬래시는 다음 글자와 짝이고(`\\{`의 `{`는 그대로 괄호다), `\\N{…}`의
    괄호는 필드가 아니다. 맨 바깥(`level` 0)에서만 `{{`·`}}`가 글자이고 홀로 선 `}`는 문법 오류다.

    Args:
        body: 따옴표 안 글자.
        start: 훑기 시작할 위치.
        raw: 원시 문자열인지.
        level: 형식 지정 중첩 수준(맨 바깥 0).

    Returns:
        `{`나 `}`의 위치, 끝까지 없으면 `len(body)`, 문법 오류면 None.
    """
    index = start
    end = len(body)
    while index < end:
        char = body[index]
        index += 1
        if not raw and char == "\\" and index < end:
            char = body[index]
            index += 1
            if char == "N":
                if index < end:
                    index += 1
                    if body[index - 1] == "{":
                        while index < end:
                            index += 1
                            if body[index - 1] == "}":
                                break
                continue
        if char in "{}":
            if level == 0:
                if index < end and body[index] == char:
                    index += 1
                    continue
                if char == "}":
                    return None
            return index - 1
    return end


def _fstring_field_nests(body: str, start: int, raw: bool, level: int) -> tuple[bool, int | None]:
    """`body[start]`의 `{`로 시작하는 치환 필드를 `fstring_find_expr`처럼 훑는다.

    식(`_fstring_expression_end`)이 끝나면 `=`, 변환(`!r`), 형식 지정(`:` 뒤, `_fstring_format_spec_end`)을 지나 `}`에
    닿는다. 두 단계 넘는 형식 지정 필드와 잘못된 변환은 CPython이 문법 오류로 멈춘다.

    Args:
        body: 따옴표 안 글자.
        start: `{`의 위치.
        raw: 원시 문자열인지.
        level: 형식 지정 중첩 수준.

    Returns:
        (식 안에 f-string이 있을 수 있는지, 필드 다음 위치 — 문법 오류면 None).
    """
    if level >= 2:
        return False, None
    nests, index = _fstring_expression_end(body, start + 1)
    if nests or index is None:
        return nests, None
    end = len(body)
    if body[index] == "=":
        index += 1
        while index < end and body[index] in " \t\n\r\x0b\x0c":
            index += 1
        if index >= end:
            return False, None
    if body[index] == "!":
        if index + 1 >= end or body[index + 1] not in "sra":
            return False, None
        index += 2
    if index < end and body[index] == ":":
        nests, found = _fstring_format_spec_end(body, index + 1, raw, level + 1)
        if nests or found is None:
            return nests, None
        index = found
    if index >= end or body[index] != "}":
        return False, None
    return False, index + 1


def _fstring_expression_end(body: str, start: int) -> tuple[bool, int | None]:
    """치환 필드의 식을 `start`부터 훑어 식을 끝낸 글자의 위치를 찾는다(`fstring_find_expr`의 첫 반복문).

    식은 괄호 밖의 `!`·`:`·`}`·`=`(`!=`·`==`·`<=`·`>=` 제외)에서 끝나고, 그 안의 문자열 리터럴은 같은 따옴표(세 겹이면
    세 겹)까지 건너뛴다. 백슬래시·`#`·짝이 맞지 않는 괄호·닫지 않은 문자열은 CPython이 식을 파싱하기 전에 문법 오류로
    멈춘다.

    Args:
        body: 따옴표 안 글자.
        start: 식이 시작하는 위치(`{` 다음).

    Returns:
        (식 안에 f-string이 있을 수 있는지, 식을 끝낸 글자의 위치 — 문법 오류면 None).
    """
    end = len(body)
    index = start
    quote = ""
    triple = False
    depth = 0
    while index < end:
        char = body[index]
        if char == "\\":
            return False, None
        if quote:
            if char == quote:
                if not triple:
                    quote = ""
                elif body.startswith(char * 3, index):
                    index += 2
                    quote = ""
        elif char in "'\"":
            if _opens_fstring(body, start, index):
                return True, None
            triple = body.startswith(char * 3, index)
            index += 2 if triple else 0
            quote = char
        elif char in "[{(":
            depth += 1
        elif char == "#":
            return False, None
        elif depth == 0 and char in "!:}=<>":
            following = body[index + 1] if index + 1 < end else ""
            if following == "=" and char in "!=<>":
                index += 1
            elif not (following and char in "<>"):
                break
        elif char in "])}":
            if depth == 0:
                return False, None
            depth -= 1
        index += 1
    if quote or depth or index >= end:
        return False, None
    return False, index


def _fstring_format_spec_end(body: str, start: int, raw: bool, level: int) -> tuple[bool, int | None]:
    """형식 지정을 `start`부터 훑어 그것을 닫는 `}`의 위치를 찾는다(리터럴과 수준 `level`의 필드가 번갈아 온다).

    Args:
        body: 따옴표 안 글자.
        start: 형식 지정이 시작하는 위치(`:` 다음).
        raw: 원시 문자열인지.
        level: 형식 지정 안 필드의 수준.

    Returns:
        (필드의 식 안에 f-string이 있을 수 있는지, 닫는 `}`의 위치 — 문법 오류면 None).
    """
    index = start
    while True:
        found = _fstring_literal_end(body, index, raw, level)
        if found is None or found >= len(body):
            return False, None
        if body[found] == "}":
            return False, found
        nests, after = _fstring_field_nests(body, found, raw, level)
        if nests or after is None:
            return nests, None
        index = after


def _opens_fstring(text: str, start: int, quote: int) -> bool:
    """치환 필드의 식(`text[start:]`)에서 `text[quote]`의 따옴표가 여는 문자열 리터럴에 f 접두사가 있을 수 있는지 본다.

    C 토크나이저는 이름 토큰의 첫 글자부터만 접두사(`b`·`r`·`u`·`f` 조합)를 읽는다. 따옴표 앞의 접두사 글자에 `f`가
    있고 그 앞이 이름 글자(글자·`_`)가 아니면 f-string이 시작한다고 본다. 숫자 뒤(`1f'…'`, 숫자 토큰 뒤에 새 토큰이
    시작할 수 있다)나 판단하기 어려운 글자 뒤도 f-string으로 본다. `if'a'`·`elif"…"`처럼 이름 안의 `f`는 아니다.

    Args:
        text: 따옴표 안 글자.
        start: 식이 시작하는 위치.
        quote: 따옴표의 위치.

    Returns:
        f 접두사가 있을 수 있으면 True.
    """
    index = quote
    while index > start and text[index - 1] in "bBrRuUfF":
        index -= 1
    if "f" not in text[index:quote].lower():
        return False
    before = text[index - 1] if index > start else ""
    return not (before == "_" or before.isalpha())


def _chain_weight(token: tokenize.TokenInfo) -> int:
    """토큰 하나가 논리 줄의 사슬에 더하는 수를 정한다.

    사슬 연산자 토큰은 1이다. 3.10의 tokenize는 f-string 전체를 STRING 토큰 하나로 내지만, 파서는 치환 필드의 식을
    파싱해 그 자리 트리에 단다. 연산자 토큰만 세면 `f"{a+a+…}"`가 상한을 그대로 지나 파서가 프로세스를 죽인다. 그래서
    f-string 토큰은 안에 든 사슬 문자 수를 더한다. 리터럴 부분과 중첩 f-string의 문자까지 세지만, 상한 판정에서 더 세는
    쪽은 안전하다(3.12부터는 치환 필드의 연산자가 따로 OP 토큰으로 나온다).

    Args:
        token: 토큰.

    Returns:
        더할 사슬 토큰 수.
    """
    if token.type == tokenize.OP:
        return 1 if token.string in _CHAIN_OPERATORS else 0
    if token.type == tokenize.STRING and "f" in _string_prefix(token.string).lower():
        return _count_marks(token.string)
    return 0


def _string_prefix(literal: str) -> str:
    """문자열 토큰의 접두사(`f`·`rb` 등, 따옴표 앞 글자)를 돌려준다.

    Args:
        literal: STRING 토큰 문자열.

    Returns:
        접두사(없으면 빈 문자열).
    """
    return literal[: len(literal) - len(literal.lstrip("bBfFrRuU"))]


def _count_marks(text: str) -> int:
    """사슬 문자(`_CHAIN_MARKS`)의 총수를 센다. 사슬 연산자 토큰마다 적어도 하나씩 들어 있어 토큰 수의 상한이다.

    Args:
        text: 소스나 토큰 문자열.

    Returns:
        사슬 문자 수.
    """
    return sum(text.count(mark) for mark in _CHAIN_MARKS)
