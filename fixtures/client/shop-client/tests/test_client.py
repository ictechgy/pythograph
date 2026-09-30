"""테스트 소스는 기본으로 스캔하지 않는다."""

import requests


def test_ping():
    """--include-tests에서만 route-call이 된다."""
    requests.get("http://api.example.test/v1/test-only", timeout=5)
