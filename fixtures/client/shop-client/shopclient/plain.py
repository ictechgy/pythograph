"""requests·httpx 최상위 함수 호출."""

import httpx
import requests

from shopclient.constants import API_ROOT, ORDERS_HOST


def list_products():
    """f-string과 모듈 상수."""
    return requests.get(f"{API_ROOT}/products", timeout=5)


def product_detail(product_id):
    """세그먼트 전체 보간과 params(경로에 영향 없음)."""
    return requests.get(f"{API_ROOT}/products/{product_id}", params={"expand": "reviews"}, timeout=5)


def search_products(term):
    """query 꼬리 리터럴."""
    return requests.get(API_ROOT + "/search?q=" + term, timeout=5)


def create_order(payload):
    """request(동사, url) — requests는 동사를 대문자로 바꾼다."""
    return requests.request("post", ORDERS_HOST + "/orders", json=payload, timeout=5)


def health():
    """점 세그먼트는 urllib3가 지운다."""
    return requests.head("http://api.example.test/v1/./status/../health", timeout=5)


def page(number):
    """증명한 query 꼬리 지역 변수."""
    suffix = f"?page={number}" if number else ""
    return requests.get(f"{API_ROOT}/pages{suffix}", timeout=5)


def token_status(token):
    """고엔트로피 세그먼트는 가린다."""
    return requests.get(f"{API_ROOT}/tokens/a1b2c3d4e5f6a7b8c9d0/{token}", timeout=5)


def warehouses():
    """httpx 최상위 함수."""
    return httpx.get("http://inventory.example.test/warehouses")


def export_rows():
    """httpx.stream(동사, url)."""
    with httpx.stream("GET", "http://inventory.example.test/export") as response:
        return response.status_code
