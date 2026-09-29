"""합성 블로그 앱 팩토리."""

from flask import Flask

from blog.extensions import db


def create_app():
    """앱을 만든다."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///blog.sqlite3"
    db.init_app(app)
    from blog import views

    app.register_blueprint(views.bp)
    return app
