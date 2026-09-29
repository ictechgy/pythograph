"""라벨을 바꾼 앱 설정."""

from django.apps import AppConfig


class SubConfig(AppConfig):
    """`pkg.sub` 앱. 라벨을 `custom_label`로 바꾼다."""

    name = "pkg.sub"
    label = "custom_label"
