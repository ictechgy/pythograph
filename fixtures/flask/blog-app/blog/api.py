"""API 블루프린트다. 접두사는 등록 시 `url_prefix`로 준다. 중첩 블루프린트를 가진다."""

from flask import Blueprint

# 접두사 없는 API 블루프린트다.
api_bp = Blueprint("api", __name__)

# API 아래에 중첩하는 관리 블루프린트다.
admin_bp = Blueprint("admin", __name__)


@api_bp.route("/scores/<float:x>")
def score(x):
    """float 변환기 규칙이다."""
    return str(x)


@api_bp.route("/ping", methods=["GET", "HEAD", "OPTIONS"])
def ping():
    """명시적 HEAD·OPTIONS를 가진 규칙이다."""
    return "pong"


@admin_bp.post("/reindex")
def reindex():
    """중첩 블루프린트의 규칙이다(POST)."""
    return "reindexed"


@admin_bp.get("")
def admin_home():
    """빈 규칙이라 접두사 자체가 경로가 된다."""
    return "admin"


api_bp.register_blueprint(admin_bp, url_prefix="/admin")
