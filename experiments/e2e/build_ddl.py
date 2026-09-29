"""종단 검증 fixture(`fixtures/e2e/shop-api`)의 모델로 Django가 만드는 SQLite DDL을 기록한다.

사용법(Django 5.2.17·DRF 3.18.1을 설치한 스크래치 가상 환경의 python으로): python build_ddl.py <출력.sql>
`schema_editor().create_model()`로 임시 SQLite 파일에 테이블을 만들고 `sqlite_master`의 DDL을 이름순으로 쓴 뒤,
DB 의존자 질문을 위한 합성 뷰(`store_open_orders`) 한 개를 덧붙인다. 제품은 분석 대상을 실행하지 않는다 — 이 스크립트는
스키마 정답을 기록하는 실험 도구다.
"""

import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# fixture 경로다.
FIXTURE = HERE.parent.parent / "fixtures" / "e2e" / "shop-api"
# DB 의존자 질문용 합성 뷰다(ORM이 만들지 않으므로 손으로 덧붙인다).
EXTRA_VIEW = (
    'CREATE VIEW "store_open_orders" AS SELECT "id", "customer_id", "created_at" '
    "FROM \"store_order\" WHERE \"status\" = 'open';"
)


def create_tables(database):
    """fixture 모델 테이블을 만든다.

    :param database: SQLite 파일 경로
    """
    sys.path.insert(0, str(FIXTURE))
    settings_module = importlib.import_module("config.settings")
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


def dump(database):
    """DDL을 이름순으로 모은다.

    :param database: SQLite 파일 경로
    :returns: DDL 문자열
    """
    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
    ).fetchall()
    connection.close()
    return "".join(f"{row[0]};\n" for row in rows) + EXTRA_VIEW + "\n"


def main():
    """DDL을 기록한다."""
    output = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "schema.sqlite3"
        create_tables(database)
        output.write_text(dump(database), encoding="utf-8")
    print(f"wrote {output.name}")


if __name__ == "__main__":
    main()
