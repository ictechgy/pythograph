"""합성 iOS·Android 클라이언트(`clients/`)의 route-call 문서와 역방향 순회 문서를 실제 생산자로 기록한다.

사용법:
    python record_clients.py --cartograph <cartograph> --kartograph <kartograph launcher> [--java-home <JDK 17/21>]
    python record_clients.py --client android --kartograph <kartograph launcher> [--java-home <JDK 17/21>]

클라이언트 소스를 임시 디렉터리에 복사해 빌드하고(`swift build`, `gradle compileKotlin`), cartograph `routes`·
`impact --format language-traversal`, kartograph `snapshot`·`routes --role client`·`impact --format language-traversal`을
실행한다. 역방향 순회의 root는 route-call 사실의 `symbol.usr`다(`--roots-from <http 문서>`). 기록 전에 문서의 `project`
(임시 디렉터리 realpath)를 합성 경로(`/e2e/clients/<이름>`)로 바꾸고, 그 밖에 절대 경로가 남아 있으면 실패한다.
결과는 `recorded/`에 쓴다. `--client`(반복 가능)로 한 클라이언트만 다시 기록할 수 있다 — 한쪽 생산자만 바뀌었을 때
다른 쪽 기록을 건드리지 않기 위해서다. 제품(pythograph)은 이 도구들을 실행하지 않는다.
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 기록 디렉터리다.
RECORDED = HERE / "recorded"
# 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"
# 클라이언트별 합성 revision(kartograph는 40자 커밋 형식 라벨을 요구한다)이다.
REVISIONS = {"ios": "e2e-ios-client", "android": "a11d000000000000000000000000000000000e2e"}


def run(arguments, cwd=None, env=None, output=None):
    """명령을 실행하고 실패하면 멈춘다.

    :param arguments: 명령 목록
    :param cwd: 작업 디렉터리
    :param env: 환경 변수
    :param output: 표준 출력을 쓸 파일(선택)
    """
    if output is None:
        subprocess.run(arguments, cwd=cwd, env=env, check=True)
        return
    with open(output, "w", encoding="utf-8") as handle:
        subprocess.run(arguments, cwd=cwd, env=env, check=True, stdout=handle)


def sanitize(path, project, name):
    """문서의 project를 합성 경로로 바꾸고 절대 경로가 남지 않았는지 확인한다.

    :param path: 문서 경로
    :param project: 임시 project realpath
    :param name: 클라이언트 이름
    :returns: 정리한 문서
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document["project"] = f"/e2e/clients/{name}"
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if project in text or "/Users/" in text or "/private/" in text:
        raise SystemExit(f"{Path(path).name}: an absolute path remains after sanitizing")
    return text


def record_ios(cartograph, work):
    """iOS 클라이언트 문서를 기록한다.

    :param cartograph: cartograph 실행 파일
    :param work: 임시 작업 디렉터리
    :returns: {파일 이름: 내용}
    """
    project = os.path.realpath(work / "ios")
    run(["swift", "build"], cwd=project)
    http = work / "ios.http.json"
    run([cartograph, "routes", "--project", project, "--format", "json"], cwd=project, output=http)
    reverse = work / "ios-reverse.json"
    run([cartograph, "impact", "--project", project, "--format", "language-traversal", "--roots-from", str(http),
         "--revision", REVISIONS["ios"], "--generated-at", GENERATED_AT], cwd=project, output=reverse)
    return {"ios.http.json": sanitize(http, project, "ios"), "ios-reverse.json": sanitize(reverse, project, "ios")}


def record_android(kartograph, work, env):
    """Android 클라이언트 문서를 기록한다.

    :param kartograph: kartograph 실행 파일
    :param work: 임시 작업 디렉터리
    :param env: JDK를 담은 환경 변수
    :returns: {파일 이름: 내용}
    """
    project = os.path.realpath(work / "android")
    run(["gradle", "--no-daemon", "-q", "compileKotlin"], cwd=project, env=env)
    snapshot = work / "android-snapshot.json"
    run([kartograph, "snapshot", "--classes", "build/classes/kotlin/main", "--project", project, "--include-paths",
         "--revision", REVISIONS["android"]], cwd=project, env=env, output=snapshot)
    http = work / "android.http.json"
    run([kartograph, "routes", "--role", "client", "--project", project, "--graph-file", str(snapshot),
         "--format", "json"], cwd=project, env=env, output=http)
    reverse = work / "android-reverse.json"
    run([kartograph, "impact", "--format", "language-traversal", "--roots-from", str(http), "--graph-file",
         str(snapshot), "--project", project, "--generated-at", GENERATED_AT], cwd=project, env=env, output=reverse)
    return {
        "android.http.json": sanitize(http, project, "android"),
        "android-reverse.json": sanitize(reverse, project, "android"),
    }


def parse_arguments():
    """명령줄 인자를 읽고, 고른 클라이언트에 필요한 생산자가 주어졌는지 확인한다.

    :returns: 인자(`clients`는 기록할 클라이언트 이름 집합)
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", action="append", choices=("ios", "android"), dest="clients")
    parser.add_argument("--cartograph")
    parser.add_argument("--kartograph")
    parser.add_argument("--java-home")
    arguments = parser.parse_args()
    arguments.clients = set(arguments.clients or ("ios", "android"))
    if "ios" in arguments.clients and not arguments.cartograph:
        parser.error("--cartograph is required to record the ios client")
    if "android" in arguments.clients and not arguments.kartograph:
        parser.error("--kartograph is required to record the android client")
    return arguments


def main():
    """고른 클라이언트(기본은 둘 다)의 문서를 기록한다."""
    arguments = parse_arguments()
    env = dict(os.environ)
    if arguments.java_home:
        env["JAVA_HOME"] = arguments.java_home
        env["PATH"] = f"{arguments.java_home}/bin:{env['PATH']}"
    documents = {}
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        shutil.copytree(HERE / "clients", work, dirs_exist_ok=True)
        if "ios" in arguments.clients:
            documents.update(record_ios(arguments.cartograph, work))
        if "android" in arguments.clients:
            documents.update(record_android(arguments.kartograph, work, env))
    RECORDED.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(documents.items()):
        (RECORDED / name).write_text(text, encoding="utf-8")
        print(f"recorded {name}")


if __name__ == "__main__":
    main()
