"""라벨을 바꾼 앱의 모델."""

from django.db import models


class Widget(models.Model):
    """기본 테이블 이름에 앱 라벨이 들어간다."""

    name = models.CharField(max_length=20)
    owner = models.ForeignKey("accounts.User", on_delete=models.CASCADE)
