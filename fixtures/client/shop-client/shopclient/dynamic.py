"""dynamic·모호·모델링하지 않은 호출."""

import requests
import urllib3

from shopclient.constants import API_ROOT


def download(name):
    """부분 세그먼트 보간 → dynamic, channelPrefix."""
    return requests.get(f"{API_ROOT}/files/{name}.json", timeout=5)


def proxy_get(url):
    """매개변수 URL → dynamic, 선언되지 않은 래퍼 싱크."""
    return requests.get(url, timeout=5)


def service_status(base_url):
    """모르는 base 뒤 / 경로 → base 앵커."""
    return requests.get(base_url + "/status", timeout=5)


def glued(base_url):
    """모르는 base 뒤 상대 경로 → dynamic, ambiguous-base-join."""
    return requests.get(base_url + "items", timeout=5)


def pooled():
    """urllib3는 모델링하지 않는다(route-call-coverage)."""
    return urllib3.PoolManager().request("GET", "http://legacy.example.test/app/pool")


def untyped(client):
    """타입 모르는 수신자의 URL 리터럴 호출(route-call-coverage)."""
    return client.get("/untyped")
