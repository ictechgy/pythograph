"""명명 벡터를 실제 ORM으로 다시 기록한다.

사용법(Django·SQLAlchemy·Flask-SQLAlchemy와 psycopg·oracledb·pymysql을 설치한 scratch venv의 python으로):
python run_naming.py
결과는 `fixtures/persistence-naming/vectors.json`이다. `tests/test_persistence_naming.py`가 이 기록을 오프라인으로
pythograph의 이름 계산과 비교한다(100% 일치가 기준).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# 명명 fixture 루트다.
FIXTURES = REPOSITORY / "fixtures" / "persistence-naming"
# Django 백엔드 목록이다.
BACKENDS = ("sqlite3", "postgresql", "mysql", "oracle")
# (fixture, import할 모듈) 목록이다.
SQLALCHEMY_TARGETS = (("sa-catalog", "catalog.models"), ("fsa-shop", "shopapp.models"))


def run(script, *arguments):
    """덤프 스크립트를 새 인터프리터로 실행해 결과를 읽는다.

    :param script: 스크립트 이름
    :param arguments: 스크립트 인자(출력 경로 제외)
    :returns: 덤프 사전
    """
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "dump.json"
        subprocess.run([sys.executable, str(HERE / script), *map(str, arguments), str(output)],
                       cwd=scratch, env=environment, check=True)
        return json.loads(output.read_text(encoding="utf-8"))


def versions():
    """오라클에 쓴 패키지 버전을 기록한다.

    :returns: 패키지 → 버전
    """
    from importlib.metadata import version

    return {name: version(name) for name in ("django", "sqlalchemy", "flask-sqlalchemy")}


def main():
    """모든 벡터를 기록한다."""
    value = {
        "format": "pythograph-persistence-naming-vectors",
        "version": 1,
        "description": "Synthetic model definitions mapped to the table and column names the real ORMs produce "
                       "(Django per backend via connection.ops, SQLAlchemy/Flask-SQLAlchemy via mappers).",
        "oracle": versions(),
        "django": {backend: run("dump_django_names.py", FIXTURES / "django-library", backend) for backend in BACKENDS},
        "sqlalchemy": {fixture: run("dump_sqlalchemy_names.py", FIXTURES / fixture, module)
                       for fixture, module in SQLALCHEMY_TARGETS},
    }
    path = FIXTURES / "vectors.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(REPOSITORY)}")


if __name__ == "__main__":
    main()
