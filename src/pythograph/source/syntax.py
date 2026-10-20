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
import sys
import tokenize
from typing import Literal

#: 실행 중인 파서가 깊은 왼쪽 결합 사슬(`a + a + …`, `x.a.a…`, `f()()…`, `x[0][0]…`)에서 C 스택을 넘기는지다.
#: 3.10의 `ast.parse`는 그런 트리를 파이썬 객체로 바꾸다 프로세스가 신호로 죽고(종료 코드 139), 3.11부터는
#: RecursionError를 낸다.
_PARSER_OVERFLOWS_ON_CHAINS = sys.version_info < (3, 11)

#: 3.10에서 파싱 전에 허용하는 논리 줄 하나의 사슬 토큰(이항 연산자·`.`·`(`·`[`) 수다. 3.10.20 실측: 메서드 사슬
#: 8만 개(트리 깊이 16만)·`+` 15만 항에서 죽고, 메서드 사슬 5만 개·다른 사슬 10만 항은 안전했다. f-string 치환 필드
#: 안의 사슬도 임계가 같고(10만 항 안전, 15만 항에서 죽음), 바깥 사슬과 깊이가 더해진다.
MAX_CHAIN_TOKENS = 50_000

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
    if chain_too_long(text):
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return None if exceeds_depth(tree) else tree.body


def chain_too_long(text: str) -> bool:
    """실행 중인 파서가 C 스택을 넘길 만큼 긴 사슬이 한 논리 줄에 있는지 본다(3.10만 해당).

    파싱 전에 걸러야 하는 이유: 3.10은 파싱 도중 프로세스가 죽어 잡을 예외가 없다. 걸러진 소스는 파서가 거부한 깊은
    소스와 같은 실패로 남긴다. f-string 치환 필드 안의 사슬도 센다. 상한을 넘는 얕은 줄(사슬 토큰이 아주 많은 한 줄짜리
    목록, 사슬 문자가 아주 많은 f-string)도 3.10에서는 함께 걸러진다.

    Args:
        text: 파싱할 소스.

    Returns:
        파싱하면 안 되면 True. 3.11 이상은 파서가 스스로 RecursionError를 내므로 늘 False다.
    """
    return _PARSER_OVERFLOWS_ON_CHAINS and _longest_chain(text) > MAX_CHAIN_TOKENS


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
