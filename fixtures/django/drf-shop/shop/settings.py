"""합성 상점 프로젝트 설정이다. 라우트 추출 검증용이며 비밀값은 합성 값이다."""

from pathlib import Path

# 프로젝트 루트 디렉터리다.
BASE_DIR = Path(__file__).resolve().parent.parent

# 합성 전용 키다. 실제 배포에 쓰지 않는다.
SECRET_KEY = "synthetic-fixture-key-not-secret"

# 운영 설정을 흉내 내 DEBUG를 끈다(static() 경로는 등록되지 않는다).
DEBUG = False

# 테스트 클라이언트 호스트를 허용한다.
ALLOWED_HOSTS = ["*"]

# 설치 앱 목록이다.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "catalog",
    "orders",
]

# 기본 미들웨어다.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

# 루트 URLconf다.
ROOT_URLCONF = "shop.urls"

# 관리자 화면용 템플릿 설정이다.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# WSGI 진입점이다.
WSGI_APPLICATION = "shop.wsgi.application"

# 로컬 sqlite 데이터베이스다(뷰는 DB를 쓰지 않는다).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# 정적 파일 URL이다.
STATIC_URL = "static/"

# 업로드 파일 URL이다.
MEDIA_URL = "/media/"

# 업로드 파일 디렉터리다.
MEDIA_ROOT = BASE_DIR / "media"

# 기본 기본키 타입이다.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 끝 슬래시 리다이렉트를 켠다(Django 기본값과 같다).
APPEND_SLASH = True
