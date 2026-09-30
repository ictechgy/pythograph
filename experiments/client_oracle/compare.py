"""모의 서버 기록과 route-call 사실 비교(표준 라이브러리 전용, `run_oracle.py`와 `tests/test_client_oracle.py`가 함께 쓴다).

판정:
- root 템플릿은 기록한 경로 전체와, base 템플릿은 경로의 세그먼트 경계 꼬리와 맞아야 한다. `{}`는 비어 있지 않은
  세그먼트 하나다. query는 비교하지 않는다(사실은 query 꼬리를 뗀다).
- 동사가 확정된 사실은 기록한 동사와 같아야 한다. authority가 있으면 기록한 Host 헤더(소문자, 기본 포트 없음)와 같아야 한다.
- dynamic 사실은 `dynamic`이다. `channelPrefix`가 있으면 root는 경로 앞부분, base는 경로 안의 세그먼트 경계와 맞아야 한다.
"""

import re


def segments(path):
    """경로를 세그먼트로 나눈다.

    :param path: `/`로 시작하는 경로
    :returns: 세그먼트 목록
    """
    return path.split("/")[1:]


def segment_matches(template_segment, segment):
    """템플릿 세그먼트 하나가 요청 세그먼트와 맞는지 본다.

    :param template_segment: 템플릿 세그먼트(`{}` 가능)
    :param segment: 요청 세그먼트
    :returns: 맞으면 True
    """
    if template_segment == "{}":
        return bool(segment)
    return template_segment == segment


def template_matches(template, anchor, path):
    """템플릿이 요청 경로와 맞는지 본다.

    :param template: 정규 템플릿
    :param anchor: `root` 또는 `base`
    :param path: 기록한 경로(query 제외)
    :returns: 맞으면 True
    """
    wanted = segments(template)
    actual = segments(path)
    if anchor == "root":
        return len(wanted) == len(actual) and all(map(segment_matches, wanted, actual))
    if len(wanted) > len(actual):
        return False
    tail = actual[len(actual) - len(wanted) :]
    return all(map(segment_matches, wanted, tail))


def prefix_matches(prefix, anchor, path):
    """dynamic 사실의 channelPrefix가 요청 경로와 맞는지 본다.

    :param prefix: 접두사 템플릿
    :param anchor: 앵커
    :param path: 기록한 경로
    :returns: 맞으면 True
    """
    pattern = re.escape(prefix).replace(re.escape("{}"), "[^/]+")
    if anchor == "root":
        return re.match(pattern, path) is not None
    return re.search(pattern, path) is not None


def judge(record, facts):
    """기록 하나를 그 usr의 사실과 비교한다.

    :param record: {usr, method, path, host}
    :param facts: 같은 usr의 route-call 사실 목록
    :returns: (결과, 사실 설명) — 결과는 `match`·`dynamic`·`mismatch`·`missing`
    """
    if len(facts) != 1:
        return ("missing" if not facts else "mismatch"), f"{len(facts)} facts"
    fact = facts[0]
    path = record["path"].split("?", 1)[0]
    method_ok = fact.get("methodDynamic") or fact.get("method") == record["method"]
    authority_ok = "authority" not in fact or fact["authority"] == record["host"]
    verb = fact.get("method", "?")
    if fact["dynamic"]:
        prefix = fact.get("channelPrefix")
        ok = method_ok and authority_ok and (prefix is None or prefix_matches(prefix, fact["pathAnchor"], path))
        described = f"{verb} dynamic" + (f", prefix {prefix}" if prefix else "")
        return ("dynamic" if ok else "mismatch"), described
    ok = method_ok and authority_ok and template_matches(fact["channel"], fact["pathAnchor"], path)
    return ("match" if ok else "mismatch"), f"{verb} {fact['channel']} {fact['pathAnchor']}"


def compare(recorded, document):
    """기록 전체를 문서와 비교한다.

    :param recorded: 오라클 기록(`scenarios` 목록)
    :param document: route-call 문서
    :returns: [(usr, 기록 설명, 사실 설명, 결과)]
    """
    by_usr = {}
    for fact in document["facts"]:
        by_usr.setdefault(fact["symbol"]["usr"], []).append(fact)
    rows = []
    for record in recorded["scenarios"]:
        result, described = judge(record, by_usr.get(record["usr"], []))
        rows.append((record["usr"], f"{record['method']} {record['path']}", described, result))
    return rows
