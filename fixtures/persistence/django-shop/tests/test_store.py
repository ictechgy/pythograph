"""테스트 소스(기본으로 읽지 않는다)."""

from store.models import Tag


def test_tags():
    """태그 질의."""
    assert Tag.objects.filter(name="x").count() == 0
