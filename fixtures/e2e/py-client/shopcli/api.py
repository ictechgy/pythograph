"""shop-api HTTP 클라이언트."""

import httpx
import requests

# API base URL이다.
API_BASE = "https://api.example.com/api/"


class OrdersApi:
    """주문 API(httpx base_url 결합)."""

    def __init__(self):
        self.client = httpx.Client(base_url=API_BASE, timeout=5)

    def fetch(self, order_id):
        """주문 하나를 읽는다."""
        return self.client.get(f"orders/{order_id}/").json()

    def cancel(self, order_id):
        """주문을 취소한다."""
        return self.client.post(f"orders/{order_id}/cancel/").json()


def list_products():
    """상품 목록(requests 전체 URL)."""
    return requests.get(API_BASE + "products/", timeout=5).json()


def checkout(cart):
    """결제(requests.request)."""
    return requests.request("POST", f"{API_BASE}checkout/", json=cart, timeout=5).json()
