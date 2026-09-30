"""requests Session과 API 클라이언트 클래스."""

import requests

from shopclient.constants import API_ROOT


class CatalogClient:
    """클래스 속성 base URL."""

    BASE_URL = "http://api.example.test/v1"

    def __init__(self):
        self.session = requests.Session()

    def categories(self):
        """클래스 속성을 f-string으로."""
        return self.session.get(f"{self.BASE_URL}/categories", timeout=5)

    def remove_category(self, slug):
        """클래스 속성을 + 연결로."""
        return self.session.delete(self.BASE_URL + "/categories/" + slug, timeout=5)


class ReviewClient:
    """__init__ 필드 base URL."""

    def __init__(self):
        self.base = "http://api.example.test/v2"
        self.session = requests.session()

    def reviews(self, product_id):
        """필드를 f-string으로."""
        return self.session.get(f"{self.base}/products/{product_id}/reviews", timeout=5)


def update_profile(token):
    """with 문 Session."""
    with requests.Session() as session:
        return session.put(f"{API_ROOT}/profile", headers={"Authorization": token}, timeout=5)
