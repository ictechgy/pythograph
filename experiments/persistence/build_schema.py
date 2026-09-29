"""fixture 모델로 실제 ORM이 만드는 SQLite DDL을 기록한다(isthmus·schemagraph 종단 검증용).

사용법(scratch venv의 python으로): python build_schema.py django|flask <fixture> <출력.sql>
Django는 `schema_editor().create_model()`(자동 M2M 중간 테이블 포함), Flask-SQLAlchemy는 `db.create_all()`로
임시 SQLite 파일에 테이블을 만들고 `sqlite_master`의 DDL을 이름순으로 쓴다. 제품은 분석 대상을 실행하지 않는다.
"""

import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path


def django_schema(fixture, database):
    """Django 모델 테이블을 만든다.

    :param fixture: fixture 경로
    :param database: SQLite 파일 경로
    """
    sys.path.insert(0, str(fixture))
    settings_module = importlib.import_module("shop.settings")
    names = {name: getattr(settings_module, name) for name in dir(settings_module) if name.isupper()}
    names["DATABASES"] = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)}}
    from django.conf import settings

    settings.configure(**names)
    import django

    django.setup()
    from django.apps import apps
    from django.db import connection

    with connection.schema_editor() as editor:
        for model in apps.get_models():
            if model._meta.managed and not model._meta.proxy:
                editor.create_model(model)


def flask_schema(fixture, database):
    """Flask-SQLAlchemy 모델 테이블을 만든다.

    :param fixture: fixture 경로
    :param database: SQLite 파일 경로
    """
    sys.path.insert(0, str(fixture))
    from flask import Flask

    importlib.import_module("blog.models")
    extensions = importlib.import_module("blog.extensions")
    app = Flask("schema-oracle", instance_path=str(database.parent))
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database}"
    extensions.db.init_app(app)
    with app.app_context():
        extensions.db.create_all()


def dump(database):
    """DDL을 이름순으로 모은다.

    :param database: SQLite 파일 경로
    :returns: DDL 텍스트
    """
    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
    ).fetchall()
    connection.close()
    return "".join(f"{row[0]};\n" for row in rows)


def main(argv):
    """DDL을 파일로 쓴다.

    :param argv: 종류, fixture, 출력 경로
    """
    kind, fixture, output = argv
    with tempfile.TemporaryDirectory() as scratch:
        database = Path(scratch) / "schema.sqlite3"
        (django_schema if kind == "django" else flask_schema)(Path(fixture).resolve(), database)
        text = dump(database)
    header = f"-- {kind} fixture DDL recorded by experiments/persistence/build_schema.py (SQLite)\n"
    Path(output).write_text(header + text, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
