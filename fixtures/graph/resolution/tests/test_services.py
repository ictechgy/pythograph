"""테스트 소스는 기본으로 정점이 아니다(--include-tests)."""

from app.core.services import exact_calls


def test_exact():
    """테스트다."""
    assert exact_calls() is not None
