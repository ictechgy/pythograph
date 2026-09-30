"""파이썬 클라이언트 × Django 서버 종단 trace: pythograph 클라이언트 문서와 서버 기록을 isthmus workspace trace로 잇는다.

사용법:
    python run_trace.py --isthmus <isthmus dist/cli/main.js> [--record] [--out <디렉터리>]

1. `fixtures/e2e/py-client`에 `pythograph routes --role client`와 `impact`(root = route-call usr 전체, 역방향)를 실행한다.
2. 문서의 `project`를 합성 경로(`/e2e/clients/python`)로 바꾼다. 서버 member는 `experiments/e2e/recorded/`를 쓴다.
3. workspace trace context(member server·python, link `python->api` host 귀속, route 네 개 선택)를 쓰고 `isthmus trace`를
   실행해 `expectations.py`의 체인을 확인한다. `--record`면 입력과 출력을 `recorded/`에 쓴다.

isthmus가 python route-call을 받기 전(개발 중 계약)에는 `routeKindPlatforms`의 route-call에 python을 더한 스크래치 빌드로
실행한다.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expectations import CLIENT_PROJECT, CLIENT_REVISION, check, trace_context  # noqa: E402

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# 클라이언트 fixture다.
FIXTURE = REPOSITORY / "fixtures" / "e2e" / "py-client"
# 기록 디렉터리다.
RECORDED = HERE / "recorded"
# 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"
# 클라이언트 기록 파일 이름이다.
CLIENT_DOCUMENTS = ("python.http.json", "python-reverse.json")


def pythograph(*arguments):
    """pythograph 명령 목록을 만든다(저장소의 개발 환경).

    :param arguments: 하위 명령과 인자
    :returns: 명령 목록
    """
    return ["uv", "run", "--project", str(REPOSITORY), "pythograph", *arguments]


def run(arguments, output):
    """명령을 실행해 표준 출력을 파일로 쓴다.

    :param arguments: 명령 목록
    :param output: 출력 파일
    """
    with open(output, "w", encoding="utf-8") as handle:
        code = subprocess.run(arguments, stdout=handle, check=False).returncode
    if code != 0:
        raise SystemExit(f"{Path(arguments[0]).name} exited with {code}")


def client_documents(out):
    """클라이언트 문서 두 개를 만든다.

    :param out: 산출물 디렉터리
    """
    project = os.path.realpath(FIXTURE)
    run(pythograph("routes", "--role", "client", "--project", project, "--generated-at", GENERATED_AT),
        out / "python.http.json")
    facts = json.loads((out / "python.http.json").read_text(encoding="utf-8"))["facts"]
    roots = list(dict.fromkeys(fact["symbol"]["usr"] for fact in facts))
    (out / "roots.json").write_text(json.dumps(roots), encoding="utf-8")
    run(pythograph("impact", "--project", project, "--revision", CLIENT_REVISION, "--generated-at", GENERATED_AT,
                   "--roots-from", str(out / "roots.json")), out / "python-reverse.json")
    for name in CLIENT_DOCUMENTS:
        path = out / name
        document = json.loads(path.read_text(encoding="utf-8"))
        document["project"] = CLIENT_PROJECT
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    """체인을 실행하고 기대 경로를 확인한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--isthmus", required=True)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--out")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        # context가 서버 기록을 `../../e2e/recorded`로 가리키므로 같은 깊이의 임시 트리를 만든다.
        root = Path(arguments.out or directory)
        out = root / "experiments" / "client_e2e" / "recorded"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPOSITORY / "experiments" / "e2e" / "recorded", root / "experiments" / "e2e" / "recorded",
                        dirs_exist_ok=True)
        client_documents(out)
        context = out / "routes.context.json"
        context.write_text(json.dumps(trace_context(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        run(["node", arguments.isthmus, "trace", str(context)], out / "routes.trace.json")
        trace = json.loads((out / "routes.trace.json").read_text(encoding="utf-8"))
        problems = check(trace)
        if arguments.record:
            for name in (*CLIENT_DOCUMENTS, "routes.context.json", "routes.trace.json"):
                (RECORDED / name).write_text((out / name).read_text(encoding="utf-8"), encoding="utf-8")
    for problem in problems:
        print(f"MISMATCH: {problem}")
    print("all expected chains matched" if not problems else f"{len(problems)} mismatches")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
