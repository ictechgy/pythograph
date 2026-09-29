"""합성 블로그 Flask 앱이다. 앱 팩토리에서 블루프린트와 규칙을 등록한다."""

from flask import Flask
from werkzeug.routing import BaseConverter

from blog.api import api_bp
from blog.posts import posts_bp
from blog.views import ItemAPI, SettingsView


class HexConverter(BaseConverter):
    """16진수 문자열만 받는 사용자 변환기다."""

    # 변환기가 맞추는 정규식이다.
    regex = "[0-9a-f]+"


def create_app():
    """블로그 앱을 만들고 라우트를 등록한다.

    :returns: 설정을 마친 Flask 앱
    """
    app = Flask(__name__)
    app.url_map.converters["hex"] = HexConverter

    @app.route("/")
    def index():
        """홈이다(기본 GET)."""
        return "home"

    @app.route("/contact", methods=["GET", "POST"])
    def contact():
        """문의 폼이다(GET, POST)."""
        return "contact"

    @app.route("/about/")
    def about():
        """끝 슬래시 branch 규칙이다(strict_slashes 기본값 True)."""
        return "about"

    @app.route("/feed", strict_slashes=False)
    def feed():
        """끝 슬래시가 선택인 규칙이다."""
        return "feed"

    @app.get("/color/<hex:value>")
    def color(value):
        """사용자 변환기(hex) 규칙이다."""
        return value

    @app.get("/lang/<string(length=2):lang>/")
    def language(lang):
        """길이 인자가 있는 string 변환기 규칙이다."""
        return lang

    @app.get("/files/<path:p>")
    def download(p):
        """끝 catch-all(path 변환기) 규칙이다."""
        return p

    app.add_url_rule("/health", view_func=health)
    app.add_url_rule("/items", view_func=ItemAPI.as_view("items"))
    app.add_url_rule("/items/<int:item_id>", view_func=ItemAPI.as_view("item"))
    app.add_url_rule("/settings", view_func=SettingsView.as_view("settings"))
    app.register_blueprint(posts_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    return app


def health():
    """add_url_rule로 등록하는 모듈 수준 함수 뷰다(기본 GET)."""
    return "ok"
