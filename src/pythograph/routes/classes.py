"""클래스 기반 뷰의 정적 분석: 상속 사슬, 정의된 메서드, 클래스 속성, 알려진 프레임워크 기반 클래스.

프로젝트 클래스는 소스에서, 알려진 프레임워크 클래스(Django generic view·`django.contrib.auth.views`, DRF generic
view·viewset·mixin, Flask `View`·`MethodView`)는 아래 표에서 메서드를 얻는다. 표의 값은 설치한 패키지를
직접 조사해 확인했다(Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3 — `docs/HTTP-ROUTES.md`).
표에 없는 외부 기반 클래스가 있으면 메서드 집합을 확정할 수 없다(`unknown_bases`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable

#: 상속 사슬을 따라가는 최대 깊이다.
MAX_CLASS_DEPTH = 32

#: 프레임워크 클래스 표의 항목: (종류, 정의한 이름).
_FrameworkClass = tuple[str, frozenset[str]]


def _entry(kind: str, *names: str) -> _FrameworkClass:
    """표 항목을 만든다.

    Args:
        kind: 뷰 종류(`django-view`·`drf-apiview`·`drf-viewset`·`drf-mixin`·`flask-view`·
            `flask-methodview`·`mixin`).
        names: 클래스가 정의한 메서드 이름.

    Returns:
        표 항목.
    """
    return kind, frozenset(names)


#: Django `django.views.generic` 계열 클래스(마지막 이름 기준)와 정의한 핸들러다.
DJANGO_VIEW_CLASSES: dict[str, _FrameworkClass] = {
    "View": _entry("django-view", "options"),
    "TemplateView": _entry("django-view", "get", "options"),
    "RedirectView": _entry("django-view", "get", "head", "post", "options", "delete", "put", "patch"),
    "ListView": _entry("django-view", "get", "options"),
    "DetailView": _entry("django-view", "get", "options"),
    "FormView": _entry("django-view", "get", "post", "put", "options"),
    "CreateView": _entry("django-view", "get", "post", "put", "options"),
    "UpdateView": _entry("django-view", "get", "post", "put", "options"),
    "DeleteView": _entry("django-view", "get", "post", "delete", "options"),
    "ArchiveIndexView": _entry("django-view", "get", "options"),
    "YearArchiveView": _entry("django-view", "get", "options"),
    "MonthArchiveView": _entry("django-view", "get", "options"),
    "WeekArchiveView": _entry("django-view", "get", "options"),
    "DayArchiveView": _entry("django-view", "get", "options"),
    "TodayArchiveView": _entry("django-view", "get", "options"),
    "DateDetailView": _entry("django-view", "get", "options"),
    "BaseDetailView": _entry("django-view", "get", "options"),
    "BaseListView": _entry("django-view", "get", "options"),
    "ProcessFormView": _entry("django-view", "get", "post", "put", "options"),
    "BaseFormView": _entry("django-view", "get", "post", "put", "options"),
    "BaseCreateView": _entry("django-view", "get", "post", "put", "options"),
    "BaseUpdateView": _entry("django-view", "get", "post", "put", "options"),
    "BaseDeleteView": _entry("django-view", "get", "post", "delete", "options"),
    "BaseDateListView": _entry("django-view", "get", "options"),
    "BaseArchiveIndexView": _entry("django-view", "get", "options"),
    "BaseYearArchiveView": _entry("django-view", "get", "options"),
    "BaseMonthArchiveView": _entry("django-view", "get", "options"),
    "BaseWeekArchiveView": _entry("django-view", "get", "options"),
    "BaseDayArchiveView": _entry("django-view", "get", "options"),
    "BaseTodayArchiveView": _entry("django-view", "get", "options"),
    "BaseDateDetailView": _entry("django-view", "get", "options"),
    "DeletionMixin": _entry("mixin", "post", "delete"),
}

#: Django `django.contrib.auth.views` 클래스와 정의한(물려받은) 핸들러다. Django 5.2.17 설치본에서 `hasattr`로 조사했다.
DJANGO_AUTH_VIEW_CLASSES: dict[str, _FrameworkClass] = {
    "LoginView": _entry("django-view", "get", "post", "put", "options"),
    "LogoutView": _entry("django-view", "get", "post", "options"),
    "PasswordChangeView": _entry("django-view", "get", "post", "put", "options"),
    "PasswordChangeDoneView": _entry("django-view", "get", "options"),
    "PasswordResetView": _entry("django-view", "get", "post", "put", "options"),
    "PasswordResetDoneView": _entry("django-view", "get", "options"),
    "PasswordResetConfirmView": _entry("django-view", "get", "post", "put", "options"),
    "PasswordResetCompleteView": _entry("django-view", "get", "options"),
}

#: 프레임워크 클래스가 선언한 `http_method_names`(외부 점 경로 → 소문자 동사)다. 사슬 앞의 프로젝트 선언이 이긴다.
#: Django 5.2.17 `LogoutView.http_method_names = ["post", "options"]`(GET 로그아웃 제거).
FRAMEWORK_HTTP_METHOD_NAMES: dict[str, tuple[str, ...]] = {
    "django.contrib.auth.views.LogoutView": ("post", "options"),
}

#: 핸들러를 정의하지 않고 dispatch 등만 바꾸는 알려진 믹스인(외부 점 경로)이다.
TRANSPARENT_MIXINS = frozenset(
    {
        "builtins.object",
        "object",
        "abc.ABC",
        "typing.Generic",
        "django.contrib.auth.mixins.LoginRequiredMixin",
        "django.contrib.auth.mixins.PermissionRequiredMixin",
        "django.contrib.auth.mixins.UserPassesTestMixin",
        "django.contrib.auth.mixins.AccessMixin",
        "django.contrib.auth.views.RedirectURLMixin",
        "django.contrib.auth.views.PasswordContextMixin",
        "django.contrib.messages.views.SuccessMessageMixin",
        "django.views.generic.base.ContextMixin",
        "django.views.generic.base.TemplateResponseMixin",
        "django.views.generic.detail.SingleObjectMixin",
        "django.views.generic.list.MultipleObjectMixin",
        "django.views.generic.edit.FormMixin",
        "django.views.generic.edit.ModelFormMixin",
        "django.views.generic.detail.SingleObjectTemplateResponseMixin",
        "django.views.generic.list.MultipleObjectTemplateResponseMixin",
        "django.views.generic.dates.YearMixin",
        "django.views.generic.dates.MonthMixin",
        "django.views.generic.dates.WeekMixin",
        "django.views.generic.dates.DayMixin",
        "django.views.generic.dates.DateMixin",
    }
)

#: DRF 클래스(마지막 이름 기준)와 정의한 핸들러·action이다.
DRF_CLASSES: dict[str, _FrameworkClass] = {
    "APIView": _entry("drf-apiview", "options"),
    "GenericAPIView": _entry("drf-apiview", "options"),
    "CreateAPIView": _entry("drf-apiview", "post", "options"),
    "ListAPIView": _entry("drf-apiview", "get", "options"),
    "RetrieveAPIView": _entry("drf-apiview", "get", "options"),
    "DestroyAPIView": _entry("drf-apiview", "delete", "options"),
    "UpdateAPIView": _entry("drf-apiview", "put", "patch", "options"),
    "ListCreateAPIView": _entry("drf-apiview", "get", "post", "options"),
    "RetrieveUpdateAPIView": _entry("drf-apiview", "get", "put", "patch", "options"),
    "RetrieveDestroyAPIView": _entry("drf-apiview", "get", "delete", "options"),
    "RetrieveUpdateDestroyAPIView": _entry("drf-apiview", "get", "put", "patch", "delete", "options"),
    "ViewSetMixin": _entry("drf-viewset"),
    "ViewSet": _entry("drf-viewset", "options"),
    "GenericViewSet": _entry("drf-viewset", "options"),
    "ModelViewSet": _entry(
        "drf-viewset", "list", "create", "retrieve", "update", "partial_update", "destroy", "options"
    ),
    "ReadOnlyModelViewSet": _entry("drf-viewset", "list", "retrieve", "options"),
    "ListModelMixin": _entry("mixin", "list"),
    "CreateModelMixin": _entry("mixin", "create"),
    "RetrieveModelMixin": _entry("mixin", "retrieve"),
    "UpdateModelMixin": _entry("mixin", "update", "partial_update"),
    "DestroyModelMixin": _entry("mixin", "destroy"),
}

#: Flask 클래스 기반 뷰다.
FLASK_CLASSES: dict[str, _FrameworkClass] = {
    "View": _entry("flask-view"),
    "MethodView": _entry("flask-methodview"),
}

#: 알려진 프레임워크 클래스를 찾을 때 허용하는 모듈 접두사다.
_FRAMEWORK_MODULES = (
    ("django.views", DJANGO_VIEW_CLASSES),
    ("django.contrib.auth.views", DJANGO_AUTH_VIEW_CLASSES),
    ("rest_framework", DRF_CLASSES),
    ("flask.views", FLASK_CLASSES),
    ("flask.sansio.views", FLASK_CLASSES),
)


@dataclass(frozen=True)
class ActionInfo:
    """DRF `@action`으로 표시한 viewset 메서드.

    Attributes:
        name: 메서드 이름(action 이름).
        detail: 상세 경로인지. 확정하지 못하면 None.
        methods: 소문자 HTTP 동사 → 처리 함수 이름(`mapping`).
        url_path: 경로 조각(기본은 메서드 이름).
        location_node: 위치로 쓸 노드(`@action` 장식자).
        owner: 메서드를 정의한 프로젝트 클래스.
        complete: 인자를 모두 정적으로 읽었는지.
    """

    name: str
    detail: bool | None
    methods: dict[str, str]
    url_path: str | None
    location_node: ast.AST
    owner: ProjectSymbol
    complete: bool


@dataclass
class ClassInfo:
    """클래스 하나의 분석 결과.

    Attributes:
        symbol: 분석한 클래스(프로젝트 또는 외부).
        names: 상속 사슬 전체에서 정의한 메서드·속성 이름.
        attributes: 속성 이름 → (정의한 모듈 경로, 값 식). 사슬에서 먼저 나오는 정의가 이긴다.
        kinds: 사슬에서 만난 프레임워크 뷰 종류.
        actions: DRF extra action 목록(사슬 순서).
        unknown_bases: 표에 없는 외부 기반 클래스나 해석하지 못한 기반이 있는지.
    """

    symbol: ProjectSymbol | ExternalSymbol
    names: set[str] = field(default_factory=set)
    attributes: dict[str, tuple[str, ast.expr]] = field(default_factory=dict)
    kinds: set[str] = field(default_factory=set)
    actions: list[ActionInfo] = field(default_factory=list)
    unknown_bases: bool = False


def framework_class(dotted: str) -> _FrameworkClass | None:
    """외부 점 경로가 알려진 프레임워크 클래스면 표 항목을 돌려준다.

    Args:
        dotted: 외부 점 경로(`rest_framework.generics.ListAPIView` 등 재수출 표기 포함).

    Returns:
        표 항목 또는 None.
    """
    name = dotted.rsplit(".", 1)[-1]
    for prefix, table in _FRAMEWORK_MODULES:
        if (dotted == prefix or dotted.startswith(prefix + ".")) and name in table:
            return table[name]
    return None


def analyze_class(symbols: SymbolTable, symbol: Symbol | None) -> ClassInfo | None:
    """클래스의 상속 사슬을 따라 메서드·속성·action을 모은다.

    Args:
        symbols: 이름 해석기.
        symbol: 클래스 해석 결과.

    Returns:
        분석 결과, 클래스가 아니면 None.
    """
    if isinstance(symbol, ExternalSymbol):
        info = ClassInfo(symbol=symbol)
        _add_external(info, symbol.dotted)
        return info
    if not isinstance(symbol, ProjectSymbol) or not isinstance(symbol.node, ast.ClassDef):
        return None
    info = ClassInfo(symbol=symbol)
    for entry in _linearize(symbols, symbol, 0, set()):
        if isinstance(entry, ProjectSymbol):
            _add_project(symbols, info, entry)
        elif isinstance(entry, ExternalSymbol):
            _add_external(info, entry.dotted)
        else:
            info.unknown_bases = True
    return info


def _linearize(
    symbols: SymbolTable, symbol: ProjectSymbol, depth: int, seen: set[str]
) -> list[ProjectSymbol | ExternalSymbol | None]:
    """상속 사슬을 왼쪽 우선 깊이 우선으로 펴고, 뒤에 다시 나오는 클래스는 뒤쪽 위치만 남긴다.

    C3 선형화의 근사다. 다이아몬드가 없는 흔한 뷰 계층에서는 C3과 같은 순서다.

    Args:
        symbols: 이름 해석기.
        symbol: 시작 클래스.
        depth: 현재 깊이.
        seen: 순환 방지용 id 집합.

    Returns:
        클래스 목록(해석 실패는 None).
    """
    if depth > MAX_CLASS_DEPTH or symbol.id in seen:
        return [None] if depth > MAX_CLASS_DEPTH else []
    node = symbol.node
    assert isinstance(node, ast.ClassDef)
    order: list[ProjectSymbol | ExternalSymbol | None] = [symbol]
    for base in node.bases:
        resolved = symbols.resolve_expr(symbol.path, base)
        if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
            order.extend(_linearize(symbols, resolved, depth + 1, seen | {symbol.id}))
        elif isinstance(resolved, ExternalSymbol):
            order.append(resolved)
        else:
            order.append(None)
    return _keep_last(order)


def _keep_last(order: list[ProjectSymbol | ExternalSymbol | None]) -> list[ProjectSymbol | ExternalSymbol | None]:
    """같은 클래스가 여러 번 나오면 마지막 위치만 남긴다(None은 모두 남긴다).

    Args:
        order: 펼친 목록.

    Returns:
        정리한 목록.
    """
    keys = [_class_key(entry) for entry in order]
    return [entry for index, entry in enumerate(order) if keys[index] is None or keys[index] not in keys[index + 1 :]]


def _class_key(entry: ProjectSymbol | ExternalSymbol | None) -> str | None:
    """중복 판정용 키를 만든다.

    Args:
        entry: 클래스.

    Returns:
        키 또는 None.
    """
    if isinstance(entry, ProjectSymbol):
        return entry.id
    if isinstance(entry, ExternalSymbol):
        return entry.dotted
    return None


def _add_external(info: ClassInfo, dotted: str) -> None:
    """외부 기반 클래스의 이름을 표에서 더한다. 표에 없으면 확정 불가로 표시한다.

    Args:
        info: 채울 결과.
        dotted: 외부 점 경로.
    """
    if dotted in TRANSPARENT_MIXINS:
        return
    known = framework_class(dotted)
    if known is None:
        info.unknown_bases = True
        return
    kind, names = known
    info.kinds.add(kind)
    info.names.update(names)
    methods = FRAMEWORK_HTTP_METHOD_NAMES.get(dotted)
    if methods is not None:
        value = ast.List(elts=[ast.Constant(method) for method in methods], ctx=ast.Load())
        info.attributes.setdefault("http_method_names", (dotted, value))


def _add_project(symbols: SymbolTable, info: ClassInfo, symbol: ProjectSymbol) -> None:
    """프로젝트 클래스 본문의 메서드·속성·action을 더한다.

    Args:
        symbols: 이름 해석기.
        info: 채울 결과.
        symbol: 프로젝트 클래스.
    """
    node = symbol.node
    assert isinstance(node, ast.ClassDef)
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.names.add(statement.name)
            action = _action_info(symbols, symbol, statement)
            if action is not None and all(existing.name != action.name for existing in info.actions):
                info.actions.append(action)
            _merge_mapping(symbols, info, symbol, statement)
        for name, value in _class_assignments(statement):
            info.names.add(name)
            info.attributes.setdefault(name, (symbol.path, value))


def _class_assignments(statement: ast.stmt) -> list[tuple[str, ast.expr]]:
    """클래스 본문 대입문의 (이름, 값)을 돌려준다.

    Args:
        statement: 클래스 본문 문장.

    Returns:
        (이름, 값 식) 목록.
    """
    if isinstance(statement, ast.Assign):
        return [(target.id, statement.value) for target in statement.targets if isinstance(target, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
        return [(statement.target.id, statement.value)]
    return []


def _action_info(
    symbols: SymbolTable, owner: ProjectSymbol, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> ActionInfo | None:
    """`@action(...)` 장식자를 읽는다.

    DRF `rest_framework/decorators.py` `action`: `methods` 기본 `["get"]`, `url_path` 기본 함수 이름,
    `detail`은 필수다. 동사는 소문자로 `mapping`에 들어간다.

    Args:
        symbols: 이름 해석기.
        owner: 메서드를 정의한 클래스.
        function: 메서드 정의.

    Returns:
        action 정보, `@action`이 아니면 None.
    """
    for decorator in function.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        resolved = symbols.resolve_expr(owner.path, target)
        if not isinstance(resolved, ExternalSymbol) or resolved.dotted not in (
            "rest_framework.decorators.action",
            "rest_framework.decorators.detail_route",
        ):
            continue
        return _read_action(owner, function, decorator, call)
    return None


def _read_action(
    owner: ProjectSymbol, function: ast.FunctionDef | ast.AsyncFunctionDef, decorator: ast.expr, call: ast.Call | None
) -> ActionInfo:
    """`@action` 호출 인자를 정적으로 읽는다.

    Args:
        owner: 메서드를 정의한 클래스.
        function: 메서드 정의.
        decorator: 장식자 식.
        call: 장식자 호출(인자 없는 장식자면 None).

    Returns:
        action 정보.
    """
    keywords = {keyword.arg: keyword.value for keyword in (call.keywords if call else []) if keyword.arg}
    complete = call is not None and not call.args and all(keyword.arg for keyword in call.keywords)
    detail = _literal(keywords.get("detail"))
    methods_value = _literal(keywords.get("methods")) if "methods" in keywords else ["get"]
    url_path_value = _literal(keywords.get("url_path")) if "url_path" in keywords else function.name
    if not isinstance(detail, bool):
        complete = False
    methods: dict[str, str] = {}
    if isinstance(methods_value, list) and all(isinstance(method, str) for method in methods_value):
        methods = {str(method).lower(): function.name for method in methods_value}
    else:
        complete = False
    if not isinstance(url_path_value, str):
        complete = False
    return ActionInfo(
        name=function.name,
        detail=detail if isinstance(detail, bool) else None,
        methods=methods,
        url_path=url_path_value if isinstance(url_path_value, str) else None,
        location_node=decorator,
        owner=owner,
        complete=complete,
    )


def _merge_mapping(
    symbols: SymbolTable, info: ClassInfo, owner: ProjectSymbol, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> None:
    """`@<action>.mapping.<method>` 장식자로 추가한 동사를 해당 action에 더한다.

    Args:
        symbols: 이름 해석기.
        info: 채울 결과.
        owner: 정의한 클래스.
        function: 메서드 정의.
    """
    del symbols, owner
    for decorator in function.decorator_list:
        if (
            isinstance(decorator, ast.Attribute)
            and isinstance(decorator.value, ast.Attribute)
            and decorator.value.attr == "mapping"
            and isinstance(decorator.value.value, ast.Name)
        ):
            action_name = decorator.value.value.id
            for action in info.actions:
                if action.name == action_name:
                    action.methods[decorator.attr.lower()] = function.name


def _literal(node: ast.expr | None) -> object:
    """리터럴 식만 값으로 바꾼다(이름은 따라가지 않는다).

    Args:
        node: 식.

    Returns:
        값, 리터럴이 아니면 `...`(Ellipsis) 표식.
    """
    if node is None:
        return ...
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return ...


def attribute_literal(info: ClassInfo, name: str) -> object:
    """클래스 속성 값을 리터럴로 읽는다.

    Args:
        info: 클래스 분석 결과.
        name: 속성 이름.

    Returns:
        값, 없으면 None, 리터럴이 아니면 `...`.
    """
    found = info.attributes.get(name)
    return None if found is None else _literal(found[1])
