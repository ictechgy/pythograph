"""`routes --role client` 규칙별 회귀: 라이브러리 인식, base 결합, 상수 증명, 래퍼 선언, 한계 문구."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pythograph.routes.client.compose import (
    JOIN_AIOHTTP,
    JOIN_NONE,
    UNKNOWN_BASE,
    BaseUrl,
    Literal,
    Value,
    join_path,
    mask_prefix,
    remove_dot_segments,
    split_url,
)
from pythograph.routes.client.formats import Field, format_pieces, percent_pieces
from pythograph.routes.versions import spec_minimum
from tests.conftest import GENERATED_AT, run_cli

#: 합성 프로젝트를 만드는 함수 타입이다.
MakeProject = Callable[[dict[str, str]], Path]


def client_document(project: Path, *extra: str) -> dict[str, Any]:
    """`routes --role client` 문서를 만든다.

    Args:
        project: 프로젝트 경로.
        extra: 추가 인자.

    Returns:
        문서.
    """
    code, out, err = run_cli(
        ["routes", "--role", "client", "--project", str(project), "--generated-at", GENERATED_AT, *extra]
    )
    assert code == 0, err
    document: dict[str, Any] = json.loads(out)
    return document


def rows(document: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    """사실을 usr → (method, channel 또는 prefix, 앵커, authority) 사전으로 요약한다(usr마다 사실 하나).

    Args:
        document: 문서.

    Returns:
        요약 사전.
    """
    summary: dict[str, tuple[Any, ...]] = {}
    for fact in document["facts"]:
        path = fact["channel"] if not fact["dynamic"] else ("dynamic", fact.get("channelPrefix"))
        usr = fact["symbol"]["usr"].split("#", 1)[1]
        assert usr not in summary, usr
        summary[usr] = (fact.get("method"), path, fact["pathAnchor"], fact.get("authority"))
    return summary


def limitation_text(document: dict[str, Any]) -> str:
    """limitation 문장을 한 문자열로 잇는다.

    Args:
        document: 문서.

    Returns:
        문자열.
    """
    return "\n".join(document["limitations"])


def test_interpolation_forms_and_constants(make_project: MakeProject) -> None:
    """f-string·`+`·`%`·`str.format`·모듈 상수(다른 모듈 포함)를 조각으로 읽는다."""
    root = make_project(
        {
            "app/__init__.py": "",
            "app/config.py": 'HOST = "https://api.example.com"\nAPI = HOST + "/v1"\n',
            "app/calls.py": """
                import requests as r
                from requests import get
                from app import config
                from app.config import API

                def percent(item_id):
                    return r.get("%s/items/%s" % (API, item_id))

                def percent_named(item_id):
                    return r.get("%(base)s/items/%(id)d" % {"base": API, "id": item_id})

                def fmt(item_id):
                    return get("{}/items/{}/{{raw}}".format(API, item_id))

                def fmt_named(slug):
                    return r.get("{base}/tags/{slug!s}".format(base=config.API, slug=slug))

                def fmt_spec(item_id):
                    return r.get("{0}/items/{1:04d}".format(API, item_id))

                def get(url):
                    return url
            """,
        }
    )
    summary = rows(client_document(root))
    assert summary["percent"] == ("GET", "/v1/items/{}", "root", "api.example.com")
    assert summary["percent_named"] == ("GET", "/v1/items/{}", "root", "api.example.com")
    assert summary["fmt_named"] == ("GET", "/v1/tags/{}", "root", "api.example.com")
    assert summary["fmt_spec"] == ("GET", "/v1/items/{}", "root", "api.example.com")
    # 같은 모듈이 `get`을 다시 정의하므로 `get(...)`은 requests 호출이 아니다.
    assert "fmt" not in summary


def test_unproven_constants_are_values(make_project: MakeProject) -> None:
    """다시 묶이거나 global·모듈 속성 대입이 있는 이름은 상수가 아니다."""
    root = make_project(
        {
            "app/__init__.py": "",
            "app/consts.py": """
                import os
                TWICE = "https://a.example.com"
                TWICE = "https://b.example.com"
                GLOBAL = "https://c.example.com"
                PATCHED = "https://d.example.com"
                if os.environ.get("X"):
                    COND = "https://e.example.com"
                else:
                    COND = "https://f.example.com"
                for LOOP in ("https://g.example.com",):
                    pass
                SAFE = "https://h.example.com"

                def switch():
                    global GLOBAL
                    GLOBAL = "https://z.example.com"
            """,
            "app/use.py": """
                import requests
                from app import consts
                from app.consts import TWICE, GLOBAL, COND, LOOP, SAFE

                consts.PATCHED = "https://y.example.com"

                def twice():
                    return requests.get(TWICE + "/x")

                def global_():
                    return requests.get(GLOBAL + "/x")

                def patched():
                    return requests.get(consts.PATCHED + "/x")

                def cond():
                    return requests.get(COND + "/x")

                def loop():
                    return requests.get(LOOP + "/x")

                def safe():
                    return requests.get(SAFE + "/x")
            """,
        }
    )
    summary = rows(client_document(root))
    for name in ("twice", "global_", "patched", "cond", "loop"):
        assert summary[name] == ("GET", "/x", "base", None), name
    assert summary["safe"] == ("GET", "/x", "root", "h.example.com")


def test_absolute_url_forms(make_project: MakeProject) -> None:
    """userinfo 제거, host가 값인 URL은 base, 다른 scheme·scheme 상대·상대 URL은 dynamic, 웹훅은 가린다."""
    root = make_project(
        {
            "app/net.py": """
                import requests

                def userinfo():
                    return requests.get("https://user:pw@API.Example.com:8443/v1/me#top")

                def dynamic_host(host):
                    return requests.get(f"https://{host}/v1/items")

                def dynamic_port(port):
                    return requests.get(f"http://api.example.com:{port}/v1/items")

                def ftp():
                    return requests.get("ftp://files.example.com/x")

                def scheme_relative():
                    return requests.get("//api.example.com/x")

                def relative():
                    return requests.get("/v1/items")

                def webhook():
                    return requests.post("https://hooks.slack.com/services/T0/B0/abc")

                def discord():
                    return requests.post("https://discord.com/api/webhooks/1/token")

                def root_only():
                    return requests.get("https://api.example.com?x=1")

                def host_then_query(host):
                    return requests.get(f"https://{host}?q=1")
            """
        }
    )
    document = client_document(root)
    summary = rows(document)
    assert summary["userinfo"] == ("GET", "/v1/me", "root", "api.example.com:8443")
    assert summary["dynamic_host"] == ("GET", "/v1/items", "base", None)
    assert summary["dynamic_port"] == ("GET", "/v1/items", "base", None)
    assert summary["ftp"][1] == ("dynamic", None)
    assert summary["scheme_relative"][1] == ("dynamic", None)
    assert summary["relative"][1] == ("dynamic", None)
    assert summary["webhook"] == ("POST", "/{}/{}/{}/{}", "root", "hooks.slack.com")
    assert summary["discord"] == ("POST", "/api/webhooks/{}/{}", "root", "discord.com")
    assert summary["root_only"] == ("GET", "/", "root", "api.example.com")
    assert summary["host_then_query"] == ("GET", "/", "base", None)
    masked = {fact["symbol"]["usr"]: fact.get("maskedSegments") for fact in document["facts"]}
    assert masked["app/net.py#webhook"] == 4


def test_query_tail_proofs(make_project: MakeProject) -> None:
    """끝 지역 변수가 `?`로 시작하거나 빈 값뿐이면 query 꼬리로 뗀다. 증명하지 못하면 dynamic이다."""
    root = make_project(
        {
            "app/net.py": """
                import requests
                from urllib.parse import urlencode

                BASE = "https://api.example.com"

                def concat(q):
                    query = "?" + urlencode(q)
                    return requests.get(f"{BASE}/search{query}")

                def conditional(q):
                    query = f"?{urlencode(q)}" if q else ""
                    return requests.get(BASE + "/search" + query)

                def unproven(q):
                    suffix = urlencode(q)
                    return requests.get(f"{BASE}/search{suffix}")

                def middle(q, item):
                    query = "?" + q
                    return requests.get(f"{BASE}/a{query}/{item}")
            """
        }
    )
    document = client_document(root)
    summary = rows(document)
    assert summary["concat"] == ("GET", "/search", "root", "api.example.com")
    assert summary["conditional"] == ("GET", "/search", "root", "api.example.com")
    assert summary["unproven"][1] == ("dynamic", "/search")
    assert summary["middle"][1] == ("dynamic", "/a")
    stripped = {fact["symbol"]["usr"] for fact in document["facts"] if fact.get("queryTailStripped")}
    assert stripped == {"app/net.py#concat", "app/net.py#conditional"}


def test_client_objects(make_project: MakeProject) -> None:
    """모듈 변수·주석 매개변수·데이터 클래스 필드·하위 클래스·서로 다른 base·주입 필드를 판정한다."""
    root = make_project(
        {
            "app/__init__.py": "",
            "app/shared.py": 'import httpx\nclient = httpx.Client(base_url="https://inv.example.com/api/")\n',
            "app/net.py": """
                from dataclasses import dataclass

                import httpx
                import requests
                from app import shared
                from app.shared import client

                def module_variable():
                    return client.get("stock")

                def module_attribute():
                    return shared.client.get("/stock")

                def annotated_session(session: requests.Session):
                    return session.get("https://api.example.com/a")

                def annotated_client(http: "httpx.Client | None"):
                    return http.get("/b")

                @dataclass
                class Holder:
                    http: httpx.AsyncClient

                    async def call(self):
                        return await self.http.delete("items/1")

                class Api(httpx.Client):
                    def ping(self):
                        return self.get("/ping")

                class Mixed:
                    def __init__(self, flag):
                        if flag:
                            self.http = httpx.Client(base_url="https://a.example.com/x/")
                        else:
                            self.http = httpx.Client(base_url="https://b.example.com/y/")

                    def call(self):
                        return self.http.put("z")

                class Injected:
                    def __init__(self, session):
                        self.session = session

                    def call(self):
                        return self.session.get("/untyped")

                def kwargs_base(options):
                    with httpx.Client(**options) as http:
                        return http.get("/k")

                def no_base():
                    with httpx.Client() as http:
                        return http.get("/nb")

                def none_base():
                    with httpx.Client(base_url=None) as http:
                        return http.get("/nb")
            """,
        }
    )
    document = client_document(root)
    summary = rows(document)
    assert summary["module_variable"] == ("GET", "/api/stock", "root", "inv.example.com")
    assert summary["module_attribute"] == ("GET", "/api/stock", "root", "inv.example.com")
    assert summary["Holder.call"] == ("DELETE", "/items/1", "base", None)
    assert summary["Api.ping"] == ("GET", "/ping", "base", None)
    assert summary["Mixed.call"] == ("PUT", "/z", "base", None)
    assert summary["kwargs_base"] == ("GET", "/k", "base", None)
    assert summary["no_base"][1] == ("dynamic", None)
    assert summary["none_base"][1] == ("dynamic", None)
    assert summary["annotated_session"] == ("GET", "/a", "root", "api.example.com")
    assert summary["annotated_client"] == ("GET", "/b", "base", None)
    assert "1 get/post/request-style calls pass a URL literal" in limitation_text(document)


def test_httpx_base_rules(make_project: MakeProject) -> None:
    """httpx base 재대입은 리터럴 base를 버리고, `//`·모르는 base 뒤 `..`는 주장하지 않는다."""
    root = make_project(
        {
            "app/net.py": """
                import httpx

                def network_path():
                    with httpx.Client(base_url="https://api.example.com/api") as http:
                        return http.get("//users")

                def parent(base):
                    with httpx.Client(base_url=base) as http:
                        return http.get("../users")

                def dotted(base):
                    with httpx.Client(base_url=base) as http:
                        return http.get("./users/./me")
            """
        }
    )
    summary = rows(client_document(root))
    assert summary["network_path"][1] == ("dynamic", None)
    assert summary["parent"][1] == ("dynamic", None)
    assert summary["dotted"] == ("GET", "/users/me", "base", None)
    rewritten = make_project(
        {
            "app/net.py": """
                import httpx

                def call():
                    http = httpx.Client(base_url="https://api.example.com/api/")
                    http.base_url = "https://other.example.com/"
                    return http.get("users")
            """
        }
    )
    assert rows(client_document(rewritten))["call"] == ("GET", "/users", "base", None)


AIOHTTP_CALLS = """
    import aiohttp

    async def with_path():
        async with aiohttp.ClientSession(base_url="https://recs.example.com/api/") as session:
            return await session.get("items")

    async def origin_rooted():
        async with aiohttp.ClientSession("https://recs.example.com") as session:
            return await session.get("/items")

    async def origin_relative():
        async with aiohttp.ClientSession("https://recs.example.com") as session:
            return await session.get("items")

    async def absolute_on_base():
        async with aiohttp.ClientSession(base_url="https://recs.example.com/") as session:
            return await session.get("https://other.example.com/x")

    async def no_slash():
        async with aiohttp.ClientSession(base_url="https://recs.example.com/api") as session:
            return await session.get("/items")

    async def top_level():
        async with aiohttp.request("get", "https://recs.example.com/top") as response:
            return response.status
"""


@pytest.mark.parametrize(
    ("requirements", "expected"),
    [
        (
            None,
            {
                "with_path": ("dynamic", None),
                "origin_rooted": "/items",
                "origin_relative": ("dynamic", None),
                "absolute_on_base": ("dynamic", None),
            },
        ),
        (
            "aiohttp>=3.11,<4\n",
            {
                "with_path": "/api/items",
                "origin_rooted": "/items",
                "origin_relative": "/items",
                "absolute_on_base": ("dynamic", None),
            },
        ),
        (
            "aiohttp==3.14.3\n",
            {
                "with_path": "/api/items",
                "origin_rooted": "/items",
                "origin_relative": "/items",
                "absolute_on_base": "/x",
            },
        ),
    ],
)
def test_aiohttp_version_constraints(
    make_project: MakeProject, requirements: str | None, expected: dict[str, object]
) -> None:
    """aiohttp base 결합은 선언 파일이 증명한 버전에 따라 주장 범위가 달라진다."""
    files = {"app/recs.py": AIOHTTP_CALLS}
    if requirements is not None:
        files["requirements.txt"] = requirements
    summary = rows(client_document(make_project(files)))
    for name, path in expected.items():
        assert summary[name][1] == path, name
    assert summary["no_slash"][1] == ("dynamic", None)
    assert summary["top_level"] == ("GET", "/top", "root", "recs.example.com")


def test_urllib_methods(make_project: MakeProject) -> None:
    """urlopen 동사는 Request(method=)·데이터로 정하고, 모르면 methodDynamic이다."""
    root = make_project(
        {
            "app/legacy.py": """
                import urllib.request
                from urllib.parse import urljoin

                BASE = "https://legacy.example.com/app/"

                def opaque(target):
                    return urllib.request.urlopen(target)

                def data_variable(payload):
                    return urllib.request.urlopen(BASE + "x", payload)

                def lower_method():
                    return urllib.request.urlopen(urllib.request.Request(BASE + "y", method="put"))

                def request_data(payload):
                    return urllib.request.urlopen(urllib.request.Request(BASE + "z", data={"a": 1}))

                def unsent():
                    return urllib.request.Request(BASE + "never")

                def joined_absolute():
                    return urllib.request.urlopen(urljoin(BASE, "https://other.example.com/w"))
            """
        }
    )
    document = client_document(root)
    summary = rows(document)
    assert summary["opaque"][0] is None
    assert summary["data_variable"][0] is None
    assert summary["lower_method"][0] is None
    assert summary["request_data"] == ("POST", "/app/z", "root", "legacy.example.com")
    assert summary["joined_absolute"] == ("GET", "/w", "root", "other.example.com")
    assert "1 urllib.request.Request objects are not passed directly to urlopen" in limitation_text(document)


def test_unmodeled_and_request_methods(make_project: MakeProject) -> None:
    """모델링하지 않는 API·클라이언트 메서드는 세고, request(동사, url)의 동사를 정한다."""
    root = make_project(
        {
            "app/net.py": """
                import http.client
                import httpx
                import requests

                VERB = "patch"

                def prepared():
                    session = requests.Session()
                    return session.send(requests.Request("GET", "https://api.example.com/p").prepare())

                def raw():
                    return http.client.HTTPSConnection("api.example.com").request("GET", "/raw")

                def constant_verb():
                    return requests.request(VERB, "https://api.example.com/v")

                def dynamic_verb(verb):
                    return httpx.request(verb, "https://api.example.com/d")

                def bad_verb():
                    return requests.request("fetch", "https://api.example.com/b")

                def stream_on_session():
                    return requests.Session().stream("GET", "https://api.example.com/s")

                def missing_url(**options):
                    return requests.get(**options)
            """
        }
    )
    document = client_document(root)
    summary = rows(document)
    assert summary["constant_verb"][0] == "PATCH"
    assert summary["dynamic_verb"][0] is None
    assert summary["bad_verb"][0] is None
    assert "stream_on_session" not in summary
    assert summary["missing_url"][1] == ("dynamic", None)
    text = limitation_text(document)
    assert "calls use requests request APIs" in text
    assert "calls use http request APIs" in text


def test_include_tests_and_service(make_project: MakeProject) -> None:
    """테스트 소스는 --include-tests에서만 testSource로 싣고, --service는 모든 사실에 싣는다."""
    root = make_project(
        {
            "app/net.py": 'import requests\n\ndef call():\n    return requests.get("https://a.example.com/x")\n',
            "tests/test_net.py": 'import requests\n\ndef test_x():\n    requests.get("https://a.example.com/t")\n',
        }
    )
    default = client_document(root, "--service", "shop")
    assert default["sourceSets"] == {"tests": "excluded"} and default["service"] == "shop"
    assert [fact["service"] for fact in default["facts"]] == ["shop"]
    included = client_document(root, "--include-tests")
    assert included["sourceSets"] == {"tests": "included"}
    assert sorted(fact.get("testSource", False) for fact in included["facts"]) == [False, True]


def test_empty_project_keeps_http_target(make_project: MakeProject) -> None:
    """호출이 없어도 target은 http이고 roles는 client다(스캔했으나 호출 없음)."""
    document = client_document(make_project({"app/x.py": "VALUE = 1\n", "app/broken.py": "def (:\n"}))
    assert document["facts"] == [] and document["target"] == "http" and document["roles"] == ["client"]
    assert "could not be analyzed" in limitation_text(document)


WRAPPER_CODE = """
    import enum

    import requests

    class Verb(enum.Enum):
        GET = "GET"
        PUT = "PUT"

    METHOD = "DELETE"

    def send(method, path, *rest, **options):
        return requests.request(method, "https://gw.example.com" + path)

    def run(args, options):
        send(Verb.PUT, "/a")
        send("GET", "https://gw.example.com/full")
        send(METHOD, "/c")
        send(*args)
        send("GET", **options)
        send("GET", "relative")
        send(method=Verb.GET, path=f"/items/{options}")
        send("GET", options + "/tail")
"""


def _wrappers(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """래퍼 선언 파일을 쓴다.

    Args:
        tmp_path: 임시 디렉터리.
        entries: 래퍼 항목.

    Returns:
        파일 경로.
    """
    path = tmp_path / "wrappers.json"
    path.write_text(json.dumps({"format": "http-wrappers", "version": 1, "wrappers": entries}), encoding="utf-8")
    return path


def test_wrapper_bindings(make_project: MakeProject, tmp_path: Path) -> None:
    """래퍼 호출의 경로·동사 바인딩(`*args`·`**kwargs`·상대 경로·전체 URL·앞 값)과 service를 확인한다."""
    root = make_project({"gw/net.py": WRAPPER_CODE})
    wrappers = _wrappers(
        tmp_path,
        [
            {
                "language": "python",
                "kind": "function",
                "owner": "gw/net.py",
                "name": "send",
                "methodArg": {"index": 0, "label": "method"},
                "pathArg": {"index": 1, "label": "path"},
                "methodEnum": {"GET": "GET", "PUT": "PUT"},
                "pathAnchor": "root",
                "service": "gateway",
            }
        ],
    )
    document = client_document(root, "--wrappers", str(wrappers))
    facts = sorted(
        (fact["location"]["line"], fact.get("method"), fact["channel"], fact["pathAnchor"], fact["dynamic"])
        for fact in document["facts"]
    )
    assert facts == [
        (15, "PUT", "/a", "root", False),
        (16, "GET", "/full", "root", False),
        (17, "DELETE", "/c", "root", False),
        (18, None, None, "base", True),
        (19, "GET", None, "base", True),
        (20, "GET", None, "base", True),
        (21, "GET", "/items/{}", "root", False),
        (22, "GET", "/tail", "base", False),
    ]
    assert {fact["service"] for fact in document["facts"]} == {"gateway"}
    assert "calls join a path to a base URL" in limitation_text(document)


def test_wrapper_unresolved_declarations(make_project: MakeProject, tmp_path: Path) -> None:
    """대상이 없거나 호출이 0건인 파이썬 래퍼는 http-wrapper-unresolved이고 다른 언어 항목은 적용하지 않는다."""
    root = make_project({"gw/net.py": WRAPPER_CODE, "gw/models.py": "class Endpoint:\n    pass\n"})
    base = {"language": "python", "kind": "function", "defaultMethod": "GET", "pathArg": {"index": 0}}
    wrappers = _wrappers(
        tmp_path,
        [
            {**base, "owner": "gw/net.py", "name": "missing", "pathAnchor": "base"},
            {**base, "owner": "gw/net.py", "name": "run", "pathAnchor": "base"},
            {**base, "kind": "constructor", "owner": "gw/models.py#Endpoint", "name": "__init__", "pathAnchor": "base"},
            {**base, "language": "kotlin", "owner": "com.example.Api", "name": "call", "pathAnchor": "base"},
        ],
    )
    text = limitation_text(client_document(root, "--wrappers", str(wrappers)))
    assert "wrappers[0] names no Python definition" in text
    assert "wrappers[1] matched no calls" in text
    assert "wrappers[2] matched no calls" in text
    assert "wrappers[3]" not in text


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"format": "http-wrappers", "version": 2, "wrappers": []}),
        json.dumps({"format": "http-wrappers", "version": 1}),
        json.dumps({"format": "http-wrappers", "version": 1, "wrappers": {}}),
        json.dumps({"format": "http-wrappers", "version": 1, "wrappers": ["x"]}),
        json.dumps(
            {
                "format": "http-wrappers",
                "version": 1,
                "wrappers": [
                    {
                        "language": "python",
                        "kind": "function",
                        "owner": "a.py",
                        "name": "f",
                        "pathArg": {"index": 0},
                        "defaultMethod": "GET",
                        "pathAnchor": "base",
                        "extra": 1,
                    }
                ],
            }
        ),
    ],
)
def test_invalid_wrapper_files_exit_64(make_project: MakeProject, tmp_path: Path, content: str) -> None:
    """선언 파일이 스키마를 어기면 사용법 오류(64)이고 표준 출력은 비어 있다."""
    path = tmp_path / "wrappers.json"
    path.write_text(content, encoding="utf-8")
    root = make_project({"a.py": "X = 1\n"})
    code, out, err = run_cli(["routes", "--role", "client", "--project", str(root), "--wrappers", str(path)])
    assert code == 64 and out == "" and "wrappers" in err


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "function", "owner": "a.py", "name": "f", "pathAnchor": "base"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"index": 0}, "pathAnchor": "base"},
        {"kind": "function", "owner": "a", "name": "f", "pathArg": {"index": 0}, "defaultMethod": "GET"},
        {"kind": "constructor", "owner": "a.py#A", "name": "make", "pathArg": {"index": 0}, "defaultMethod": "GET"},
        {"kind": "constructor", "owner": "a.py", "name": "__init__", "pathArg": {"index": 0}, "defaultMethod": "GET"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"index": -1}, "defaultMethod": "GET"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"label": "a b"}, "defaultMethod": "GET"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"other": 1}, "defaultMethod": "GET"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"index": 0}, "defaultMethod": "get"},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"index": 0}, "methodEnum": {"g": "FETCH"}},
        {"kind": "function", "owner": "a.py", "name": "f", "pathArg": {"index": 0}, "methodEnum": []},
        {"kind": "function", "owner": "a.py", "name": "", "pathArg": {"index": 0}, "defaultMethod": "GET"},
        {"kind": "method", "owner": "a.py", "name": "f", "pathArg": {"index": 0}, "defaultMethod": "GET"},
    ],
)
def test_invalid_wrapper_entries_exit_64(make_project: MakeProject, tmp_path: Path, entry: dict[str, object]) -> None:
    """래퍼 항목 하나가 스키마·파이썬 owner 규칙을 어기면 64다."""
    full = {"language": "python", "pathAnchor": "base", **entry}
    path = _wrappers(tmp_path, [full])
    root = make_project({"a.py": "X = 1\n"})
    code, out, _ = run_cli(["routes", "--role", "client", "--project", str(root), "--wrappers", str(path)])
    assert code == 64 and out == ""


def test_unreadable_wrapper_file_exits_2(make_project: MakeProject, tmp_path: Path) -> None:
    """읽을 수 없는 선언 파일은 입력 오류(2)다."""
    root = make_project({"a.py": "X = 1\n"})
    missing = tmp_path / "missing.json"
    code, out, _ = run_cli(["routes", "--role", "client", "--project", str(root), "--wrappers", str(missing)])
    assert code == 2 and out == ""


def test_compose_helpers() -> None:
    """조립 도우미의 경계 동작."""
    assert remove_dot_segments("/a/b/../../../c/.") == "/c/"
    assert remove_dot_segments("/a/..") == "/"
    assert split_url([]).kind == "dynamic"
    assert split_url([Value(), Literal("/x")]).kind == "base-value"
    assert split_url([Literal("https://h:/x")]).authority == "h"
    assert split_url([Literal("https://bad host/x")]).authority is None
    assert join_path(JOIN_NONE, UNKNOWN_BASE, "x").raw is None
    assert join_path(JOIN_AIOHTTP, BaseUrl(True, "", "h", True), "x", (3, 14)).raw == "/x"
    assert join_path("rfc3986", BaseUrl(True, "/api/", "h", True), "").raw == "/api/"
    assert join_path("rfc3986", UNKNOWN_BASE, "").ambiguous
    assert join_path("rfc3986", UNKNOWN_BASE, "a/.").raw == "/a/"
    assert mask_prefix("/v1/abcdefgh12345678/", None) == "/v1/{}/"
    with pytest.raises(ValueError, match="unknown join style"):
        join_path("nope", UNKNOWN_BASE, "x")


def _merged(pieces: list[object] | None) -> list[object]:
    """이웃한 리터럴 조각을 합친다(`string.Formatter.parse`는 이스케이프에서 리터럴을 나눈다).

    Args:
        pieces: 조각 목록.

    Returns:
        합친 목록(빈 리터럴 제외).
    """
    merged: list[object] = []
    for piece in pieces or []:
        if isinstance(piece, str) and merged and isinstance(merged[-1], str):
            merged[-1] = merged[-1] + piece
        elif piece != "":
            merged.append(piece)
    return merged


def test_format_parsers() -> None:
    """`%` 서식·`str.format` 템플릿 문법 판정."""
    assert percent_pieces("/a/%s/%%/%d", keyed=False) == ["/a/", Field(0, True), "/", "%", "/", Field(1, False), ""]
    assert percent_pieces("/a/%(x)s", keyed=True) == ["/a/", Field("x", True), ""]
    assert percent_pieces("/a/%(x)s", keyed=False) is None
    assert percent_pieces("/a/%*d", keyed=False) is None
    assert percent_pieces("/a/%y", keyed=False) is None
    assert _merged(format_pieces("/{}/{{x}}/{1}/{name!r}")) == [
        "/",
        Field(0, True),
        "/{x}/",
        Field(1, True),
        "/",
        Field("name", False),
    ]
    assert format_pieces("/{0.attr}") is None
    assert format_pieces("/{") is None


@pytest.mark.parametrize(
    ("spec", "minimum"),
    [
        ("==3.14.3", (3, 14)),
        (">=3.11,<4", (3, 11)),
        ("~=3.12.1", (3, 12)),
        ("^3.11", (3, 11)),
        ("~3.9", (3, 9)),
        ("<4", None),
        (">=3", (3, 0)),
        (">=3.*", (3, 0)),
        (">=x", None),
    ],
)
def test_spec_minimum(spec: str, minimum: tuple[int, int] | None) -> None:
    """버전 지정자의 하한을 읽는다."""
    assert spec_minimum(spec) == minimum


def test_declared_minimum_needs_every_declaration_bounded(make_project: MakeProject) -> None:
    """선언 하나라도 하한이 없으면 aiohttp 버전을 증명하지 못한다."""
    files = {
        "app/recs.py": AIOHTTP_CALLS,
        "requirements.txt": "aiohttp>=3.12\n",
        "requirements-dev.txt": "aiohttp<4\n",
    }
    summary = rows(client_document(make_project(files)))
    assert summary["with_path"][1] == ("dynamic", None)


def test_hidden_rebindings_are_not_proven(make_project: MakeProject) -> None:
    """리뷰 회귀: 모듈 함수의 `self` 매개변수·중첩 함수의 대입, 기본값 안 바다코끼리 재대입은 값을 모르게 한다."""
    root = make_project(
        {
            "app/__init__.py": "",
            "app/one.py": """
                import httpx

                class Api:
                    def __init__(self):
                        self.base = "https://good.example.com"

                def reset(self):
                    self.base = "https://evil.example.com"

                def users(api: Api):
                    return httpx.get(api.base + "/v1/users")
            """,
            "app/two.py": """
                import httpx

                class Api2:
                    def __init__(self):
                        self.base = "https://good.example.com"

                        def patch():
                            self.base = "https://evil.example.com"

                        patch()

                    def users(self):
                        return httpx.get(self.base + "/v1/users")
            """,
            "app/three.py": """
                import requests

                API = "https://good.example.com"

                def handler(a=(API := "https://evil.example.com")):
                    return a

                def users3():
                    return requests.get(API + "/v1/users")
            """,
        }
    )
    summary = rows(client_document(root))
    for name in ("users", "Api2.users", "users3"):
        assert summary[name] == ("GET", "/v1/users", "base", None), name


def test_urljoin_absolute_keeps_dot_segments(make_project: MakeProject) -> None:
    """CPython urljoin은 절대 URL 인자를 그대로 돌려주고 urllib는 점 세그먼트를 보존해 보낸다(리뷰 지적 반박)."""
    root = make_project(
        {
            "app/legacy.py": """
                import urllib.parse
                import urllib.request

                def call():
                    return urllib.request.urlopen(urllib.parse.urljoin("https://x.example.com/", "https://h.example.com/a/../b"))
            """
        }
    )
    assert rows(client_document(root))["call"] == ("GET", "/a/../b", "root", "h.example.com")
