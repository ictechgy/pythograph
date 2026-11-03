"""프레임워크가 부르는 함수: Celery 태스크와 Django 시그널 수신자.

Celery 태스크(`@shared_task`, `@app.task`)는 워커가, `@receiver(signal)` 함수는 Django 시그널 발송이 부르므로 프로젝트
호출자가 없다. 그래프는 그 호출을 모델링하지 않고 `framework-dispatch:` 한계로 센다. 프로젝트가 태스크를 큐에 넣는
호출(`task.delay(…)`, `task.apply_async(…)`, 서명을 만드는 `task.s(…)`·`si`·`signature`·`map`·`starmap`·`chunks`)은 그
태스크 본문이 나중에 실행된다는 뜻이므로 태스크로 가는 `callback` 간선이다. 태스크로 확인하지 못한 장식 함수의 같은
호출은 추측하지 않고 `framework-callback` 미해석 호출로 센다.
"""

from __future__ import annotations

import ast

from pythograph.graph.index import Definition
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue, ExternalValue, FunctionValue, MethodValue

#: 태스크를 큐에 넣거나 나중에 실행할 서명을 만드는 메서드 이름이다.
TASK_METHODS = frozenset({"delay", "apply_async", "s", "si", "signature", "map", "starmap", "chunks"})

#: 태스크를 만드는 Celery 장식자의 외부 점 경로다.
_CELERY_DECORATORS = frozenset({"celery.shared_task", "celery.task", "celery.app.shared_task"})

#: Celery 앱 인스턴스의 태스크 장식자 속성 이름이다(`@app.task`, `@celery_app.shared_task`).
_CELERY_APP_ATTRIBUTES = frozenset({"task", "shared_task"})

#: Django 시그널 수신자 장식자의 외부 점 경로다.
_SIGNAL_DECORATORS = frozenset({"django.dispatch.receiver", "django.dispatch.dispatcher.receiver"})

#: 태스크 종류 이름이다(집계 키).
CELERY_TASK = "celery-task"

#: 시그널 수신자 종류 이름이다(집계 키).
SIGNAL_RECEIVER = "django-signal-receiver"


class TaskClassifier:
    """함수 정의가 Celery 태스크·Django 시그널 수신자인지 장식자로 판정한다(정의마다 한 번)."""

    def __init__(self, resolver: Resolver) -> None:
        """판정기를 만든다.

        Args:
            resolver: 값 해석기.
        """
        self.resolver = resolver
        self._kinds: dict[str, str | None] = {}

    def kind(self, definition: Definition) -> str | None:
        """함수 정의의 프레임워크 진입 종류를 돌려준다.

        Args:
            definition: 함수·메서드 정의.

        Returns:
            `celery-task`·`django-signal-receiver`, 아니면 None.
        """
        if definition.id not in self._kinds:
            self._kinds[definition.id] = self._classify(definition)
        return self._kinds[definition.id]

    def _classify(self, definition: Definition) -> str | None:
        """같은 점 경로의 모든 정의 장식자를 본다.

        Args:
            definition: 정의.

        Returns:
            종류 또는 None.
        """
        if definition.parent is None or definition.kind not in ("function", "method"):
            return None
        for node in definition.nodes:
            for decorator in getattr(node, "decorator_list", []):
                found = self._decorator_kind(definition.parent, decorator)
                if found is not None:
                    return found
        return None

    def _decorator_kind(self, scope: Definition, decorator: ast.expr) -> str | None:
        """장식자 하나의 종류를 정한다.

        Args:
            scope: 장식자 식을 푸는 범위(정의의 바깥 범위).
            decorator: 장식자 식.

        Returns:
            종류 또는 None.
        """
        expr = decorator.func if isinstance(decorator, ast.Call) else decorator
        value = self.resolver.value(scope, expr)
        if isinstance(value, ExternalValue):
            if value.dotted in _CELERY_DECORATORS:
                return CELERY_TASK
            if value.dotted in _SIGNAL_DECORATORS:
                return SIGNAL_RECEIVER
        project_value = isinstance(value, (FunctionValue, ClassValue, MethodValue))
        if isinstance(expr, ast.Attribute) and expr.attr in _CELERY_APP_ATTRIBUTES and not project_value:
            return CELERY_TASK
        return None


def is_decorated(definition: Definition) -> bool:
    """정의 중 하나라도 장식자가 있는지 본다(장식하지 않은 함수에는 `.delay` 같은 속성이 없다).

    Args:
        definition: 정의.

    Returns:
        장식했으면 True.
    """
    return any(getattr(node, "decorator_list", []) for node in definition.nodes)
