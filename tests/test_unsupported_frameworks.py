"""pythograph가 라우트를 추출하지 않는 서버 프레임워크(FastAPI·Starlette 등)는 감지해 한계로 알린다."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.conftest import routes_document

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]


def test_fastapi_only_project_names_the_unsupported_framework(make_project: MakeProject) -> None:
    """FastAPI만 있는 프로젝트는 사실 0건이고, 한계가 Django·Flask가 없다는 말 대신 FastAPI를 짚는다."""
    root = make_project(
        {
            "main.py": """
                from fastapi import APIRouter, FastAPI

                app = FastAPI()
                router = APIRouter()


                @router.get("/items/{item_id}")
                def read_item(item_id: int):
                    return {"id": item_id}


                app.include_router(router, prefix="/v1")
            """,
        }
    )
    document = routes_document(root)
    assert document["facts"] == []
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any("FastAPI" in line and "not extracted" in line for line in limitations), limitations


def test_unsupported_framework_next_to_flask_is_reported(make_project: MakeProject) -> None:
    """Flask 앱 옆에 Starlette 모듈이 있으면 Flask 사실은 그대로 내고 Starlette 라우트 누락을 알린다."""
    root = make_project(
        {
            "app.py": """
                from flask import Flask

                app = Flask(__name__)


                @app.route("/health")
                def health():
                    return "ok"
            """,
            "asgi.py": """
                from starlette.applications import Starlette
                from starlette.routing import Route

                api = Starlette(routes=[Route("/ping", lambda request: None)])
            """,
        }
    )
    document = routes_document(root)
    facts = document["facts"]
    assert isinstance(facts, list) and [fact["channel"] for fact in facts] == ["/health"]
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(line.startswith("route-coverage: 1 modules import Starlette") for line in limitations), limitations


def test_flask_host_options_are_counted(make_project: MakeProject) -> None:
    """`subdomain`·`host`·`host_matching`·`redirect_to`는 사실이 담지 못하므로 경로 사실과 함께 한계로 센다."""
    root = make_project(
        {
            "app.py": """
                from flask import Blueprint, Flask

                app = Flask(__name__, host_matching=True, static_host="static.example")
                admin = Blueprint("admin", __name__, subdomain="admin")


                @app.route("/a", host="api.example")
                def api_a():
                    return "a"


                @app.route("/old", redirect_to="/new")
                def old():
                    return "old"


                @admin.route("/panel")
                def panel():
                    return "panel"


                @app.route("/plain")
                def plain():
                    return "plain"


                app.register_blueprint(admin, subdomain="staff")
            """,
        }
    )
    document = routes_document(root)
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(line.startswith("route-coverage: 5 Flask registrations use subdomain") for line in limitations), (
        limitations
    )
