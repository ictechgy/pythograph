"""경로 골격·정규식 변환 규칙 테스트."""

from __future__ import annotations

import pytest

from pythograph.exchange.template import encode_decoded_literal, normalize_uri_path
from pythograph.routes.pattern import (
    ExpansionCapped,
    Literal,
    Param,
    Unconvertible,
    capture_group_texts,
    convert_regex,
    dynamic_shape,
    regex_param,
    skeleton_shapes,
)


def _templates(regex: str, *, endpoint: bool = True, fullmatch: bool = False) -> list[tuple[str | None, object]]:
    """정규식 하나를 루트 아래 템플릿으로 바꾼다.

    Args:
        regex: 정규식.
        endpoint: 끝점 여부.
        fullmatch: fullmatch 여부.

    Returns:
        (channel, trailingSlash 또는 dynamic 표시) 목록.
    """
    alternatives = [
        (Literal("/"), *option)
        for option in convert_regex(regex, endpoint=endpoint, anchored_by_fullmatch=fullmatch).alternatives
    ]
    return [
        (shape.channel, "dynamic" if shape.dynamic else shape.trailing_slash)
        for shape in skeleton_shapes(alternatives, raw=regex, trailing_policy="strict")
    ]


def test_literal_and_int_group() -> None:
    """리터럴과 숫자 묶음은 `{}`와 int 제약이 된다."""
    alternatives = [
        (Literal("/"), *option)
        for option in convert_regex(r"^items/(?P<pk>\d+)/$", endpoint=True, anchored_by_fullmatch=True).alternatives
    ]
    shape = skeleton_shapes(alternatives, raw=None, trailing_policy="strict")[0]
    assert shape.channel == "/items/{}/"
    assert [(item.segment, item.kind) for item in shape.constraints] == [(1, "int")]


def test_optional_trailing_slash_merges_to_optional() -> None:
    """끝 `/?`는 `trailingSlash: optional` 하나로 합친다."""
    assert _templates(r"^archive/(?P<slug>[-a-zA-Z0-9_]+)/?$", fullmatch=True) == [("/archive/{}", "optional")]


def test_alternation_and_character_set_expand() -> None:
    """대안과 유한 문자 집합은 템플릿 여러 개로 펼친다(파서가 `v1|v2`를 `v[12]`로 바꿔도 같다)."""
    assert sorted(_templates(r"^(?:v1|v2)/ping/$", fullmatch=True)) == [
        ("/v1/ping/", "strict"),
        ("/v2/ping/", "strict"),
    ]
    assert sorted(_templates(r"^(?P<kind>news|blog)/$", fullmatch=True)) == [("/blog/", "strict"), ("/news/", "strict")]


def test_unanchored_and_open_patterns_are_unconvertible() -> None:
    """시작이 고정되지 않은 search와 끝이 열린 endpoint는 확정할 수 없다."""
    with pytest.raises(Unconvertible):
        convert_regex(r"items/$", endpoint=True, anchored_by_fullmatch=False)
    with pytest.raises(Unconvertible):
        convert_regex(r"^items/", endpoint=True, anchored_by_fullmatch=False)
    with pytest.raises(Unconvertible):
        convert_regex(r"^api/$", endpoint=False, anchored_by_fullmatch=False)


def test_unmodeled_regex_features() -> None:
    """전후방 탐색·대소문자 무시·파싱 오류는 확정할 수 없다."""
    for regex in (
        r"^(?!admin)[a-z]+/$",
        r"(?i)^items/$",
        r"^(?P<x>[a-z]+)/(?P=x)/$",
        r"^(unclosed/$",
        r"^a(?i:b)/$",
        r"^\bword/$",
    ):
        with pytest.raises(Unconvertible):
            convert_regex(regex, endpoint=True, anchored_by_fullmatch=True)


def test_catch_all_last_segment_and_empty_variant() -> None:
    """`/`를 넘는 끝 파라미터는 `{**}`이고, 빈 값을 받으면 빈 끝 세그먼트 변형을 더한다."""
    assert _templates(r"^files/(?P<p>.+)$", fullmatch=True) == [("/files/{**}", None)]
    assert sorted(_templates(r"^files/(?P<p>.*)$", fullmatch=True), key=str) == [
        ("/files/", "strict"),
        ("/files/{**}", None),
    ]


def test_catch_all_in_middle_or_partial_is_dynamic() -> None:
    """중간·부분 세그먼트 catch-all과 한 세그먼트 두 파라미터는 dynamic이다."""
    assert _templates(r"^(?P<p>.+)/edit/$", fullmatch=True) == [("^(?P<p>.+)/edit/$", "dynamic")]
    assert _templates(r"^x(?P<p>.+)$", fullmatch=True)[0][1] == "dynamic"
    assert _templates(r"^(?P<a>[0-9]+)-(?P<b>[0-9]+)\.(?P<c>[a-z]+)$", fullmatch=True)[0][1] == "dynamic"


def test_mixed_capture_group_collapses_to_one_parameter() -> None:
    """캡처 묶음 안쪽이 파라미터 여럿이면 묶음 전체를 regex 파라미터 하나로 본다."""
    alternatives = [
        (Literal("/"), *option)
        for option in convert_regex(
            r"^(?P<code>[a-z]+-[0-9]+)/$", endpoint=True, anchored_by_fullmatch=True
        ).alternatives
    ]
    shape = skeleton_shapes(alternatives, raw=None, trailing_policy="strict")[0]
    assert shape.channel == "/{}/"
    assert shape.constraints[0].kind == "regex"
    assert shape.constraints[0].pattern == "[a-z]+-[0-9]+"


def test_regex_param_kinds() -> None:
    """파라미터 정규식의 종류 판별(int·slug·uuid·제약 없음·regex·catch-all)."""
    assert regex_param("[0-9]+").kind == "int"
    assert regex_param(r"\d+").kind == "int"
    assert regex_param("[-a-zA-Z0-9_]+").kind == "slug"
    assert regex_param(r"[-\w]+").kind == "slug"
    assert regex_param("[^/]+").kind is None
    assert regex_param("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}").kind == "uuid"
    assert regex_param("[0-9]{4}") == Param(kind="regex", pattern="[0-9]{4}", may_be_empty=False, crosses_slash=False)
    assert regex_param(".+").crosses_slash
    assert regex_param("[^/]*").may_be_empty
    assert regex_param(r"\S+").crosses_slash
    assert regex_param(r"[\s]+").kind == "regex"
    with pytest.raises(Unconvertible):
        regex_param("(?=x)x")


def test_expansion_cap() -> None:
    """펼친 템플릿이 16개를 넘으면 상한 예외다."""
    with pytest.raises(ExpansionCapped):
        convert_regex(r"^(?:a|b)(?:c|d)(?:e|f)(?:g|h)(?:i|j)/$", endpoint=True, anchored_by_fullmatch=True)
    many = "|".join(f"x{index}" for index in range(20))
    with pytest.raises(ExpansionCapped):
        convert_regex(rf"^(?:{many})/$", endpoint=True, anchored_by_fullmatch=True)


def test_repeated_literals() -> None:
    """고정 횟수 리터럴 반복은 리터럴로 펼친다."""
    assert _templates(r"^a{3}/$", fullmatch=True) == [("/aaa/", "strict")]


def test_capture_group_texts_skip_classes_and_escapes() -> None:
    """캡처 묶음 원문 추출은 문자 집합·이스케이프·비캡처 묶음을 건너뛴다."""
    assert capture_group_texts(r"^(?P<a>[)(]+)/(\()/(?:x)(?P<b>[]a]+)$") == {1: "[)(]+", 2: r"\(", 3: "[]a]+"}
    assert capture_group_texts(r"(?P<a") == {}


def test_dynamic_shape_hides_unsafe_raw_text() -> None:
    """dynamic 원문은 제어 문자나 길이 초과면 싣지 않는다."""
    assert dynamic_shape("a\nb", "x").channel is None
    assert dynamic_shape("x" * 3000, "x").channel is None
    assert dynamic_shape("/ok", "x").channel == "/ok"


def test_non_canonical_result_is_dynamic() -> None:
    """정규화 뒤에도 문법을 어기면(루트가 아님) dynamic이다."""
    shapes = skeleton_shapes([(Literal("x"),)], raw="x", trailing_policy="strict")
    assert shapes[0].dynamic


def test_encode_decoded_literal() -> None:
    """디코드된 공간의 `%`·공백·비ASCII·짝 없는 서로게이트를 인코딩한다."""
    assert encode_decoded_literal("a%20b/café") == "a%2520b/caf%C3%A9"
    assert encode_decoded_literal("\ud800") == "%EF%BF%BD"
    assert normalize_uri_path("/a%zz") == "/a%25zz"
