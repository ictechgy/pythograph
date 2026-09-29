"""오라클 하네스 공용 도구다.

pythograph 문서의 route-decl 사실에서 표본 경로를 만들고, 핸들러 신원 문자열
(`<fixture 기준 POSIX 경로>#<점 표기 qualname>`)을 계산하고, 결과를 결정적인 JSON으로 쓴다.
이 모듈은 scratch venv 안에서만 실행한다. 제품(pythograph)은 분석 대상을 import하지 않는다.
"""

import inspect
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

# 표본 값 후보다. 정규식 제약은 이 순서로 처음 fullmatch하는 값을 쓴다.
REGEX_CANDIDATES = (
    "7",
    "en",
    "12.5",
    "2024",
    "abc",
    "abc-1",
    "ABC123",
    "draft",
    "json",
    "a1b2",
    "123e4567-e89b-12d3-a456-426614174000",
)

# 닫힌 제약 종류별 표본 값이다.
KIND_SAMPLES = {
    "int": "7",
    "uuid": "123e4567-e89b-12d3-a456-426614174000",
    "slug": "abc-1",
}

# method 제한 여부를 확인할 때 보내는 동사다.
PROBE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def load_document(path):
    """pythograph 문서(JSON)를 읽는다.

    :param path: 문서 경로
    :returns: 파싱한 문서 사전
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """키를 정렬하고 두 칸 들여쓰기로 결정적인 JSON을 쓴다.

    :param path: 쓸 경로
    :param value: 직렬화할 값
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    Path(path).write_text(text + "\n", encoding="utf-8")


def template_segments(channel):
    """정규 템플릿을 세그먼트 목록으로 나눈다(맨 앞 `/` 뒤부터).

    :param channel: `/`로 시작하는 정규 템플릿
    :returns: 세그먼트 문자열 목록
    """
    return channel[1:].split("/")


def constraint_map(fact):
    """사실의 paramConstraints를 세그먼트 인덱스별 사전으로 바꾼다.

    :param fact: route-decl 사실
    :returns: {세그먼트 인덱스: 제약}
    """
    return {entry["segment"]: entry for entry in fact.get("paramConstraints", [])}


def sample_value(constraint):
    """제약을 만족하는 표본 파라미터 값을 고른다.

    :param constraint: paramConstraints 항목 또는 None
    :returns: 표본 문자열
    """
    if constraint is None:
        return "abc"
    kind = constraint.get("kind")
    if kind in KIND_SAMPLES:
        return KIND_SAMPLES[kind]
    pattern = constraint.get("pattern")
    if kind == "regex" and pattern:
        return first_regex_candidate(pattern)
    return "abc"


def first_regex_candidate(pattern):
    """정규식을 fullmatch하는 첫 후보를 고른다. 없으면 "abc"다.

    :param pattern: 정규식 문자열
    :returns: 표본 문자열
    """
    compiled = re.compile(pattern)
    for candidate in REGEX_CANDIDATES:
        if compiled.fullmatch(candidate):
            return candidate
    return "abc"


def sample_segment(segment, constraint):
    """세그먼트 하나를 표본 문자열로 바꾼다.

    :param segment: 템플릿 세그먼트
    :param constraint: 그 세그먼트의 제약 또는 None
    :returns: 퍼센트 디코드한 표본 세그먼트
    """
    if segment == "{**}":
        return "a/b"
    if "{}" in segment:
        prefix, suffix = segment.split("{}", 1)
        return unquote(prefix) + sample_value(constraint) + unquote(suffix)
    return unquote(segment)


def sample_path(fact):
    """사실의 템플릿에서 표본 요청 경로를 만든다.

    :param fact: dynamic이 아닌 route-decl 사실
    :returns: `/`로 시작하는 표본 경로
    """
    constraints = constraint_map(fact)
    segments = template_segments(fact["channel"])
    parts = [sample_segment(seg, constraints.get(index)) for index, seg in enumerate(segments)]
    return "/" + "/".join(parts)


def toggled_slash(path):
    """끝 슬래시를 붙이거나 뗀 경로다. 루트는 바꾸지 않는다.

    :param path: 표본 경로
    :returns: 끝 슬래시를 뒤집은 경로 또는 None(루트)
    """
    if path == "/":
        return None
    return path[:-1] if path.endswith("/") else path + "/"


def lexical_qualname(obj):
    """객체의 qualname에서 `<locals>`를 뺀 점 표기 이름이다.

    :param obj: 함수나 클래스
    :returns: 점 표기 qualname
    """
    return obj.__qualname__.replace(".<locals>", "")


def source_file(obj):
    """객체가 정의된 소스 파일 경로다. 모르면 None이다.

    :param obj: 함수나 클래스
    :returns: 절대 Path 또는 None
    """
    module = sys.modules.get(getattr(obj, "__module__", ""), None)
    try:
        file = inspect.getsourcefile(obj) if inspect.isfunction(obj) else getattr(module, "__file__", None)
    except TypeError:
        file = None
    return Path(file).resolve() if file else None


def object_id(obj, fixture_dir):
    """객체의 pythograph 형식 신원(`<상대 경로>#<qualname>`)이다.

    fixture 밖(프레임워크)의 객체는 `external:<module>:<qualname>`으로 구분한다.

    :param obj: 함수나 클래스
    :param fixture_dir: fixture 루트(절대 Path)
    :returns: 신원 문자열
    """
    file = source_file(obj)
    if file is not None and file.is_relative_to(fixture_dir):
        return f"{file.relative_to(fixture_dir).as_posix()}#{lexical_qualname(obj)}"
    return f"external:{obj.__module__}:{lexical_qualname(obj)}"


def unwrap_function(func):
    """`functools.wraps`가 남긴 `__wrapped__` 사슬을 끝까지 푼다.

    :param func: 데코레이터로 감싼 함수
    :returns: 원래 함수
    """
    seen = set()
    while hasattr(func, "__wrapped__") and id(func) not in seen:
        seen.add(id(func))
        func = func.__wrapped__
    return func


def fact_summary(fact):
    """기록에 남길 사실 요약이다(위치·절대 경로 제외).

    :param fact: route-decl 사실
    :returns: 요약 사전
    """
    keys = ("method", "channel", "pathAnchor", "trailingSlash", "paramConstraints", "catchAllPrefix", "order")
    summary = {key: fact[key] for key in keys if key in fact}
    summary["usr"] = fact.get("symbol", {}).get("usr")
    return summary


def static_facts(document):
    """dynamic이 아닌 route-decl 사실을 결정적 순서로 고른다.

    :param document: pythograph 문서
    :returns: 사실 목록
    """
    facts = [f for f in document.get("facts", []) if f.get("kind") == "route-decl" and not f.get("dynamic")]
    return sorted(facts, key=fact_sort_key)


def dynamic_facts(document):
    """dynamic route-decl 사실의 요약 목록이다.

    :param document: pythograph 문서
    :returns: 요약 목록
    """
    facts = [f for f in document.get("facts", []) if f.get("kind") == "route-decl" and f.get("dynamic")]
    return sorted((fact_summary(f) for f in facts), key=lambda s: json.dumps(s, sort_keys=True))


def fact_sort_key(fact):
    """사실 정렬 키다.

    :param fact: route-decl 사실
    :returns: 정렬 튜플
    """
    return (fact["channel"], fact["method"], fact.get("symbol", {}).get("usr") or "", json.dumps(fact, sort_keys=True))


def trailing_check(fact, resolves_same):
    """trailingSlash 선언을 뒤집은 경로로 확인한다.

    :param fact: route-decl 사실
    :param resolves_same: 경로를 받아 같은 핸들러로 풀리는지 돌려주는 함수
    :returns: True/False, 확인하지 않으면 None
    """
    mode = fact.get("trailingSlash")
    toggled = toggled_slash(sample_path(fact))
    if mode is None or toggled is None:
        return None
    same = resolves_same(toggled)
    return (not same) if mode == "strict" else same
