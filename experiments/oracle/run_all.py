"""두 fixture에 pythograph를 실행하고 오라클 덤프를 `recorded/`에 기록한 뒤 지표를 출력한다.

사용법(scratch venv의 python으로): python run_all.py [pythograph 명령 ...]
명령을 생략하면 `uv run --project <저장소> pythograph`를 쓴다. 덤프는 절대 경로·시각 없이 결정적으로 쓴다.
fixture를 import하는 것은 이 하네스뿐이며, 제품은 분석 대상을 실행하지 않는다.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 저장소 루트다.
REPOSITORY = HERE.parent.parent
# (dump 스크립트, fixture 경로, 기록 이름) 목록이다.
TARGETS = (
    ("dump_django.py", REPOSITORY / "fixtures" / "django" / "drf-shop", "drf-shop.json"),
    ("dump_flask.py", REPOSITORY / "fixtures" / "flask" / "blog-app", "blog-app.json"),
)
# 결정적 출력을 위한 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"


def pythograph_command(argv):
    """pythograph 실행 명령을 정한다.

    :param argv: 명령줄에서 받은 명령(비면 기본값)
    :returns: 명령 목록
    """
    return list(argv) or ["uv", "run", "--project", str(REPOSITORY), "pythograph"]


def run_pythograph(command, fixture, output):
    """fixture에 routes 명령을 실행해 문서를 파일로 쓴다.

    :param command: pythograph 명령 목록
    :param fixture: fixture 경로
    :param output: 문서를 쓸 경로
    """
    args = command + ["routes", "--role", "server", "--project", str(fixture), "--generated-at", GENERATED_AT]
    with open(output, "w", encoding="utf-8") as handle:
        subprocess.run(args, stdout=handle, check=True)


def run_dump(script, fixture, document, record):
    """덤프 스크립트를 이 인터프리터로 실행한다(바이트코드 파일을 남기지 않는다).

    :param script: dump 스크립트 이름
    :param fixture: fixture 경로
    :param document: pythograph 문서 경로
    :param record: 기록을 쓸 경로
    """
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, str(HERE / script), str(fixture), str(document), str(record)],
                   cwd=HERE, env=environment, check=True)


def main(argv):
    """모든 대상을 실행하고 compare.py의 종료 코드로 끝난다.

    :param argv: pythograph 명령(선택)
    """
    command = pythograph_command(argv)
    records = []
    (HERE / "recorded").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for script, fixture, name in TARGETS:
            document = Path(scratch) / f"{name}.doc.json"
            run_pythograph(command, fixture, document)
            record = HERE / "recorded" / name
            run_dump(script, fixture, document, record)
            records.append(str(record))
    result = subprocess.run([sys.executable, str(HERE / "compare.py"), *records], cwd=HERE)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main(sys.argv[1:])
