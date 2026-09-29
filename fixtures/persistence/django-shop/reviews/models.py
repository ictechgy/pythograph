"""상품 후기."""

from django.conf import settings
from django.db import models


class Review(models.Model):
    """후기. 다른 앱의 상품을 문자열로 가리킨다."""

    product = models.ForeignKey("store.Product", on_delete=models.CASCADE)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField()
    body = models.TextField(blank=True)
