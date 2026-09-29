"""합성 명명 벡터 프로젝트 설정. 오라클은 DATABASES ENGINE을 바꿔 가며 같은 모델을 import한다."""

SECRET_KEY = "synthetic-fixture-not-a-secret"
DEBUG = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "library",
    "accounts",
]
INSTALLED_APPS += ["warehouse_management_system"]
INSTALLED_APPS.append("pkg.sub.apps.SubConfig")

AUTH_USER_MODEL = "accounts.User"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "naming.sqlite3",
    }
}
