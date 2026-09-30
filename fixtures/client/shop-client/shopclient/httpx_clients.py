"""httpx Client·AsyncClient의 base_url 결합."""

import httpx

from shopclient.constants import SHIPPING_ROOT


def stock():
    """base 끝 / 없음 + / 경로 → base 경로 뒤."""
    with httpx.Client(base_url="http://inventory.example.test/api") as client:
        return client.get("/stock")


def patch_item(sku):
    """base 끝 / + 상대 경로."""
    with httpx.Client(base_url="http://inventory.example.test/api/") as client:
        return client.patch(f"items/{sku}", json={"sku": sku})


class ShippingClient:
    """__init__에서 만든 클라이언트 필드."""

    def __init__(self):
        self.client = httpx.Client(base_url=SHIPPING_ROOT)

    def quote(self):
        """/로 시작해도 base 경로 뒤(httpx는 앞 /를 뗀다)."""
        return self.client.post("/quotes", json={})

    def label(self, shipment_id):
        """request(동사, url)."""
        return self.client.request("GET", f"/labels/{shipment_id}")


async def rates():
    """AsyncClient."""
    async with httpx.AsyncClient(base_url="http://shipping.example.test/v3") as client:
        return await client.get("rates")
