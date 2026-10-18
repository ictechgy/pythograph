"""교환 형식이 식별자에 금지하는 문자 판정(모든 계층이 함께 쓰는 한 벌).

isthmus 계약(GRAPH-EXCHANGE의 `location`·식별자 규칙)은 프로젝트 경로·위치 경로·채널·메서드·심볼 이름·서비스 같은
식별자에 C0·C1 제어 문자(NEL U+0085 포함), U+2028/U+2029, 짝 없는 서러게이트(U+D800~U+DFFF)를 금지하고, 소비자는
이를 어긴 문서를 통째로 거부한다. 서러게이트는 UTF-8로 쓸 수도 없어 출력 쓰기부터 실패한다. 같은 판정을 여러 곳에
따로 두면 서로 어긋나므로(서러게이트를 빠뜨린 사본이 있었다) 다른 패키지를 import하지 않는 맨 아래 계층인 여기에만
두고 cli·graph·routes·exchange·source가 함께 쓴다.
"""

from __future__ import annotations

import re

#: 계약이 식별자에 금지하는 문자다(C0·C1 제어 문자, U+2028/2029, UTF-8로 쓸 수 없는 짝 없는 서러게이트).
FORBIDDEN_CHARACTERS = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]")


def is_contract_safe(text: str) -> bool:
    """문자열을 교환 형식의 식별자로 실을 수 있는지(비어 있지 않고 금지 문자가 없는지) 판정한다.

    Args:
        text: 검사할 문자열.

    Returns:
        실을 수 있으면 True.
    """
    return bool(text) and FORBIDDEN_CHARACTERS.search(text) is None
