"""이름으로 건너뛰는 디렉터리(`build`·`dist`·`env`·`venv`): 패키지는 들어가고, 패키지가 아닌 소스는 한계로 센다."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pythograph.graph.build import build_graph
from pythograph.source.project import Project
from tests.conftest import GENERATED_AT, run_cli

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]


def test_package_named_like_build_output_is_scanned(make_project: MakeProject) -> None:
    """`__init__.py`를 직접 담은 `build` 디렉터리는 프로젝트 패키지라 순회한다(가상 환경 표식이 있으면 건너뛴다)."""
    root = make_project(
        {
            "shop/__init__.py": "",
            "shop/build/__init__.py": "",
            "shop/build/views.py": "def handler():\n    return 1\n",
            "env/__init__.py": "",
            "env/pyvenv.cfg": "home = /usr/bin\n",
            "env/lib/helper.py": "VALUE = 1\n",
            "build/lib/shop/__init__.py": "",
        }
    )
    project = Project.open(root)
    files = project.python_files()
    assert "shop/build/views.py" in files
    assert not any(path.startswith(("env/", "build/")) for path in files)
    assert project.skipped_source_directories == 0


def test_non_package_sources_in_skipped_names_are_counted(make_project: MakeProject) -> None:
    """패키지가 아닌데 `.py`를 직접 담은 건너뛰기 이름 디렉터리는 세어 graph 한계로 남긴다(점 이름은 세지 않는다)."""
    root = make_project(
        {
            "app.py": "def main():\n    return 1\n",
            "tools/build/make_release.py": "def release():\n    return 2\n",
            "dist/readme.txt": "artifacts\n",
            ".venv/lib/site.py": "X = 1\n",
        }
    )
    project = Project.open(root)
    assert project.skipped_source_directories == 1
    limitations = build_graph(project, False).limitations["direct"]
    assert any(
        line.startswith("scan-incomplete: 1 directories named build, dist, env, or venv") for line in limitations
    ), limitations


def test_routes_and_schema_report_skipped_source_directories(make_project: MakeProject) -> None:
    """routes·schema도 같은 공백을 각자의 한계 접두사로 싣는다."""
    root = make_project(
        {
            "app.py": "from flask import Flask\n\napp = Flask(__name__)\n",
            "venv/helpers.py": "def helper():\n    return 1\n",
        }
    )
    code, out, _ = run_cli(["routes", "--role", "server", "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0
    routes: dict[str, Any] = json.loads(out)
    assert any("directories named build, dist, env, or venv" in line for line in routes["limitations"])
    code, out, _ = run_cli(["schema", "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0
    schema: dict[str, Any] = json.loads(out)
    assert any("directories named build, dist, env, or venv" in line for line in schema["limitations"])


def test_namespace_package_under_skipped_name_is_counted(make_project: MakeProject) -> None:
    """`build/__init__.py` 없이 한 단계 아래에 패키지를 둔 namespace 배치는 세고, setuptools 산출물은 세지 않는다."""
    root = make_project(
        {
            "shop/__init__.py": "",
            "shop/build/handlers/__init__.py": "",
            "shop/build/handlers/views.py": "def handler():\n    return 1\n",
            "build/lib/mypkg/__init__.py": "",
            "build/lib.linux-x86_64-3.12/mypkg/__init__.py": "",
            "build/bdist.linux-x86_64/mypkg/__init__.py": "",
        }
    )
    assert Project.open(root).skipped_source_directories == 1


def test_large_artifact_directory_without_python_is_not_counted(make_project: MakeProject) -> None:
    """`.py` 없이 항목만 많은 `dist/`(512개 초과)는 소스를 담은 것으로 세지 않는다."""
    files = {f"dist/pkg-{index}.whl": "wheel\n" for index in range(600)}
    files["app.py"] = "def main():\n    return 1\n"
    assert Project.open(make_project(files)).skipped_source_directories == 0
