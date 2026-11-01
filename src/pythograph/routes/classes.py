"""클래스 기반 뷰의 정적 분석: 상속 사슬, 정의된 메서드, 클래스 속성, 알려진 프레임워크 기반 클래스.

프로젝트 클래스는 소스에서, 알려진 프레임워크 클래스(Django generic view·`django.contrib.auth.views`, DRF generic
view·viewset·mixin, Flask `View`·`MethodView`)는 아래 표에서 메서드를 얻는다. 표의 값은 설치한 패키지를
직접 조사해 확인했다(Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3 — `docs/HTTP-ROUTES.md`).
표에 없는 외부 기반 클래스가 있으면 메서드 집합을 확정할 수 없다(`unknown_bases`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.source.evaluate import literal_value
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable

#: 상속 사슬을 따라가는 최대 깊이다.
MAX_CLASS_DEPTH = 32

#: 클래스 하나를 분석하며 같은 (클래스, 깊이)를 다시 펴는 횟수의 상한이다. 정적 상속 순환이 있으면 같은 클래스·깊이도
#: 지나온 경로에 따라 순환 가드가 다른 결과를 내 기억한 결과를 쓸 수 없고 다시 편다. 경로 수만큼 다시 펴는 순환 격자를
#: 막는 상한이며, 넘으면 남은 기반을 해석 실패로 둔다. 실제 계층의 순환은 이름을 가린 기반(`class View(View)`, 자기
#: 자신으로 풀린다) 정도라 다시 펴는 일이 거의 없다.
MAX_RELINEARIZATIONS = 1024

#: 프레임워크 클래스 표의 항목: (종류, 정의한 이름).
_FrameworkClass = tuple[str, frozenset[str]]

#: 펼친 상속 사슬의 항목: 프로젝트 클래스, 외부 클래스, 해석 실패(None).
_Entry = ProjectSymbol | ExternalSymbol | None


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
    for entry in _Linearizer(symbols).entries(symbol):
        if isinstance(entry, ProjectSymbol):
            _add_project(symbols, info, entry)
        elif isinstance(entry, ExternalSymbol):
            _add_external(info, entry.dotted)
        else:
            info.unknown_bases = True
    return info


@dataclass(frozen=True)
class _Order:
    """상속 사슬 한 단계를 펼친 결과와 그 결과가 기댄 순환 가드 판정.

    순환 가드는 지금 펴는 경로의 조상을 건너뛰므로, 같은 클래스·깊이라도 정적 상속 순환이 있으면 지나온 경로에 따라
    결과가 다르다. 펼치며 가드를 확인한 클래스 id와 그중 조상이라 건너뛴 id를 함께 남겨, 다른 경로에서도 판정이 같을
    때만 결과를 다시 쓴다.

    Attributes:
        entries: 펼친 클래스 목록(해석 실패는 None).
        checked: 펼치며 순환 가드를 확인한 클래스 id.
        skipped: checked 중 이 단계 바깥(조상)이라 건너뛴 id.
    """

    entries: tuple[_Entry, ...]
    checked: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()


#: 해석 실패 하나로 끝나는 펼침이다(깊이 상한을 넘었거나 다시 펴는 상한을 넘었다).
_UNKNOWN_ORDER = _Order((None,))


class _Linearizer:
    """상속 사슬을 왼쪽 우선 깊이 우선으로 펴고, 뒤에 다시 나오는 클래스는 뒤쪽 위치만 남긴다.

    C3 선형화의 근사다. 다이아몬드가 없는 흔한 뷰 계층에서는 C3과 같은 순서다. 기반마다 사슬을 다시 펴면 같은 두
    부모를 물려받는 다이아몬드 격자가 깊이 d에서 2^d번 재귀하므로 (클래스, 깊이)마다 결과를 기억한다. 깊이가 키에
    있는 이유는 MAX_CLASS_DEPTH 절단이 깊이에 따라 다른 결과를 내기 때문이다. `analyze_class` 호출 하나 동안 쓴다.
    """

    def __init__(self, symbols: SymbolTable) -> None:
        """계산기를 만든다.

        Args:
            symbols: 이름 해석기.
        """
        self.symbols = symbols
        self._memo: dict[tuple[ProjectSymbol, int], _Order] = {}
        self._active: set[str] = set()
        self._relinearized = 0
        self._exhausted = False

    def entries(self, symbol: ProjectSymbol) -> tuple[_Entry, ...]:
        """클래스의 상속 사슬을 펼친다.

        Args:
            symbol: 시작 클래스.

        Returns:
            클래스 목록(해석 실패는 None).
        """
        return self._linearize(symbol, 0).entries

    def _linearize(self, symbol: ProjectSymbol, depth: int) -> _Order:
        """클래스 하나의 사슬을 편다. 같은 클래스·깊이를 순환 가드 판정이 같은 경로에서 다시 만나면 기억한 결과를 쓴다.

        Args:
            symbol: 클래스.
            depth: 현재 깊이.

        Returns:
            펼친 결과. 깊이 상한을 넘으면 해석 실패 하나, 지금 경로의 조상이면 빈 결과다(순환 방지).
        """
        if depth > MAX_CLASS_DEPTH:
            return _UNKNOWN_ORDER
        if symbol.id in self._active:
            return _Order((), frozenset({symbol.id}), frozenset({symbol.id}))
        remembered = self._memo.get((symbol, depth))
        if remembered is not None and self._still_applies(remembered):
            return remembered
        if not self._may_expand(again=remembered is not None):
            return _UNKNOWN_ORDER
        order = self._expand(symbol, depth)
        if not self._exhausted:
            self._memo[(symbol, depth)] = order
        return order

    def _still_applies(self, remembered: _Order) -> bool:
        """기억한 결과가 지금 경로에서도 같은지 본다(가드를 확인한 클래스 중 지금 조상인 것이 그때와 같은지).

        Args:
            remembered: 기억한 결과.

        Returns:
            같으면 True.
        """
        return {name for name in self._active if name in remembered.checked} == remembered.skipped

    def _may_expand(self, again: bool) -> bool:
        """한 번 더 펼쳐도 되는지 정한다. 다시 펴는 횟수가 상한을 넘으면 그 뒤로는 어떤 클래스도 펴지 않는다.

        Args:
            again: 같은 클래스·깊이를 다시 펴는지.

        Returns:
            펼쳐도 되면 True.
        """
        if again and not self._exhausted:
            self._relinearized += 1
            self._exhausted = self._relinearized > MAX_RELINEARIZATIONS
        return not self._exhausted

    def _expand(self, symbol: ProjectSymbol, depth: int) -> _Order:
        """클래스 본문의 기반을 차례로 펼쳐 잇는다.

        Args:
            symbol: 클래스.
            depth: 현재 깊이.

        Returns:
            펼친 결과.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        parts = [_Order((symbol,), frozenset({symbol.id}))]
        self._active.add(symbol.id)
        try:
            parts += [self._base(symbol, base, depth) for base in node.bases]
        finally:
            self._active.discard(symbol.id)
        return _joined(parts, symbol.id)

    def _base(self, symbol: ProjectSymbol, base: ast.expr, depth: int) -> _Order:
        """기반 식 하나를 펼친다: 프로젝트 클래스면 그 사슬, 외부 클래스면 그것 하나, 모르면 해석 실패 하나.

        Args:
            symbol: 기반을 적은 클래스.
            base: 기반 식.
            depth: 기반을 적은 클래스의 깊이.

        Returns:
            펼친 결과.
        """
        resolved = self.symbols.resolve_expr(symbol.path, base)
        if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
            return self._linearize(resolved, depth + 1)
        return _Order((resolved if isinstance(resolved, ExternalSymbol) else None,))


def _joined(parts: list[_Order], own: str) -> _Order:
    """클래스 자신과 기반들의 펼친 결과를 잇는다.

    자기 id에서 걸린 가드는 그 클래스 안쪽 순환이라 바깥 경로 판정(`skipped`)에서 뺀다.

    Args:
        parts: 자신, 기반 순서의 펼친 결과.
        own: 클래스 id.

    Returns:
        이은 결과.
    """
    entries = _keep_last([entry for part in parts for entry in part.entries])
    checked = frozenset(name for part in parts for name in part.checked)
    skipped = frozenset(name for part in parts for name in part.skipped if name != own)
    return _Order(tuple(entries), checked, skipped)


def _keep_last(order: list[_Entry]) -> list[_Entry]:
    """같은 클래스가 여러 번 나오면 마지막 위치만 남긴다. 해석 실패(None)도 마지막 하나만 남긴다.

    `analyze_class`는 None을 만나면 표식(`unknown_bases`)만 켜므로 None의 개수·위치는 결과를 바꾸지 않는다. None을 모두
    남기면 해석 실패가 있는 다이아몬드 격자에서 단계마다 두 배로 불어난다. 뒤에서부터 한 번 훑어 길이에 선형이다.

    Args:
        order: 펼친 목록.

    Returns:
        정리한 목록.
    """
    seen: set[str | None] = set()
    kept: list[_Entry] = []
    for entry in reversed(order):
        key = _class_key(entry)
        if key not in seen:
            seen.add(key)
            kept.append(entry)
    kept.reverse()
    return kept


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
    detail = literal_value(keywords.get("detail"))
    methods_value = literal_value(keywords.get("methods")) if "methods" in keywords else ["get"]
    url_path_value = literal_value(keywords.get("url_path")) if "url_path" in keywords else function.name
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


def attribute_literal(info: ClassInfo, name: str) -> object:
    """클래스 속성 값을 리터럴로 읽는다.

    Args:
        info: 클래스 분석 결과.
        name: 속성 이름.

    Returns:
        값, 없으면 None, 리터럴이 아니면 `...`.
    """
    found = info.attributes.get(name)
    return None if found is None else literal_value(found[1])
