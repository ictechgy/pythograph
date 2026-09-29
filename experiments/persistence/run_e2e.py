"""pythograph `schema` 문서를 schemagraph 카탈로그와 isthmus로 조인하는 종단 검증.

사용법: python run_e2e.py --schemagraph <schemagraph 실행 파일> --isthmus <isthmus dist/cli/main.js> [--out <디렉터리>]
fixture마다 기록한 SQLite DDL(`e2e/<fixture>.sql`, `build_schema.py`가 실제 ORM으로 만든 것)을 임시 DB에 적용하고
`schemagraph scan --emit-document`·`facts`, `pythograph schema`, `isthmus check --pairs`를 차례로 실행해
오류·경고·매치 수를 출력한다. 모든 도구는 이 스크립트 밖에서 빌드한다(제품은 다른 도구를 실행하지 않는다).
"""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# 종단 검증 fixture다.
FIXTURES = ("django-shop", "flask-blog")
# 결정적 출력을 위한 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"


def run(arguments, output=None):
    """명령을 실행한다. 출력 파일이 있으면 표준 출력을 거기에 쓴다.

    :param arguments: 명령 목록
    :param output: 표준 출력 파일 경로(선택)
    :returns: 종료 코드
    """
    if output is None:
        return subprocess.run(arguments, check=False).returncode
    with open(output, "w", encoding="utf-8") as handle:
        return subprocess.run(arguments, stdout=handle, check=False).returncode


def verify(fixture, tools, out):
    """fixture 하나를 종단 검증한다.

    :param fixture: fixture 이름
    :param tools: (schemagraph, isthmus) 경로
    :param out: 산출물 디렉터리
    :returns: 요약 사전
    """
    schemagraph, isthmus = tools
    project = os.path.realpath(REPOSITORY / "fixtures" / "persistence" / fixture)
    database = out / f"{fixture}.sqlite3"
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    connection.executescript((HERE / "e2e" / f"{fixture}.sql").read_text(encoding="utf-8"))
    connection.close()
    catalog, graph, sql, code, check = (out / f"{fixture}.{name}.json" for name in ("catalog", "graph", "sql", "code", "check"))
    run([schemagraph, "scan", f"sqlite:{database}", "--emit-document", str(catalog), "-o", str(graph)])
    run([schemagraph, "facts", "--document", str(catalog), "--project", project, "-o", str(sql)])
    run(["uv", "run", "--project", str(REPOSITORY), "pythograph", "schema", "--project", project,
         "--generated-at", GENERATED_AT], code)
    status = run(["node", isthmus, "check", str(code), str(sql), "--pairs"], check)
    report = json.loads(check.read_text(encoding="utf-8"))
    facts = json.loads(code.read_text(encoding="utf-8"))["facts"]
    return {
        "fixture": fixture,
        "exit": status,
        "relationUses": sum(1 for fact in facts if not fact["dynamic"] and "method" not in fact),
        "columnUses": sum(1 for fact in facts if not fact["dynamic"] and "method" in fact),
        "dynamicUses": sum(1 for fact in facts if fact["dynamic"]),
        "matches": len(report.get("matches", [])),
        "issues": dict(Counter(issue["code"] for issue in report.get("issues", []))),
    }


def main():
    """모든 fixture를 검증하고 요약을 출력한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--schemagraph", required=True)
    parser.add_argument("--isthmus", required=True)
    parser.add_argument("--out")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(options.out) if options.out else Path(scratch)
        out.mkdir(parents=True, exist_ok=True)
        summaries = [verify(fixture, (options.schemagraph, options.isthmus), out) for fixture in FIXTURES]
    print(json.dumps(summaries, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
