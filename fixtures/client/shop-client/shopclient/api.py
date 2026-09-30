"""사용자 래퍼(http-wrappers.json에 선언)."""

import enum
from dataclasses import dataclass

import requests

GATEWAY = "http://gateway.example.test/gw"


class HttpMethod(enum.Enum):
    """래퍼 동사 enum."""

    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


def send(method, path, **options):
    """함수 래퍼: 동사와 경로를 그대로 흘려보낸다."""
    verb = method.value if isinstance(method, HttpMethod) else method
    return requests.request(verb, GATEWAY + path, timeout=5, **options)


@dataclass
class Endpoint:
    """생성자 래퍼."""

    path: str
    method: str = "GET"

    def perform(self):
        """요청을 보낸다."""
        return requests.request(self.method, GATEWAY + self.path, timeout=5)


class Gateway:
    """메서드 래퍼."""

    def call(self, path, method=HttpMethod.GET):
        """기본 동사 GET."""
        return send(method, path)
