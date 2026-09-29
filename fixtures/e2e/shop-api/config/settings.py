"""합성 종단 검증 API 설정이다(Phase 6 체인 fixture)."""

SECRET_KEY = "synthetic-fixture-not-a-secret"
DEBUG = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "config.urls"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "store",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "shop.sqlite3"}}
