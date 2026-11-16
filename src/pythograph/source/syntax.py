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
import queue
import re
import sys
import threading
import tokenize
from collections.abc import Callable
from typing import Literal, TypeVar, cast

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
#: 깊이를 MAXSTACK(약 6,000단) 수로만 막아, 그 한도 안의 식도 작은 스택(1 MiB 스레드, 2 MB인 Windows 주 스레드)을
#: 넘길 수 있다. 실측(Linux): 3.13.18은 `~` 5,967개 식 하나에 1,280 KiB가 들고, macOS 3.13(arm64)은 1 MiB 스레드에서
#: 그보다 짧은 겹친 f-string 식으로 죽었다(SIGBUS).
_PARSER_CHECKS_C_STACK = sys.version_info >= (3, 14)

#: 3.13까지 부른 스레드에서 바로 파싱하는 최대 길이(문자)다. 넘는 소스는 전용 스택에서 파싱한다(3.10·3.11에서
#: f-string이 있을 수 있는 소스는 길이와 무관하게 전용 스택이다). 실측(3.10.20·3.11.18·3.12.3·3.13.18 Linux, 스레드
#: 스택 16 KiB 단위 이분 탐색, 괄호·호출·첨자·단항·`not`·`lambda`·조건식·`**`·내포·풀어 쓰기·대입식 등 18가지 모양):
#: 가장 비싼 글자는 괄호(`(`·`[`·`{` 하나가 문법 규칙 여럿을 지나 약 1.0–1.1 KiB)다. 256자 괄호 125단 식 하나가
#: 최악으로 3.10·3.13 272 KiB, 3.11·3.12 240 KiB이고, 같은 길이를 f-string 4단(파서 다섯)에 나눠 겹친 괄호도 256 KiB
#: 이하다. 최악의 2배(544 KiB)도 1 MiB 스레드에 들어간다. 프레임이 다른 macOS·MSVC 빌드에서는 임계 이하 최악 식을
#: 512 KiB 스레드에서 읽는 테스트가 이 여유를 확인한다.
INLINE_PARSE_CHARACTERS = 256

#: 3.10·3.11에서 f-string이 있을 수 있는 소스를 파싱하는 전용 스레드에 더하는 스택(바이트)이다. 3.10·3.11의 f-string은
#: 따옴표 종류 넷(`"""`·`'''`·`"`·`'`)으로 4단까지 겹치고 파서마다 깊이 한도와 괄호 200단 한도를 다시 세므로 파서가 최대
#: 다섯 쌓인다. Linux 실측: 파서마다 한도 가까이 오른쪽 재귀(`~`·`-`·`not`·조건식·`lambda`·`**`)를 두면 3.10.20 최대
#: 4,672 KiB, 3.11.18 최대 3,712 KiB, 괄호 199단씩이면 3.10.20 2,032 KiB(2,319자). 16 MiB는 최악의 3.5배다(MSVC 프레임과
#: 호출자 프레임 여유). 3.10의 `ast2obj`는 파싱이 끝난 뒤 따로 돌아(합 약 3만 단, 약 1.9 MiB) 더하지 않는다.
FSTRING_STACK_ALLOWANCE_BYTES = 16 * 1024 * 1024

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
    전용 스레드에서 한다.

    3.13까지는 `INLINE_PARSE_CHARACTERS`보다 긴 소스를, 3.10에서는 사슬 추정치 `bound`(`chain_bound`,
    `MAX_CHAIN_TOKENS` 이하로 확인한 값)가 `SMALL_STACK_CHAIN_TOKENS`를 넘는 소스를, 3.10·3.11에서는 f-string이 있을 수
    있는 소스를 전용 스레드에서 다룬다(`guarded_stack_bytes`). 3.14부터는 파서가 스택 경계를 직접 보므로 긴 사슬이
    아니면 부른 스레드에서 다룬다. 상한을 넘는 트리는 파싱한 스레드에서 버리고 이유만 돌려준다: 3.13은 깊은 트리를
    해제하는 재귀를 trashcan이 약 1만 단까지 미루지 않아, 깊은 트리를 작은 스택에서 버리면 해제하다 죽는다. 결과와
    예외는 같은 스레드에서 한 것과 같다.

    Args:
        text: 소스.
        filename: 파서에 줄 파일 이름.
        mode: `exec`(모듈)나 `eval`(식).
        bound: 사슬 추정치.
        depth_limit: 허용하는 트리 깊이(`tree_overflow`).
        node_limit: 허용하는 트리 노드 수(`tree_overflow`).

    Returns:
        (트리, None), 상한을 넘으면 (None, `"depth"`나 `"nodes"`).

    Raises:
        SyntaxError: 문법 오류.
        ValueError: NUL 등 파서가 받지 않는 입력.
        RecursionError: 파서의 깊이 한도.
        MemoryError: 파서의 메모리 부족·깊이 한도.
        ParseStackUnavailableError: 전용 스레드를 만들 수 없을 때.
    """

    def parse_and_measure() -> tuple[ast.AST | None, Literal["depth", "nodes"] | None]:
        """파싱하고 상한을 잰다. 넘는 트리는 이 스레드에서 해제되도록 돌려주지 않는다."""
        tree = ast.parse(text, filename=filename, mode=mode)
        overflow = tree_overflow(tree, depth_limit, node_limit)
        return (None, overflow) if overflow is not None else (tree, None)

    stack = guarded_stack_bytes(text, bound)
    if stack == 0:
        return parse_and_measure()
    return _run_on_large_stack(parse_and_measure, stack)


def guarded_stack_bytes(text: str, bound: int) -> int:
    """소스를 파싱할 전용 스레드의 스택 크기를 돌려준다. 부른 스레드에서 파싱해도 되면 0이다.

    3.13까지 `INLINE_PARSE_CHARACTERS`보다 긴 소스, 3.10의 긴 사슬, 3.10·3.11에서 f-string이 있을 수 있는 소스(길이와
    무관, 델타 재리뷰 S1: 파서가 f-string마다 한도를 다시 세어 글자 수로 상한을 잡기 어렵다)가 전용 스레드로 간다.
    기본은 `MIN_PARSE_STACK_BYTES`이고, 3.10의 긴 사슬은 토큰에 비례한 크기(`parse_stack_bytes`)다. f-string이 있을
    수 있으면 겹친 파서 몫(`FSTRING_STACK_ALLOWANCE_BYTES`)을 더한다. 모두 1 MiB의 배수라 합도 플랫폼이 받는 크기다.

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
    stack = chains or MIN_PARSE_STACK_BYTES
    return stack + FSTRING_STACK_ALLOWANCE_BYTES if fstrings else stack


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
    wanted = max(MIN_PARSE_STACK_BYTES, bound * PARSE_STACK_BYTES_PER_TOKEN)
    alignment_units = -(-wanted // PARSE_STACK_ALIGNMENT_BYTES)
    return alignment_units * PARSE_STACK_ALIGNMENT_BYTES


def _run_on_large_stack(work: Callable[[], _Result], stack: int) -> _Result:
    """적어도 주어진 크기의 스택을 가진 스레드에서 `work`를 돌리고 결과나 예외를 부른 스레드로 넘긴다.

    하한(`MIN_PARSE_STACK_BYTES`)과 거기에 f-string 몫을 더한 크기까지는 그 크기 중 요청 이상인 가장 작은 것의 스레드를
    하나씩 만들어 다시 쓴다(`_ParseWorker`). 3.13까지 256자를 넘는 소스를 모두 여기서 파싱하므로 파싱마다 스레드를
    만들면 시작·종료와 스케줄링 대기가 긴 파일마다 생긴다(4코어 실측: 파일 하나에 CPU 약 0.2–0.6 ms, 재사용하면 약
    0.04–0.2 ms. 부하가 크면 벽시계 시간은 그 몇 배다). 그보다 큰 스택(3.10의 아주 긴 사슬)은 파싱마다 스레드를 만들고
    끝나면 버린다. 작업 스레드에서 난 예외는
    `BaseException`까지 담아 부른 스레드에서 그대로 다시 낸다. `work`가 다시 이 함수를 부르면 안 된다(재사용하는
    스레드가 자기 작업을 기다리게 된다).

    Args:
        work: 돌릴 작업.
        stack: 스레드 스택 크기의 하한(바이트, `PARSE_STACK_ALIGNMENT_BYTES`의 배수).

    Returns:
        작업의 결과.

    Raises:
        ParseStackUnavailableError: 그 크기의 스레드를 만들 수 없을 때.
    """
    reused = next((size for size in _reused_stack_sizes() if stack <= size), 0)
    if reused:
        result = _parse_workers.get(reused).run(work)
    else:
        outcome: list[_Result | BaseException] = []

        def run() -> None:
            """작업을 돌리고 결과나 예외를 담는다."""
            try:
                outcome.append(work())
            except BaseException as error:  # 부른 스레드에서 같은 예외로 다시 낸다(아래 raise).
                outcome.append(error)

        _start_parse_thread(run, stack).join()
        if not outcome:  # 작업 함수는 결과나 예외를 언제나 담는다(담기마저 실패한 경우만 남는다).
            raise AssertionError("the parser thread ended without recording a result or an exception")
        result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _reused_stack_sizes() -> tuple[int, int]:
    """스레드를 다시 쓰는 스택 크기(작은 것부터)다: 하한과, 하한에 f-string 몫을 더한 것(1 MiB 배수로 올림)."""
    sizes = (MIN_PARSE_STACK_BYTES, MIN_PARSE_STACK_BYTES + FSTRING_STACK_ALLOWANCE_BYTES)
    first, second = (-(-size // PARSE_STACK_ALIGNMENT_BYTES) * PARSE_STACK_ALIGNMENT_BYTES for size in sizes)
    return first, second


def _start_parse_thread(target: Callable[[], None], stack: int) -> threading.Thread:
    """주어진 크기의 스택을 가진 데몬 스레드에서 `target`을 시작한다.

    `threading.stack_size`는 프로세스(인터프리터) 전체 설정이라 스레드를 시작한 직후 원래 값으로 되돌린다. 그 사이에
    다른 스레드가 스레드를 만들면 그 스레드도 이 크기를 받는다. pythograph CLI는 파싱하는 동안 다른 스레드를 만들지
    않지만, pythograph를 라이브러리로 불러 여러 스레드에서 동시에 쓰면 그런 겹침이 생길 수 있다(재사용하는 스레드는
    크기마다 한 번만 만든다).

    Args:
        target: 스레드에서 돌릴 함수.
        stack: 스레드 스택 크기(바이트).

    Returns:
        시작한 스레드.

    Raises:
        ParseStackUnavailableError: 그 크기의 스레드를 만들 수 없을 때.
    """
    previous = threading.stack_size()
    try:
        threading.stack_size(stack)
        thread = threading.Thread(target=target, name="pythograph-parse", daemon=True)
        thread.start()
    except (RuntimeError, ValueError) as error:
        raise ParseStackUnavailableError(f"could not start a {stack}-byte parser thread") from error
    finally:
        threading.stack_size(previous)
    return thread


class _ParseWorker:
    """큰 스택을 가진 파싱 전용 스레드 하나다. 넘겨받은 작업을 차례로 돌리고 결과나 예외를 돌려준다.

    스레드는 프로세스가 끝날 때까지 남는다(데몬, 작업이 없으면 큐에서 기다린다). 작업의 예외는 담아서 돌려주므로 어떤
    작업 뒤에도 스레드는 살아 있다.
    """

    def __init__(self, stack: int) -> None:
        """스레드를 시작한다.

        Args:
            stack: 스레드 스택 크기(바이트).

        Raises:
            ParseStackUnavailableError: 그 크기의 스레드를 만들 수 없을 때.
        """
        self.stack = stack
        self._jobs: queue.SimpleQueue[tuple[Callable[[], object], queue.SimpleQueue[object]]] = queue.SimpleQueue()
        self._thread = _start_parse_thread(self._serve, stack)

    def alive(self) -> bool:
        """스레드가 살아 있는지 돌려준다(fork한 자식에서는 부모의 스레드가 없어 False다)."""
        return self._thread.is_alive()

    def run(self, work: Callable[[], _Result]) -> _Result | BaseException:
        """작업을 넘기고 끝날 때까지 기다린다.

        Args:
            work: 돌릴 작업.

        Returns:
            작업의 결과, 또는 작업이 낸 예외.
        """
        reply: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._jobs.put((work, reply))
        return cast("_Result | BaseException", reply.get())

    def _serve(self) -> None:
        """작업을 하나씩 받아 돌린다. 끝난 작업과 결과는 다음 작업을 기다리기 전에 놓는다."""
        while True:
            work, reply = self._jobs.get()
            try:
                reply.put(work())
            except BaseException as error:  # 부른 스레드에서 같은 예외로 다시 낸다(`_run_on_large_stack`).
                reply.put(error)
            del work, reply


class _ParseWorkers:
    """스택 크기마다 하나씩 둔 재사용 파싱 스레드(`_ParseWorker`)다. 여러 스레드에서 함께 불러도 된다."""

    def __init__(self) -> None:
        """비어 있는 상태로 만든다."""
        self._lock = threading.Lock()
        self._workers: dict[int, _ParseWorker] = {}

    def get(self, stack: int) -> _ParseWorker:
        """그 크기의 살아 있는 스레드를 돌려주고, 없으면 만든다.

        Args:
            stack: 스레드 스택 크기(바이트).

        Returns:
            파싱 스레드.

        Raises:
            ParseStackUnavailableError: 스레드를 새로 만들 수 없을 때(다음 요청에서 다시 시도한다).
        """
        with self._lock:
            worker = self._workers.get(stack)
            if worker is None or not worker.alive():
                worker = self._workers[stack] = _ParseWorker(stack)
            return worker

    def forget(self) -> None:
        """fork한 자식에서 부모의 스레드를 잊는다. 그 스레드는 자식에 없고, 잠금은 잠긴 채 복사됐을 수 있다."""
        self._lock = threading.Lock()
        self._workers = {}


_parse_workers = _ParseWorkers()


def _forget_parse_workers_after_fork() -> None:
    """fork한 자식이 부모의 파싱 스레드에 작업을 넘겨 영원히 기다리지 않게 한다."""
    _parse_workers.forget()


if hasattr(os, "register_at_fork"):  # Windows에는 fork가 없다.
    os.register_at_fork(after_in_child=_forget_parse_workers_after_fork)


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
    longest = count = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NEWLINE:
                count = 0
                continue
            count += _chain_weight(token)
            longest = max(longest, count)
            if longest > MAX_CHAIN_TOKENS:
                return longest
    except (tokenize.TokenError, SyntaxError):
        # 토큰으로 나눌 수 없는 소스는 파서도 문법 오류로 거부해 트리를 만들지 않으므로 센 데까지만 본다.
        return longest
    return longest


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
