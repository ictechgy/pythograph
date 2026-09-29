"""Django가 실제로 만드는 테이블·컬럼 이름을 덤프한다(명명 벡터 오라클).

사용법(scratch venv의 python으로): python dump_django_names.py <fixture> <backend> <출력.json>
backend는 sqlite3·postgresql·mysql·oracle이다. 설정 모듈의 DATABASES ENGINE만 바꿔 같은 모델을 import한다.
모델 클래스를 만들 때 Django가 부르는 `connection.ops.max_name_length()`와 SQL에 쓰는
`connection.ops.quote_name()`이 그 백엔드의 것이므로, 데이터베이스에 연결하지 않고 실제 이름을 얻는다.
이 스크립트만 fixture를 import한다. 제품(pythograph)은 분석 대상을 실행하지 않는다.
"""

import importlib
import json
import os
import sys
from pathlib import Path


def configure(fixture, backend):
    """fixture 설정 모듈을 읽고 ENGINE을 바꿔 Django를 초기화한다.

    :param fixture: fixture 경로
    :param backend: 백엔드 이름
    """
    sys.path.insert(0, str(fixture))
    if backend == "mysql":
        import pymysql

        pymysql.install_as_MySQLdb()
    module = importlib.import_module("naming.settings")
    names = {name: getattr(module, name) for name in dir(module) if name.isupper()}
    names["DATABASES"] = {"default": {"ENGINE": f"django.db.backends.{backend}", "NAME": "oracle_only"}}
    from django.conf import settings

    settings.configure(**names)
    import django

    django.setup()


def final_segments(connection, identifier):
    """백엔드 quote_name 결과를 이름 조각으로 바꾼다.

    :param connection: Django 연결
    :param identifier: Django가 SQL에 넣는 이름
    :returns: 조각 목록(따옴표 제거)
    """
    quoted = connection.ops.quote_name(identifier)
    mark = quoted[0]
    inner = quoted[1:-1] if quoted[-1] == mark else quoted
    return inner.split(f"{mark}.{mark}")


def dump(fixture, backend):
    """모든 모델(자동 중간 모델·교체된 모델 포함)의 이름을 모은다.

    :param fixture: fixture 경로
    :param backend: 백엔드 이름
    :returns: 라벨 → {table, columns}
    """
    configure(fixture, backend)
    from django.apps import apps
    from django.db import connection

    result = {}
    for model in apps.get_models(include_auto_created=True, include_swapped=True):
        options = model._meta
        if options.proxy:
            table = options.concrete_model._meta.db_table
        else:
            table = options.db_table
        result[options.label_lower] = {
            "table": final_segments(connection, table),
            "columns": {field.name: final_segments(connection, field.column)[0] for field in options.local_concrete_fields},
            "proxy": options.proxy,
        }
    return {"backend": backend, "maxNameLength": connection.ops.max_name_length(), "models": result}


def main(argv):
    """덤프를 파일로 쓴다.

    :param argv: fixture, backend, 출력 경로
    """
    fixture, backend, output = argv
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    value = dump(Path(fixture).resolve(), backend)
    Path(output).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
