"""urllib.request와 urljoin."""

from urllib.parse import urljoin
from urllib.request import Request, urlopen

from shopclient.constants import LEGACY_BASE


def legacy_status():
    """urljoin 상대 경로."""
    with urlopen(urljoin(LEGACY_BASE, "status"), timeout=5) as response:
        return response.status


def legacy_root():
    """urljoin 절대 경로는 base 경로를 바꾼다."""
    with urlopen(urljoin(LEGACY_BASE, "/root"), timeout=5) as response:
        return response.status


def legacy_submit(body):
    """Request(method=)."""
    request = Request(LEGACY_BASE + "forms/submit", data=body, method="PUT")
    with urlopen(request, timeout=5) as response:
        return response.status


def legacy_ping():
    """data가 있으면 POST."""
    with urlopen("http://legacy.example.test/app/ping", data=b"ping", timeout=5) as response:
        return response.status
