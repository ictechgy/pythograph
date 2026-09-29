"""사용자 모델(AbstractUser 상속)과 일대일 프로필."""

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """사용자 지정 사용자 모델."""

    nickname = models.CharField(max_length=40, blank=True)


class Profile(models.Model):
    """사용자 프로필."""

    user = models.OneToOneField("accounts.User", on_delete=models.CASCADE, related_name="profile")
    bio = models.TextField(blank=True)
