"""Celery 태스크 큐 호출과 Django 시그널 수신자: 큐에 넣는 호출은 callback 간선이고, 프레임워크 호출은 한계로 센다."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: Celery 태스크·시그널 수신자 합성 모듈이다.
TASKS = """
    from celery import Celery, shared_task
    from django.db.models.signals import post_save
    from django.dispatch import receiver

    app = Celery("shop")


    def render(x):
        return x


    @shared_task
    def send_email(x):
        return render(x)


    @app.task(bind=True)
    def rebuild(self, x):
        return render(x)


    def plain(x):
        return x


    def enqueue(x):
        send_email.delay(x)
        send_email.apply_async((x,))
        rebuild.s(x)
        return plain.delay(x)


    class Model:
        pass


    @receiver(post_save, sender=Model)
    def on_save(sender, **kwargs):
        return render(sender)
"""


def graph_for(make_project: MakeProject) -> CallGraph:
    """합성 프로젝트의 그래프를 만든다.

    Args:
        make_project: 합성 프로젝트 도우미.

    Returns:
        그래프.
    """
    return build_graph(Project.open(make_project({"tasks.py": TASKS})), False)


def test_task_queue_calls_are_callback_edges(make_project: MakeProject) -> None:
    """태스크의 `.delay`·`.apply_async`·`.s`는 그 태스크로 가는 direct `callback` 간선이다(평범한 함수는 아니다)."""
    graph = graph_for(make_project)
    callbacks = {
        (edge.source, edge.target) for edge in graph.edges if "callback" in edge.kinds and edge.evidence == "direct"
    }
    assert ("tasks.py#enqueue", "tasks.py#send_email") in callbacks
    assert ("tasks.py#enqueue", "tasks.py#rebuild") in callbacks
    assert ("tasks.py#enqueue", "tasks.py#plain") not in callbacks


def test_framework_entry_functions_are_reported(make_project: MakeProject) -> None:
    """프레임워크가 부르는 태스크·시그널 수신자는 뷰 핸들러가 없어도 `framework-dispatch:` 한계로 센다."""
    graph = graph_for(make_project)
    assert graph.statistics["frameworkEntries"] == {"celery-task": 2, "django-signal-receiver": 1}
    lines = [line for line in graph.limitations["direct"] if line.startswith("framework-dispatch:")]
    assert lines and "2 task-queue functions" in lines[0] and "1 Django signal receivers" in lines[0], lines


def test_unconfirmed_task_decorator_queue_call_is_counted(make_project: MakeProject) -> None:
    """Celery로 확인하지 못한 장식자(RQ `@job`)의 `.delay`는 callback 간선이 아니라 `framework-callback`으로 센다."""
    graph = build_graph(
        Project.open(
            make_project(
                {
                    "jobs.py": """
                        from rq.decorators import job


                        @job("default")
                        def notify(x):
                            return x


                        def enqueue(x):
                            return notify.delay(x)
                    """,
                }
            )
        ),
        False,
    )
    assert not [edge for edge in graph.edges if edge.source == "jobs.py#enqueue" and edge.target == "jobs.py#notify"]
    assert graph.node_map()["jobs.py#enqueue"].reasons.get("framework-callback") == 1
