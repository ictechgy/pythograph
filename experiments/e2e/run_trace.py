"""Phase 6 종료 조건 검증: Django 백엔드 × iOS/Android 체인에서 isthmus trace 세 질문의 경로를 확인한다.

사용법:
    python run_trace.py --isthmus <isthmus dist/cli/main.js> --schemagraph <schemagraph> [--record] [--out <디렉터리>]

1. `shop-api.sql`(Django가 만드는 DDL + 합성 뷰)을 임시 SQLite에 적용하고 schemagraph `scan`·`facts`·
   `impact --format language-traversal`(모든 relation-decl VertexId가 root)을 실행한다.
2. `pythograph routes`·`schema`·`reach`(root = route-decl 핸들러 usr 전체)·`impact`(root = relation-use usr 전체)를
   실행한다. isthmus capture처럼 root는 선택과 무관한 상위 집합이다.
3. 서버 문서의 `project`를 합성 경로(`/e2e/shop-api`)로 바꿔 기계와 무관한 입력을 만들고, 기록한 클라이언트 문서
   (`recorded/`, `record_clients.py`)와 함께 workspace trace context를 쓴다.
4. `isthmus trace`를 두 번 실행한다: route 선택(질문 a·b)과 relation 선택(질문 c). `expectations.py`로 기대 경로를
   확인하고, `--record`면 입력과 출력을 `recorded/`에 쓴다(`tests/test_e2e_trace.py`가 오프라인으로 다시 확인한다).
   `recorded/`의 context는 그 디렉터리에서 그대로 `isthmus trace recorded/routes.context.json`으로 다시 실행할 수 있다.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expectations import SERVER_PROJECT, check_relations, check_routes, trace_contexts  # noqa: E402

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# 서버 fixture다.
FIXTURE = REPOSITORY / "fixtures" / "e2e" / "shop-api"
# 기록 디렉터리다.
RECORDED = HERE / "recorded"
# 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"
# 서버 member의 합성 revision이다.
SERVER_REVISION = "e2e-shop-api"


def run(arguments, output=None, accept=(0,)):
    """명령을 실행한다.

    :param arguments: 명령 목록
    :param output: 표준 출력 파일(선택)
    :param accept: 받아들이는 종료 코드
    """
    handle = open(output, "w", encoding="utf-8") if output else None
    try:
        code = subprocess.run(arguments, stdout=handle, check=False).returncode
    finally:
        if handle:
            handle.close()
    if code not in accept:
        raise SystemExit(f"{Path(arguments[0]).name} {arguments[1]} exited with {code}")


def pythograph(*arguments):
    """pythograph 명령 목록을 만든다(저장소의 개발 환경).

    :param arguments: 하위 명령과 인자
    :returns: 명령 목록
    """
    return ["uv", "run", "--project", str(REPOSITORY), "pythograph", *arguments]


def server_documents(schemagraph, out):
    """서버 member 문서를 만든다.

    :param schemagraph: schemagraph 실행 파일
    :param out: 산출물 디렉터리
    :returns: 파일 이름 목록
    """
    project = os.path.realpath(FIXTURE)
    database = out / "shop.sqlite3"
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    connection.executescript((HERE / "shop-api.sql").read_text(encoding="utf-8"))
    connection.close()
    run([schemagraph, "scan", f"sqlite:{database}", "--emit-document", str(out / "catalog.json"), "-o",
         str(out / "db-graph.json")])
    run([schemagraph, "facts", "--document", str(out / "catalog.json"), "--project", project, "-o",
         str(out / "server.sql.json")])
    run(pythograph("routes", "--role", "server", "--project", project, "--generated-at", GENERATED_AT),
        out / "server.http.json")
    run(pythograph("schema", "--project", project, "--generated-at", GENERATED_AT), out / "server.persistence.json")
    traversals(schemagraph, project, out)
    for name in SERVER_DOCUMENTS:
        normalize(out / name)
    return SERVER_DOCUMENTS


# 서버 member 문서 이름이다.
SERVER_DOCUMENTS = (
    "server.http.json",
    "server.persistence.json",
    "server.sql.json",
    "server-forward.json",
    "server-reverse.json",
    "server-db.json",
)


def traversals(schemagraph, project, out):
    """세 순회 문서를 만든다(root는 사실에서 뽑은 상위 집합).

    :param schemagraph: schemagraph 실행 파일
    :param project: 서버 project realpath
    :param out: 산출물 디렉터리
    """
    handlers = fact_usrs(out / "server.http.json")
    uses = fact_usrs(out / "server.persistence.json")
    vertices = fact_usrs(out / "server.sql.json")
    (out / "forward-roots.json").write_text(json.dumps(handlers), encoding="utf-8")
    (out / "reverse-roots.json").write_text(json.dumps(uses), encoding="utf-8")
    common = ("--project", project, "--revision", SERVER_REVISION, "--generated-at", GENERATED_AT)
    run(pythograph("reach", *common, "--roots-from", str(out / "forward-roots.json")), out / "server-forward.json")
    run(pythograph("impact", *common, "--roots-from", str(out / "reverse-roots.json")), out / "server-reverse.json")
    run([schemagraph, "impact", *vertices, "--graph", str(out / "db-graph.json"), "--format", "language-traversal",
         "--max", "100000", "--project", project, "--revision", SERVER_REVISION], out / "server-db.json")


def fact_usrs(path):
    """비테스트 사실의 `symbol.usr`를 중복 없이 모은다.

    :param path: bridge-facts 문서
    :returns: usr 목록(처음 나온 순서)
    """
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    usrs = [fact["symbol"]["usr"] for fact in facts if "symbol" in fact and not fact.get("testSource")]
    return list(dict.fromkeys(usrs))


def normalize(path):
    """문서의 project를 합성 경로로 바꿔 기계와 무관하게 만든다.

    :param path: 문서 경로
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    document["project"] = SERVER_PROJECT
    if "generatedAt" in document:
        document["generatedAt"] = GENERATED_AT
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_traces(isthmus, out):
    """두 trace를 실행한다.

    :param isthmus: isthmus CLI 진입점
    :param out: 산출물 디렉터리(서버·클라이언트 문서가 있다)
    :returns: {이름: trace 문서}
    """
    results = {}
    for name, context in trace_contexts().items():
        path = out / f"{name}.context.json"
        path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        run(["node", isthmus, "trace", str(path)], out / f"{name}.trace.json")
        results[name] = json.loads((out / f"{name}.trace.json").read_text(encoding="utf-8"))
    return results


def record(out):
    """입력과 출력을 `recorded/`에 복사한다.

    :param out: 산출물 디렉터리
    """
    for name in SERVER_DOCUMENTS:
        (RECORDED / name).write_text((out / name).read_text(encoding="utf-8"), encoding="utf-8")
    for name in trace_contexts():
        for suffix in ("context", "trace"):
            source = out / f"{name}.{suffix}.json"
            (RECORDED / f"{name}.{suffix}.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print("recorded server documents, contexts, and traces")


def main():
    """전체 체인을 실행하고 기대 경로를 확인한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--isthmus", required=True)
    parser.add_argument("--schemagraph", required=True)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--out")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        out = Path(arguments.out or directory)
        out.mkdir(parents=True, exist_ok=True)
        server_documents(arguments.schemagraph, out)
        for name in ("ios.http.json", "ios-reverse.json", "android.http.json", "android-reverse.json"):
            (out / name).write_text((RECORDED / name).read_text(encoding="utf-8"), encoding="utf-8")
        traces = run_traces(arguments.isthmus, out)
        problems = check_routes(traces["routes"]) + check_relations(traces["relations"])
        if arguments.record:
            record(out)
    for problem in problems:
        print(f"MISMATCH: {problem}")
    print("all expected paths matched" if not problems else f"{len(problems)} mismatches")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
