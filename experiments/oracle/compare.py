"""오라클 덤프에서 pythograph route-decl의 정밀도와 재현율을 계산한다.

사용법: python compare.py <out.json> [<out.json> ...]

- 정밀도 = 검증된 정적 사실 / 정적 사실. 검증은 표본 경로가 풀리고, 선언한 method(ANY는 다섯 동사 모두)가
  허용되고, usr가 있으면 같은 핸들러이며, trailingSlash 선언이 뒤집은 경로로 확인되는 것이다.
- 재현율 = 검증된 사실이 닿은 끝점 항목 / 끝점 항목. 항목은 (끝점 route, 핸들러, method)이다. 사실 method
  M은 같은 M 항목을, ANY 사실은 모든 항목을 덮는다. ANY 항목은 ANY 사실만 덮는다. 프레임워크 표식이 있는
  끝점(DRF API 루트·형식 접미사)은 따로 센다.
정밀도가 1.0 미만이면 종료 코드 1이다. 이 스크립트는 fixture를 import하지 않는다.
"""

import json
import sys
from pathlib import Path


def covering_keys(probe):
    """검증된 탐침이 덮는 (route, 핸들러, method) 키 판정 함수를 만든다.

    :param probe: 탐침 기록
    :returns: 항목을 받아 덮는지 돌려주는 함수
    """
    method = probe["fact"]["method"]

    def covers(route, entry):
        """항목 하나를 덮는지 판정한다."""
        if route != probe["resolvedRoute"]:
            return False
        if method == "ANY":
            return True
        return entry["method"] == method and entry["handler"] == probe["resolvedHandler"]

    return covers


def entry_rows(endpoints):
    """끝점 기록을 (route, 항목, 표식 여부) 행으로 편다.

    :param endpoints: 끝점 기록 목록
    :returns: 행 목록
    """
    return [(e["route"], entry, bool(e["flags"])) for e in endpoints for entry in e["entries"]]


def recall(rows, probes, flagged):
    """표식 여부가 같은 행의 재현율을 계산한다.

    :param rows: entry_rows 결과
    :param probes: 검증된 탐침 목록
    :param flagged: 표식 있는 행을 셀지 여부
    :returns: (덮인 수, 전체 수, 덮이지 않은 행 목록)
    """
    selected = [row for row in rows if row[2] == flagged]
    coverers = [covering_keys(p) for p in probes]
    missed = [row for row in selected if not any(c(row[0], row[1]) for c in coverers)]
    return len(selected) - len(missed), len(selected), missed


def order_consistent(verified):
    """registration-order 사실의 `order.index` 순서가 resolver 순회 순서와 같은지 본다.

    :param verified: 검증된 탐침 목록
    :returns: (검사한 쌍이 있으면 True/False, 없으면 None)
    """
    pairs = sorted((probe["fact"]["order"]["index"], probe["traversalIndex"]) for probe in verified
                   if probe["fact"].get("order") and probe.get("traversalIndex") is not None)
    if not pairs:
        return None
    return all(left[1] <= right[1] for left, right in zip(pairs, pairs[1:]))


def ratio(numerator, denominator):
    """0으로 나누지 않는 비율이다.

    :param numerator: 분자
    :param denominator: 분모
    :returns: 비율(분모 0이면 1.0)
    """
    return 1.0 if denominator == 0 else numerator / denominator


def summarize(path):
    """덤프 하나의 지표를 계산해 출력한다.

    :param path: 덤프 경로
    :returns: 정밀도가 1.0이면 True
    """
    dump = json.loads(Path(path).read_text(encoding="utf-8"))
    probes = dump["probes"]
    verified = [p for p in probes if p["verified"]]
    rows = entry_rows(dump["endpoints"])
    covered, total, missed = recall(rows, verified, flagged=False)
    covered_f, total_f, _ = recall(rows, verified, flagged=True)
    precision = ratio(len(verified), len(probes))
    print(f"[{dump['framework']}:{dump['fixture']}] precision {len(verified)}/{len(probes)} = {precision:.3f}; "
          f"recall {covered}/{total} = {ratio(covered, total):.3f}; "
          f"framework-flagged recall {covered_f}/{total_f}; dynamic facts {len(dump['dynamicFacts'])}; "
          f"order consistent {order_consistent(verified)}; "
          f"shadowed {sum(1 for probe in verified if probe.get('shadowedBy'))}")
    for probe in probes:
        if not probe["verified"]:
            print(f"  unverified: {probe['fact']['method']} {probe['fact']['channel']} ({probe['fact'].get('usr')})")
    for route, entry, _ in missed:
        print(f"  missed: {entry['method']} {route} ({entry['handler']})")
    return precision == 1.0


def main(argv):
    """모든 덤프를 요약하고 정밀도 실패가 있으면 1로 끝난다.

    :param argv: 덤프 경로 목록
    """
    results = [summarize(path) for path in argv]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
