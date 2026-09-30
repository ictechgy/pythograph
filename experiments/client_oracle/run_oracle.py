"""모의 서버 오라클: 합성 클라이언트(`fixtures/client/shop-client`)의 실제 요청을 기록하고 route-call 사실과 비교한다.

사용법(fixture requirements를 설치한 스크래치 가상 환경의 python으로):
    python run_oracle.py [--record] [pythograph 명령 ...]

1. 127.0.0.1 임시 포트에 `http.server`를 띄우고 `socket.getaddrinfo`를 바꿔 `*.example.test`를 그 포트로 보낸다
   (외부 네트워크 요청 없음). 시나리오마다 fixture 함수를 한 번 불러 (동사, 경로, Host)를 기록한다.
2. pythograph `routes --role client --wrappers`를 fixture에 실행하고 `compare.py`로 비교해 표를 출력한다.
3. `--record`면 기록을 `recorded.json`에 쓴다(`tests/test_client_oracle.py`가 오프라인으로 다시 비교한다).

fixture를 import·실행하는 것은 이 하네스뿐이며 제품은 분석 대상을 실행하지 않는다. 서버는 끝나면 닫는다.
"""

import asyncio
import http.server
import importlib
import importlib.metadata
import inspect
import json
import platform
import socket
import subprocess
import sys
import threading
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# 합성 클라이언트 fixture다.
FIXTURE = REPOSITORY / "fixtures" / "client" / "shop-client"
# 기록 파일이다.
RECORDED = HERE / "recorded.json"
# 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"

sys.path.insert(0, str(HERE))
from compare import compare  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402


class Recorder(http.server.BaseHTTPRequestHandler):
    """모든 요청을 기록하고 빈 200을 돌려준다."""

    seen = []

    def _record(self):
        """요청 줄과 Host를 기록한다."""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        host = (self.headers.get("Host") or "").lower()
        Recorder.seen.append({"method": self.command, "path": self.path, "host": host})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _record

    def log_message(self, *arguments):
        """접근 로그를 쓰지 않는다."""


def redirect_hosts(port):
    """`*.example.test:80` 연결을 모의 서버로 보낸다.

    이름 해석은 127.0.0.1로 바꾸고(anyio는 해석 결과의 포트를 쓰지 않고 요청 포트로 잇는다), 127.0.0.1:80 연결을 모의
    서버 포트로 바꾼다. 모든 클라이언트(requests·urllib3·urllib, httpx·anyio, aiohttp)가 결국 `socket.socket.connect`로
    잇는다.

    :param port: 모의 서버 포트
    """
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def resolve(host, service, *arguments, **keywords):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if isinstance(name, str) and name.endswith(".example.test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(service or 80)))]
        return original_resolve(host, service, *arguments, **keywords)

    def target(address):
        return ("127.0.0.1", port) if tuple(address[:2]) == ("127.0.0.1", 80) else address

    socket.getaddrinfo = resolve
    socket.socket.connect = lambda self, address: original_connect(self, target(address))
    socket.socket.connect_ex = lambda self, address: original_connect_ex(self, target(address))


def call(module_name, target, arguments):
    """시나리오 함수를 부른다(코루틴이면 끝까지 실행).

    :param module_name: import 이름
    :param target: `함수` 또는 `클래스().메서드`
    :param arguments: 인자
    """
    module = importlib.import_module(module_name)
    if "()." in target:
        owner, method = target.split("().")
        function = getattr(getattr(module, owner)(), method)
    else:
        function = getattr(module, target)
    result = function(*arguments)
    if inspect.iscoroutine(result):
        asyncio.run(result)


def record():
    """모든 시나리오의 요청을 기록한다.

    :returns: 기록 문서
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scenarios = []
    try:
        redirect_hosts(server.server_address[1])
        sys.path.insert(0, str(FIXTURE))
        for usr, module_name, target, arguments in SCENARIOS:
            Recorder.seen.clear()
            call(module_name, target, arguments)
            if len(Recorder.seen) != 1:
                raise SystemExit(f"{usr} sent {len(Recorder.seen)} requests; each scenario must send one")
            scenarios.append({"usr": usr, **Recorder.seen[0]})
    finally:
        server.shutdown()
        server.server_close()
    versions = {name: importlib.metadata.version(name) for name in ("requests", "httpx", "aiohttp", "urllib3", "yarl")}
    return {
        "format": "pythograph-client-oracle",
        "version": 1,
        "python": platform.python_version(),
        "packages": versions,
        "scenarios": scenarios,
    }


def facts(command):
    """fixture의 route-call 문서를 만든다.

    :param command: pythograph 명령 목록
    :returns: 문서
    """
    arguments = command + [
        "routes",
        "--role",
        "client",
        "--project",
        str(FIXTURE),
        "--wrappers",
        str(FIXTURE / "http-wrappers.json"),
        "--generated-at",
        GENERATED_AT,
    ]
    return json.loads(subprocess.run(arguments, capture_output=True, check=True, text=True).stdout)


def main():
    """기록·비교를 실행한다."""
    argv = sys.argv[1:]
    write = "--record" in argv
    command = [item for item in argv if item != "--record"] or ["uv", "run", "--project", str(REPOSITORY), "pythograph"]
    recorded = record()
    rows = compare(recorded, facts(command))
    print("| 시나리오(usr) | 기록 요청 | route-call 사실 | 결과 |")
    print("|---|---|---|---|")
    for usr, request, fact, result in rows:
        print(f"| `{usr.split('#', 1)[1]}` | `{request}` | `{fact}` | {result} |")
    tally = {}
    for row in rows:
        tally[row[3]] = tally.get(row[3], 0) + 1
    print(f"\n{len(rows)} scenarios: " + ", ".join(f"{key} {value}" for key, value in sorted(tally.items())))
    if write:
        RECORDED.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if tally.get("mismatch") or tally.get("missing"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
