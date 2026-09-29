"""합성 상점 프로젝트 설정."""

SECRET_KEY = "synthetic-fixture-not-a-secret"
DEBUG = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "store",
    "reviews.apps.ReviewsConfig",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "shop.sqlite3"}}
