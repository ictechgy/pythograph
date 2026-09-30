"""URL 조립 규칙: 조각(리터럴·값·query 꼬리) → 정규 경로 템플릿.

정본은 isthmus `docs/HTTP-WRAPPERS.md`의 "공통 해석 규칙"과 url-compose 벡터다. 이 모듈은 파일 시스템도 구문
트리도 보지 않는 순수 함수만 둔다. 구문 트리에서 조각을 만드는 일은 `parts.py`, 라이브러리별 결합 방식 선택은
`libraries.py`가 맡는다. 순서:

1. `split_url`: 조각 앞머리를 절대 URL(scheme·authority)·앞 값(base 식)·상대 경로로 나눈다(`compose.strip`).
2. `compose_path`: 경로 조각에 query 꼬리·세그먼트 보간 규칙을 적용해 원문 템플릿을 만든다
   (`compose.query-tail`·`compose.suffix`·`compose.interpolation`).
3. `join_path`: 라이브러리 결합 방식으로 base와 잇는다(`compose.base-join`).
4. `finish`: 정규화(`compose.normalize`)·점 세그먼트 제거·마스킹(`compose.mask`)으로 사실 필드를 만든다.

값 자리는 내부 표식 `PLACEHOLDER`로 들고 다니다가 정규화 뒤에 `{}`로 바꾼다. 리터럴 중괄호(`/tpl/{x}`)와
섞이지 않게 하기 위해서다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import unquote

from pythograph.exchange.template import normalize_uri_path, template_problem

#: 세그먼트 전체를 채운 값 자리의 내부 표식이다(정규화 뒤 `{}`가 된다).
PLACEHOLDER = "\x00"

#: HTTP 계약 동사다.
HTTP_VERBS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})

#: 절대 URL 앞머리(scheme://)다. 파이썬 클라이언트가 보내는 scheme은 http·https뿐이다.
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://")

#: authority로 받는 모양(소문자 host[:port], IPv6 `[...]`)이다. 계약의 authority 검사와 같은 문자만 받는다.
_AUTHORITY = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9\-._~%!$&'()*+,;=]+)(:[0-9]*)?")

#: 경로 세그먼트를 모두 가리는 웹훅 host다.
_WEBHOOK_ALL = frozenset({"hooks.slack.com"})

#: `/api/webhooks` 뒤 세그먼트를 가리는 웹훅 host다.
_WEBHOOK_AFTER_PREFIX = frozenset({"discord.com", "discordapp.com"})

#: 고엔트로피로 보는 최소 세그먼트 길이다.
_MASK_MIN_LENGTH = 16

#: 결합 방식 이름: RFC 3986 상대 해석(aiohttp `base_url`, `urllib.parse.urljoin`).
JOIN_RFC3986 = "rfc3986"
#: 결합 방식 이름: httpx `Client(base_url)`의 `_merge_url`.
JOIN_HTTPX = "httpx-merge"
#: 결합 방식 이름: axios류 슬래시 결합(계약 벡터 실행용).
JOIN_SLASH = "slash-join"
#: 결합 방식 이름: dio 단순 연결(계약 벡터 실행용).
JOIN_DIO = "dio-concat"
#: 결합 방식 이름: base 없이 문자열로 만든 전체 URL(requests·httpx·aiohttp 최상위 호출, urllib).
JOIN_STRING = "string-concat"


@dataclass(frozen=True)
class Literal:
    """리터럴 문자열 조각(상수를 따라가 얻은 값 포함)."""

    text: str


@dataclass(frozen=True)
class Value:
    """값을 모르는 조각.

    Attributes:
        parameter: 감싼 함수의 매개변수를 그대로 쓴 조각이면 그 이름(래퍼 싱크 판정용). 아니면 None.
    """

    parameter: str | None = None


@dataclass(frozen=True)
class QueryTail:
    """query 꼬리임을 증명한 끝 조각(값이 빈 문자열이거나 `?`로 시작하는 지역 변수)."""


#: URL 조각 타입이다.
Part = Literal | Value | QueryTail


@dataclass(frozen=True)
class UrlSplit:
    """URL 앞머리 판정.

    Attributes:
        kind: `absolute`(scheme과 host 리터럴), `dynamic-host`(scheme 뒤 host가 값), `base-value`(앞이 값),
            `relative`(scheme 없는 리터럴로 시작), `dynamic`(해석 불가).
        authority: `absolute`의 소문자 authority(userinfo 제거). 계약 모양이 아니면 None.
        path: 경로 조각(`absolute`·`dynamic-host`는 host 뒤, `base-value`는 앞 값 뒤, `relative`는 전체).
        leading: `base-value`의 앞 값.
        empty_path: `absolute`에서 host 뒤에 경로·query가 전혀 없었는지(`https://h`). base 결합이 `https://h`와
            `https://h/`를 다르게 다루는 방식(dio)이 있어 구분한다.
    """

    kind: str
    authority: str | None = None
    path: tuple[Part, ...] = ()
    leading: Value | None = None
    empty_path: bool = False


@dataclass(frozen=True)
class PathResult:
    """경로 조각을 조립한 원문 템플릿.

    Attributes:
        raw: 정규화 전 경로(값 자리는 `PLACEHOLDER`). dynamic이면 None.
        prefix: dynamic일 때 문제 되는 첫 조각 앞까지의 원문(없으면 None).
        query_tail_stripped: query·fragment 꼬리를 뗐는지.
    """

    raw: str | None
    prefix: str | None = None
    query_tail_stripped: bool = False

    @property
    def dynamic(self) -> bool:
        """경로를 확정하지 못했는지 돌려준다.

        Returns:
            dynamic이면 True.
        """
        return self.raw is None


@dataclass(frozen=True)
class BaseUrl:
    """클라이언트 base URL의 판정.

    Attributes:
        known: base 경로를 확정했는지(리터럴 절대 URL, 또는 host만 값인 URL의 경로).
        path: 확정한 base 경로(`/`로 시작). host 뒤에 아무것도 없는 URL(`https://h`)이면 빈 문자열이다.
        authority: 리터럴 authority(없으면 None).
        rooted: base 경로가 서버 루트부터인지(host까지 리터럴이면 True, host가 값이면 False).
    """

    known: bool
    path: str = "/"
    authority: str | None = None
    rooted: bool = False


#: base를 모르는 클라이언트다.
UNKNOWN_BASE = BaseUrl(known=False)


@dataclass(frozen=True)
class Joined:
    """결합 결과(정규화 전).

    Attributes:
        raw: 원문 경로(`/`로 시작, 값 자리는 `PLACEHOLDER`). dynamic이면 None.
        anchor: `root` 또는 `base`.
        authority: 리터럴 authority.
        ambiguous: base를 몰라 결합 결과가 모호한지(`ambiguous-base-join:` 대상).
        remove_dots: 라이브러리가 점 세그먼트를 지우는지.
    """

    raw: str | None
    anchor: str
    authority: str | None = None
    ambiguous: bool = False
    remove_dots: bool = False


@dataclass(frozen=True)
class ComposedUrl:
    """route-call 사실의 경로 필드.

    Attributes:
        template: 정규 템플릿. dynamic이면 None.
        anchor: `root` 또는 `base`.
        authority: 리터럴 authority.
        query_tail_stripped: query 꼬리를 뗐는지.
        channel_prefix: dynamic 호출의 증명된 리터럴 접두사 템플릿.
        masked_segments: 마스킹한 세그먼트 수.
        ambiguous: `ambiguous-base-join:` 대상인지.
    """

    template: str | None
    anchor: str
    authority: str | None = None
    query_tail_stripped: bool = False
    channel_prefix: str | None = None
    masked_segments: int = 0
    ambiguous: bool = False

    @property
    def dynamic(self) -> bool:
        """템플릿을 확정하지 못했는지 돌려준다.

        Returns:
            dynamic이면 True.
        """
        return self.template is None


#: 경로를 전혀 모르는 호출이다.
DYNAMIC_URL = ComposedUrl(template=None, anchor="base")


def merge_literals(parts: tuple[Part, ...] | list[Part]) -> tuple[Part, ...]:
    """이웃한 리터럴 조각을 하나로 합친다.

    Args:
        parts: 조각 목록.

    Returns:
        합친 조각 튜플.
    """
    merged: list[Part] = []
    for part in parts:
        if isinstance(part, Literal) and merged and isinstance(merged[-1], Literal):
            merged[-1] = Literal(merged[-1].text + part.text)
        elif not (isinstance(part, Literal) and not part.text):
            merged.append(part)
    return tuple(merged)


def split_url(parts: tuple[Part, ...] | list[Part]) -> UrlSplit:
    """조각 앞머리를 절대 URL·앞 값·상대 경로로 나눈다.

    host 뒤 경로는 첫 `/`부터다. host가 값이면(`f"https://{host}/v1"`) host 뒤 경로를 base 앵커로 쓴다(계약:
    host가 동적이면 base). `//`로 시작하는 리터럴(scheme 상대 참조)은 라이브러리마다 host로 읽는 방식이 달라
    dynamic이다.

    Args:
        parts: 조각 목록.

    Returns:
        판정.
    """
    merged = merge_literals(parts)
    if not merged:
        return UrlSplit("dynamic")
    head = merged[0]
    if isinstance(head, Value):
        return UrlSplit("base-value", path=merged[1:], leading=head)
    if isinstance(head, QueryTail):
        return UrlSplit("dynamic")
    scheme = _SCHEME.match(head.text)
    if scheme is None:
        return UrlSplit("dynamic") if head.text.startswith("//") else UrlSplit("relative", path=merged)
    if scheme.group(1).lower() not in ("http", "https"):
        return UrlSplit("dynamic")
    return _split_absolute(head.text[scheme.end() :], merged[1:])


def _split_absolute(rest: str, tail: tuple[Part, ...]) -> UrlSplit:
    """scheme 뒤 문자열에서 authority와 경로를 나눈다.

    Args:
        rest: 첫 리터럴의 scheme 뒤 부분.
        tail: 나머지 조각.

    Returns:
        판정.
    """
    end = _authority_end(rest)
    if end is None and tail:
        return _dynamic_host(tail)
    raw_authority = rest if end is None else rest[:end]
    path_text = "" if end is None else rest[end:]
    if not path_text.startswith("/"):
        path_text = "/" + path_text
    path = merge_literals([Literal(path_text), *tail])
    return UrlSplit("absolute", authority=_clean_authority(raw_authority), path=path, empty_path=end is None)


def _authority_end(text: str) -> int | None:
    """authority가 끝나는 위치(`/`·`?`·`#`)를 찾는다.

    Args:
        text: scheme 뒤 문자열.

    Returns:
        위치, 없으면 None.
    """
    positions = [index for index in (text.find("/"), text.find("?"), text.find("#")) if index >= 0]
    return min(positions) if positions else None


def _dynamic_host(tail: tuple[Part, ...]) -> UrlSplit:
    """host 자리에 값이 있는 URL에서 host 뒤 경로를 찾는다.

    Args:
        tail: host 조각 뒤의 조각.

    Returns:
        `dynamic-host` 판정(경로를 찾지 못하면 dynamic).
    """
    for index, part in enumerate(tail):
        if not isinstance(part, Literal):
            continue
        end = _authority_end(part.text)
        if end is None:
            continue
        path_text = part.text[end:]
        if not path_text.startswith("/"):
            path_text = "/" + path_text
        return UrlSplit("dynamic-host", path=merge_literals([Literal(path_text), *tail[index + 1 :]]))
    return UrlSplit("dynamic")


def _clean_authority(raw: str) -> str | None:
    """userinfo를 떼고 소문자로 바꾼 authority를 돌려준다. 계약 모양이 아니면 None이다.

    Args:
        raw: scheme 뒤부터 경로 앞까지.

    Returns:
        `host[:port]` 또는 None.
    """
    host = raw.rsplit("@", 1)[-1].lower()
    if host.endswith(":"):
        host = host[:-1]
    return host if host and _AUTHORITY.fullmatch(host) else None


def compose_path(parts: tuple[Part, ...] | list[Part]) -> PathResult:
    """경로 조각을 원문 템플릿으로 조립한다(query 꼬리·세그먼트 보간 규칙).

    - 리터럴의 첫 `?`·`#`부터 끝까지를 떼고 뒤 조각도 버린다(`queryTailStripped`).
    - 증명한 query 꼬리 조각은 마지막일 때만 뗀다. 중간이면 dynamic이다.
    - 값은 앞이 `/`로 끝나고 뒤가 `/`·`?`·`#`·query 꼬리·끝일 때만 세그먼트 전체 값이다. 그 밖은 dynamic이고 그
      앞까지를 접두사로 남긴다. 첫 조각이 값이면 base 식이므로 이 규칙으로는 dynamic이다(`compose.base-join`이 맡는다).

    Args:
        parts: 경로 조각.

    Returns:
        원문 템플릿.
    """
    merged = merge_literals(parts)
    if not merged or not isinstance(merged[0], Literal):
        return PathResult(None)
    output = ""
    for index, part in enumerate(merged):
        if isinstance(part, Literal):
            cut = _query_start(part.text)
            if cut is not None:
                return PathResult(output + part.text[:cut], query_tail_stripped=True)
            output += part.text
        elif isinstance(part, QueryTail):
            if index == len(merged) - 1:
                return PathResult(output, query_tail_stripped=True)
            return PathResult(None, prefix=output)
        elif _whole_segment(output, merged[index + 1] if index + 1 < len(merged) else None):
            output += PLACEHOLDER
        else:
            return PathResult(None, prefix=output)
    return PathResult(output)


def _query_start(text: str) -> int | None:
    """리터럴에서 query·fragment가 시작하는 위치를 찾는다.

    Args:
        text: 리터럴.

    Returns:
        첫 `?`·`#` 위치, 없으면 None.
    """
    positions = [index for index in (text.find("?"), text.find("#")) if index >= 0]
    return min(positions) if positions else None


def _whole_segment(before: str, following: Part | None) -> bool:
    """값 조각이 세그먼트 전체를 채우는지 판정한다.

    Args:
        before: 값 앞까지 조립한 원문.
        following: 값 뒤 조각(없으면 None).

    Returns:
        세그먼트 전체면 True.
    """
    if not before.endswith("/"):
        return False
    if following is None or isinstance(following, QueryTail):
        return True
    return isinstance(following, Literal) and following.text[:1] in ("/", "?", "#")


def join_path(style: str, base: BaseUrl, path: str) -> Joined:
    """상대 경로(원문 템플릿)를 라이브러리 결합 방식으로 base와 잇는다.

    Args:
        style: `JOIN_*` 결합 방식 이름.
        base: base 판정.
        path: 원문 경로(값 자리는 `PLACEHOLDER`).

    Returns:
        결합 결과.

    Raises:
        ValueError: 모르는 결합 방식.
    """
    if path.startswith("//"):
        return Joined(None, "base")
    if style == JOIN_RFC3986:
        return _join_rfc3986(base, path)
    if style == JOIN_HTTPX:
        return _join_httpx(base, path)
    if style == JOIN_SLASH:
        return _join_slash(base, path)
    if style == JOIN_DIO:
        return _join_dio(base, path)
    if style == JOIN_STRING:
        return Joined(None, "base")
    raise ValueError(f"unknown join style {style}")


def _join_rfc3986(base: BaseUrl, path: str) -> Joined:
    """RFC 3986 5.2 상대 해석(yarl `URL.join`, `urllib.parse.urljoin`)이다.

    `/`로 시작하는 경로는 base 경로를 모두 바꾸므로 base를 몰라도 root다. 상대 경로는 base의 마지막 `/`까지에
    붙이고 점 세그먼트를 지운다. base를 모르면 base 앵커이고, 점 세그먼트가 base 경로로 올라갈 수 있으면 dynamic이다.

    Args:
        base: base 판정.
        path: 원문 경로.

    Returns:
        결합 결과.
    """
    if path.startswith("/"):
        return Joined(path, "root", base.authority, remove_dots=True)
    if not base.known:
        return _unknown_relative(path)
    if not path:
        merged = base.path or "/"
    elif not base.path:
        merged = "/" + path
    else:
        merged = base.path[: base.path.rfind("/") + 1] + path
    return Joined(merged, _base_anchor(base), base.authority, remove_dots=True)


def _join_httpx(base: BaseUrl, path: str) -> Joined:
    """httpx `Client._merge_url`: base 끝에 `/`를 보장하고 경로 앞 `/`를 모두 떼어 붙인 뒤 점 세그먼트를 지운다.

    Args:
        base: base 판정.
        path: 원문 경로.

    Returns:
        결합 결과.
    """
    relative = path.lstrip("/")
    if not base.known:
        return _unknown_relative(relative)
    prefix = base.path if base.path.endswith("/") else base.path + "/"
    return Joined(prefix + relative, _base_anchor(base), base.authority, remove_dots=True)


def _join_slash(base: BaseUrl, path: str) -> Joined:
    """axios `combineURLs`: base 끝 `/`(최대 둘)를 떼고 `/` 하나로 경로(앞 `/` 제거)를 잇는다.

    Args:
        base: base 판정.
        path: 원문 경로.

    Returns:
        결합 결과.
    """
    relative = path.lstrip("/")
    if not base.known:
        return Joined("/" + relative, "base")
    prefix = re.sub(r"/?/$", "", base.path)
    return Joined(prefix + "/" + relative, _base_anchor(base), base.authority)


def _join_dio(base: BaseUrl, path: str) -> Joined:
    """dio `RequestOptions.uri`: 문자열 연결 뒤 `//`를 `/`로 줄이고 점 세그먼트를 지운다.

    Args:
        base: base 판정.
        path: 원문 경로.

    Returns:
        결합 결과.
    """
    if not base.known:
        return Joined(path, "base") if path.startswith("/") else Joined(None, "base", ambiguous=True)
    if not base.path and not path.startswith("/"):
        return Joined(None, "base", ambiguous=True)
    joined = (base.path + path).replace("//", "/")
    return Joined(joined, _base_anchor(base), base.authority, remove_dots=True)


def _unknown_relative(path: str) -> Joined:
    """base를 모르는 상대 경로를 base 앵커로 잇는다. 점 세그먼트가 있으면 base로 올라갈 수 있어 dynamic이다.

    Args:
        path: 앞 `/` 없는 원문 경로.

    Returns:
        결합 결과.
    """
    if any(segment in (".", "..") for segment in path.split("/")):
        return Joined(None, "base")
    return Joined("/" + path, "base")


def _base_anchor(base: BaseUrl) -> str:
    """확정한 base 경로 뒤 결과의 앵커를 정한다.

    Args:
        base: base 판정.

    Returns:
        host까지 리터럴이면 `root`, host가 값이면 `base`.
    """
    return "root" if base.rooted else "base"


def base_from_parts(parts: tuple[Part, ...] | list[Part]) -> BaseUrl:
    """base URL 조각에서 base 판정을 만든다.

    절대 URL 리터럴이면 host 뒤 경로(query 제거, 점 세그먼트 제거)와 authority, host가 값인 URL은 경로만 안다.
    경로에 값이 섞이면 base를 모른다.

    Args:
        parts: base URL 식의 조각.

    Returns:
        base 판정.
    """
    split = split_url(parts)
    if split.kind not in ("absolute", "dynamic-host"):
        return UNKNOWN_BASE
    path = compose_path(split.path)
    if path.raw is None or PLACEHOLDER in path.raw:
        return UNKNOWN_BASE
    rooted = split.kind == "absolute"
    base_path = "" if split.empty_path else remove_dot_segments(path.raw)
    return BaseUrl(True, base_path, split.authority if rooted else None, rooted)


def remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4 점 세그먼트 제거다.

    Args:
        path: `/`로 시작하는 경로.

    Returns:
        점 세그먼트를 지운 경로.
    """
    output: list[str] = []
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "..":
            if output:
                output.pop()
            if last:
                output.append("")
        elif segment == ".":
            if last:
                output.append("")
        else:
            output.append(segment)
    return "/" + "/".join(output)


def finish(joined: Joined, query_tail_stripped: bool) -> ComposedUrl:
    """결합 결과를 정규화·마스킹해 사실 필드로 만든다.

    Args:
        joined: 결합 결과(raw가 있어야 한다).
        query_tail_stripped: query 꼬리를 뗐는지.

    Returns:
        사실 필드. 정규 문법을 지키지 못하면 dynamic.
    """
    if joined.raw is None:
        return ComposedUrl(None, joined.anchor, joined.authority, ambiguous=joined.ambiguous)
    template = canonical_template(joined.raw, joined.remove_dots)
    if template is None:
        return ComposedUrl(None, joined.anchor, joined.authority)
    masked, count = mask_template(template, joined.authority)
    return ComposedUrl(masked, joined.anchor, joined.authority, query_tail_stripped, None, count)


def canonical_template(raw: str, remove_dots: bool) -> str | None:
    """원문 경로를 정규 템플릿으로 바꾼다.

    리터럴 부분은 URI 공간 정규화(대문자 hex, unreserved 디코드, pchar 밖 문자 인코딩, 리터럴 중괄호 `%7B`)를
    하고 값 자리를 `{}`로 쓴다. 라이브러리가 점 세그먼트를 지우면 정규화 뒤에 지운다.

    Args:
        raw: `/`로 시작하는 원문 경로.
        remove_dots: 점 세그먼트를 지울지.

    Returns:
        정규 템플릿, 문법을 지키지 못하면 None.
    """
    if not raw.startswith("/"):
        return None
    normalized = "{}".join(normalize_uri_path(chunk) for chunk in raw.split(PLACEHOLDER))
    if remove_dots:
        normalized = remove_dot_segments(normalized)
    return normalized if template_problem(normalized) is None else None


def mask_template(template: str, authority: str | None) -> tuple[str, int]:
    """고엔트로피 세그먼트와 알려진 웹훅 경로를 `{}`로 가린다(`compose.mask`).

    Args:
        template: 정규 템플릿.
        authority: 리터럴 authority(없으면 None).

    Returns:
        (가린 템플릿, 가린 세그먼트 수).
    """
    host = (authority or "").rsplit(":", 1)[0] if authority and not authority.startswith("[") else authority or ""
    segments = template.split("/")
    count = 0
    for index in range(1, len(segments)):
        segment = segments[index]
        if not segment or "{}" in segment:
            continue
        if _masks_segment(host, segments, index):
            segments[index] = "{}"
            count += 1
    return "/".join(segments), count


def _masks_segment(host: str, segments: list[str], index: int) -> bool:
    """세그먼트 하나를 가릴지 정한다.

    Args:
        host: 포트를 뗀 host.
        segments: `/`로 나눈 템플릿(0번은 빈 문자열).
        index: 세그먼트 위치.

    Returns:
        가리면 True.
    """
    if host in _WEBHOOK_ALL:
        return True
    if host in _WEBHOOK_AFTER_PREFIX and segments[1:3] == ["api", "webhooks"] and index >= 3:
        return True
    decoded = unquote(segments[index])
    has_letter = any(character.isascii() and character.isalpha() for character in decoded)
    has_digit = any(character.isascii() and character.isdigit() for character in decoded)
    return len(decoded) >= _MASK_MIN_LENGTH and has_letter and has_digit


def mask_prefix(prefix: str, authority: str | None) -> str:
    """channelPrefix에 route-call 원문과 같은 마스킹을 적용한다.

    Args:
        prefix: 정규 템플릿 접두사.
        authority: 리터럴 authority.

    Returns:
        가린 접두사.
    """
    return mask_template(prefix, authority)[0]


def with_prefix(composed: ComposedUrl, prefix: ComposedUrl | None) -> ComposedUrl:
    """dynamic 호출에 증명된 접두사를 붙인다. 접두사가 없거나 dynamic이면 그대로다.

    Args:
        composed: dynamic 호출의 사실 필드.
        prefix: 접두사를 같은 규칙으로 조립한 결과.

    Returns:
        `channelPrefix`와 앵커를 채운 사실 필드.
    """
    if prefix is None or prefix.template is None:
        return composed
    return replace(composed, channel_prefix=prefix.template, anchor=prefix.anchor, authority=prefix.authority)
