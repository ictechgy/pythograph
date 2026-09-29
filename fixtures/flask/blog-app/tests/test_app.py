"""테스트 전용 Flask 앱이다. 기본 추출에서 제외되어야 한다."""

from flask import Flask

# 테스트에서만 쓰는 앱이다.
test_app = Flask(__name__)


@test_app.route("/test-only")
def fake_endpoint():
    """테스트 전용 가짜 뷰다."""
    return "fake"


def test_fake_endpoint_is_registered():
    """테스트 앱에 규칙이 있는지만 본다."""
    assert any(rule.rule == "/test-only" for rule in test_app.url_map.iter_rules())
