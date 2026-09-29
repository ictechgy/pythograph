"""Django(+DRF) 프로젝트에서 route-decl을 추출한다.

흐름: 설정 모듈 → `ROOT_URLCONF` → URLconf 트리(`urlconf.py`, DRF 라우터는 `drf.py`) → 깊이 우선으로
펴서 등록 순서를 매김 → 각 endpoint의 경로 조각을 정규 템플릿으로(`pattern.py`), 뷰를 (method, 핸들러)로
(`views.py`) 바꿈 → 선언과 공백.

Django는 URL 패턴을 목록 순서대로 시도하고 처음 맞는 것을 쓴다(`URLResolver.resolve`). 그래서 문서는
`dispatch: "registration-order"`이고, 사실마다 `order: {group: "django:<ROOT_URLCONF>", index: <깊이 우선 순번>}`을
싣는다. 한 패턴에서 나온 사실(method·펼친 템플릿)은 같은 순번을 공유한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from pythograph.routes.classes import analyze_class
from pythograph.routes.django.drf import DrfRouterBuilder
from pythograph.routes.django.settings import DjangoSettings, load_settings
from pythograph.routes.django.urlconf import DrfBinding, Endpoint, Entry, Include, Opaque, Piece, UrlconfReader
from pythograph.routes.django.views import Handler, ViewResolution, ViewResolver, viewset_handler
from pythograph.routes.model import Extraction, RouteDecl, RouteOrder, RouteShape, ScopeRange
from pythograph.routes.pattern import (
    ExpansionCapped,
    Literal,
    Skeleton,
    Unconvertible,
    convert_regex,
    dynamic_shape,
    skeleton_shapes,
)
from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.project import is_test_path
from pythograph.source.symbols import SymbolTable

#: Django 기본 경로 변환기의 정규식이다(`django/urls/converters.py` `DEFAULT_CONVERTERS`).
DEFAULT_CONVERTERS: dict[str, str | None] = {
    "int": "[0-9]+",
    "path": ".+",
    "slug": "[-a-zA-Z0-9_]+",
    "str": "[^/]+",
    "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
}

#: `path()` 문자열의 변환기 자리 정규식이다(`django/urls/resolvers.py` `_PATH_PARAMETER_COMPONENT_RE`).
_PARAMETER_COMPONENT = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<parameter>[^>]+)>")


@dataclass(frozen=True)
class DjangoOptions:
    """Django 추출 옵션.

    Attributes:
        settings_module: `--settings` 값(없으면 진입 파일에서 찾는다).
        include_tests: 테스트 소스의 선언도 낼지.
        dispatch: 문서 dispatch(`registration-order` 또는 근사용 `specificity`).
    """

    settings_module: str | None
    include_tests: bool
    dispatch: str


@dataclass(frozen=True)
class _Flat:
    """깊이 우선으로 편 항목 하나."""

    pieces: tuple[Piece, ...]
    entry: Endpoint | Opaque
    conditional: bool


def extract_django(symbols: SymbolTable, options: DjangoOptions) -> Extraction:
    """Django 프로젝트의 route-decl을 추출한다.

    Args:
        symbols: 이름 해석기.
        options: 추출 옵션.

    Returns:
        추출 결과.
    """
    extraction = Extraction(dispatch=options.dispatch, framework="django")
    settings = load_settings(symbols, options.settings_module)
    root = settings.get("ROOT_URLCONF")
    root_path = symbols.project.resolve_module(root) if isinstance(root, str) else None
    if root_path is None:
        extraction.add_gap(
            "route-coverage:",
            "the Django ROOT_URLCONF could not be resolved statically, so no "
            "routes were extracted; pass --settings <module>",
        )
        return extraction
    apps = settings.get("INSTALLED_APPS")
    if isinstance(apps, list) and "rest_framework" in apps:
        extraction.packages.add("djangorestframework")
    evaluator = Evaluator(symbols, settings.values)
    converters = dict(DEFAULT_CONVERTERS)
    builder = DrfRouterBuilder(symbols, evaluator, converters)
    reader = UrlconfReader(symbols, evaluator, builder)
    patterns = reader.module_patterns(root_path)
    converters.update(reader.converters.regexes)
    if patterns is None:
        extraction.add_gap(
            "route-coverage:", "the Django ROOT_URLCONF module could not be read or defines no urlpatterns"
        )
        return extraction
    context = _Context(
        symbols, evaluator, converters, options, extraction, _anchor(settings, extraction), f"django:{root}"
    )
    for index, flat in enumerate(_flatten(patterns, (), False)):
        context.emit(flat, index)
    _settings_gaps(settings, extraction)
    return extraction


def _anchor(settings: DjangoSettings, extraction: Extraction) -> str:
    """경로 앵커를 정한다. `FORCE_SCRIPT_NAME`이 있으면 접두사를 확정하지 못한 것으로 본다.

    Args:
        settings: 읽은 설정.
        extraction: 공백을 더할 결과.

    Returns:
        `root` 또는 `base`.
    """
    value = settings.get("FORCE_SCRIPT_NAME")
    if value is None or (value is UNKNOWN and "FORCE_SCRIPT_NAME" not in settings.values):
        return "root"
    extraction.add_gap("unresolved-route-prefix:", "FORCE_SCRIPT_NAME sets a script prefix that is not modeled")
    return "base"


def _settings_gaps(settings: DjangoSettings, extraction: Extraction) -> None:
    """설정에서 알 수 있는 framework 제공 경로를 공백으로 더한다.

    `django.contrib.staticfiles`는 `DEBUG`의 `runserver`에서 `STATIC_URL` 아래 파일을 제공한다
    (`django/contrib/staticfiles/handlers.py`). 상대 `STATIC_URL`은 스크립트 접두사 기준이다.

    Args:
        settings: 읽은 설정.
        extraction: 공백을 더할 결과.
    """
    apps = settings.get("INSTALLED_APPS")
    if not isinstance(apps, list) or "django.contrib.staticfiles" not in apps:
        if not isinstance(apps, list):
            extraction.add_gap(
                "framework-provided-routes:",
                "INSTALLED_APPS could not be read, so apps that serve their own routes are unknown",
            )
        return
    static_url = settings.get("STATIC_URL")
    message = "django.contrib.staticfiles serves STATIC_URL files in DEBUG"
    if not isinstance(static_url, str):
        extraction.add_gap("framework-provided-routes:", message)
        return
    if "://" in static_url or static_url.startswith("//"):
        return
    prefix = "/" + static_url.strip("/")
    extraction.add_gap("framework-provided-routes:", message, ScopeRange(template_prefixes=(prefix,)))


class _Context:
    """편 항목을 선언·공백으로 바꾸는 공유 상태."""

    def __init__(
        self,
        symbols: SymbolTable,
        evaluator: Evaluator,
        converters: dict[str, str | None],
        options: DjangoOptions,
        extraction: Extraction,
        anchor: str,
        group: str,
    ) -> None:
        """상태를 만든다.

        Args:
            symbols: 이름 해석기.
            evaluator: 상수 평가기.
            converters: 변환기 이름 → 정규식.
            options: 추출 옵션.
            extraction: 채울 결과.
            anchor: 기본 경로 앵커.
            group: 등록 순서 그룹 이름.
        """
        self.symbols = symbols
        self.views = ViewResolver(symbols, evaluator)
        self.converters = converters
        self.options = options
        self.extraction = extraction
        self.anchor = anchor
        self.group = group

    def emit(self, flat: _Flat, index: int) -> None:
        """편 항목 하나를 처리한다.

        Args:
            flat: 편 항목.
            index: 깊이 우선 순번.
        """
        if isinstance(flat.entry, Opaque):
            self._opaque(flat)
            return
        endpoint = flat.entry
        test_source = is_test_path(endpoint.location.path)
        if test_source and not self.options.include_tests:
            return
        anchor = "base" if any(piece.kind == "locale" for piece in flat.pieces) else self.anchor
        shapes = self._shapes(flat.pieces)
        resolution = self._resolve(endpoint)
        if flat.conditional:
            self._gap_for("route-coverage:", "{count} URL patterns are registered under a condition", shapes, anchor)
            return
        self._record_shape_gaps(shapes, anchor)
        if resolution.uncertain:
            self._gap_for(
                "route-coverage:",
                "{count} views accept methods that could not be determined "
                "statically and were emitted as ANY or as their known methods",
                shapes,
                anchor,
            )
        order = RouteOrder(self.group, index) if self.options.dispatch == "registration-order" else None
        for shape in shapes:
            for handler in resolution.handlers:
                self._add_decl(handler, shape, anchor, endpoint, test_source, order)

    def _add_decl(
        self,
        handler: Handler,
        shape: RouteShape,
        anchor: str,
        endpoint: Endpoint,
        test_source: bool,
        order: RouteOrder | None,
    ) -> None:
        """선언 하나를 더한다. usr가 없으면 체인 전용 한계를 센다.

        Args:
            handler: 핸들러.
            shape: 경로 판정.
            anchor: 경로 앵커.
            endpoint: 원본 endpoint.
            test_source: 테스트 소스 여부.
            order: 등록 순서.
        """
        if handler.usr is None:
            self.extraction.add_gap(
                "missing-route-usrs:", "{count} route handlers are outside the project and have no pythograph symbol id"
            )
        self.extraction.decls.append(
            RouteDecl(
                method=handler.method,
                shape=shape,
                path_anchor=anchor,
                location=endpoint.location,
                qualified_name=handler.qualified_name,
                usr=handler.usr,
                test_source=test_source,
                order=order,
            )
        )

    def _resolve(self, endpoint: Endpoint) -> ViewResolution:
        """endpoint의 뷰를 (method, 핸들러)로 푼다.

        Args:
            endpoint: endpoint.

        Returns:
            해석 결과.
        """
        if endpoint.drf is not None:
            return self._drf_handlers(endpoint.drf)
        if endpoint.view is None:
            return ViewResolution((Handler("ANY", None, "unresolved-view"),), True)
        return self.views.resolve(endpoint.module, endpoint.view)

    def _drf_handlers(self, binding: DrfBinding) -> ViewResolution:
        """라우터가 연결한 viewset action의 핸들러를 만든다.

        Args:
            binding: 라우터 연결.

        Returns:
            해석 결과.
        """
        if binding.framework_view is not None:
            return ViewResolution((Handler("GET", None, f"{binding.framework_view}.get"),), False)
        info = analyze_class(
            self.symbols,
            self.symbols.resolve_expr(binding.viewset_path, binding.viewset) if binding.viewset is not None else None,
        )
        if info is None:
            return ViewResolution((Handler("ANY", None, "unresolved-view"),), True)
        handlers = tuple(
            viewset_handler(info, action, method.upper()) for method, action in binding.mapping if method != "head"
        )
        return ViewResolution(handlers, info.unknown_bases)

    def _shapes(self, pieces: tuple[Piece, ...]) -> list[RouteShape]:
        """경로 조각을 정규 템플릿 판정으로 바꾼다.

        Args:
            pieces: 루트부터의 조각.

        Returns:
            판정 목록.
        """
        raw = "/" + "".join(piece.text or "<dynamic>" for piece in pieces if piece.kind != "locale")
        try:
            alternatives: list[Skeleton] = [(Literal("/"),)]
            for piece in pieces:
                alternatives = _cross(alternatives, self._piece_alternatives(piece))
        except Unconvertible as error:
            return [dynamic_shape(raw, str(error))]
        return skeleton_shapes(alternatives, raw=raw, trailing_policy="strict")

    def _piece_alternatives(self, piece: Piece) -> list[Skeleton]:
        """조각 하나를 토큰열 대안으로 바꾼다.

        Args:
            piece: 경로 조각.

        Returns:
            토큰열 대안.

        Raises:
            Unconvertible: 확정할 수 없을 때.
        """
        if piece.kind == "locale":
            return [()]
        if piece.text is None:
            raise Unconvertible("a URL pattern string is not a literal")
        if piece.kind == "route":
            regex = route_to_regex(piece.text, piece.endpoint, self.converters)
            return list(convert_regex(regex, endpoint=piece.endpoint, anchored_by_fullmatch=False).alternatives)
        fullmatch = piece.endpoint and piece.text.endswith("$")
        return list(convert_regex(piece.text, endpoint=piece.endpoint, anchored_by_fullmatch=fullmatch).alternatives)

    def _record_shape_gaps(self, shapes: list[RouteShape], anchor: str) -> None:
        """dynamic 판정의 공백을 센다.

        Args:
            shapes: 판정 목록.
            anchor: 경로 앵커.
        """
        del anchor
        for shape in shapes:
            if shape.dynamic:
                capped = shape.coverage_reason is not None and "more than 16" in shape.coverage_reason
                prefix = "route-template-expansion-capped:" if capped else "route-coverage:"
                self.extraction.add_gap(
                    prefix, "{count} URL patterns are emitted as dynamic: " + str(shape.coverage_reason)
                )

    def _gap_for(self, prefix: str, message: str, shapes: list[RouteShape], anchor: str) -> None:
        """판정 템플릿으로 스코프를 만들 수 있으면 스코프 있는 공백을, 아니면 전체 공백을 더한다.

        Args:
            prefix: limitation 접두사.
            message: 문장 틀.
            shapes: 판정 목록.
            anchor: 경로 앵커.
        """
        templates = tuple(shape.channel for shape in shapes if not shape.dynamic and shape.channel)
        if anchor == "root" and templates and len(templates) == len(shapes):
            self.extraction.add_gap(prefix, message, ScopeRange(templates=templates))
        else:
            self.extraction.add_gap(prefix, message)

    def _opaque(self, flat: _Flat) -> None:
        """불투명 항목을 공백으로 바꾼다. 접두사가 정적이면 세그먼트 경계 접두사로 스코프를 둔다.

        Args:
            flat: 편 불투명 항목.
        """
        opaque = flat.entry
        assert isinstance(opaque, Opaque)
        prefixes = self._prefix_scope(flat.pieces)
        # 루트 접두사만이고 method 제한도 없으면 문서 전체 효과와 같으므로 스코프를 싣지 않는다.
        useful = prefixes and (prefixes != ("/",) or opaque.methods)
        scope = ScopeRange(template_prefixes=prefixes, methods=opaque.methods) if useful else None
        self.extraction.add_gap(opaque.prefix, opaque.reason, scope)

    def _prefix_scope(self, pieces: tuple[Piece, ...]) -> tuple[str, ...]:
        """include 접두사 조각을 세그먼트 경계 템플릿 접두사로 바꾼다.

        Args:
            pieces: 루트부터의 include 조각.

        Returns:
            접두사 목록, 확정할 수 없거나 base 앵커면 빈 튜플.
        """
        if self.anchor != "root" or any(piece.kind == "locale" for piece in pieces):
            return ()
        shapes = self._shapes(pieces)
        if any(shape.dynamic or shape.channel is None for shape in shapes):
            return ()
        return tuple(dict.fromkeys(_boundary_prefix(str(shape.channel)) for shape in shapes))


def route_to_regex(route: str, endpoint: bool, converters: dict[str, str | None]) -> str:
    """`path()` 문자열을 Django와 같은 규칙으로 정규식으로 바꾼다(`_route_to_regex`).

    Args:
        route: 경로 문자열.
        endpoint: 뷰에 닿는 패턴인지(끝에 `\\Z`).
        converters: 변환기 이름 → 정규식.

    Returns:
        정규식.

    Raises:
        Unconvertible: Django가 거부하는 문자열이거나 변환기를 알 수 없을 때.
    """
    parts = ["^"]
    previous = 0
    for match in _PARAMETER_COMPONENT.finditer(route):
        if any(character.isspace() for character in match.group(0)):
            raise Unconvertible("a path() route has whitespace inside <…>, which Django rejects")
        converter = match.group("converter") or "str"
        parameter = match.group("parameter")
        if not parameter.isidentifier():
            raise Unconvertible("a path() parameter name is not an identifier, which Django rejects")
        if converter not in converters:
            raise Unconvertible("a path() route uses a converter that is not registered statically")
        regex = converters[converter]
        if regex is None:
            raise Unconvertible("a registered path converter has a regex that is not a literal")
        parts.append(re.escape(route[previous : match.start()]))
        parts.append(f"(?P<{parameter}>{regex})")
        previous = match.end()
    parts.append(re.escape(route[previous:]))
    if endpoint:
        parts.append(r"\Z")
    return "".join(parts)


def _flatten(entries: tuple[Entry, ...], pieces: tuple[Piece, ...], conditional: bool) -> Iterator[_Flat]:
    """URL 패턴 트리를 깊이 우선(등록 순서)으로 편다.

    Args:
        entries: 항목 목록.
        pieces: 지금까지의 접두사 조각.
        conditional: 상위가 조건부인지.

    Yields:
        편 항목.
    """
    for entry in entries:
        is_conditional = conditional or entry.conditional
        if isinstance(entry, Include):
            yield from _flatten(entry.children, (*pieces, entry.piece), is_conditional)
        elif isinstance(entry, Endpoint):
            yield _Flat((*pieces, entry.piece), entry, is_conditional)
        else:
            yield _Flat(pieces, entry, is_conditional)


def _cross(left: list[Skeleton], right: list[Skeleton]) -> list[Skeleton]:
    """두 조각의 토큰열 대안을 모두 잇는다.

    Args:
        left: 앞 대안.
        right: 뒤 대안.

    Returns:
        이은 대안.

    Raises:
        ExpansionCapped: 16개를 넘을 때.
    """
    if len(left) * len(right) > 16:
        raise ExpansionCapped("URL pattern alternatives expand to more than 16 templates")
    return [first + second for first in left for second in right]


def _boundary_prefix(template: str) -> str:
    """템플릿을 세그먼트 경계 접두사로 자른다(`/admin/` → `/admin`, `/api` 뒤 이어 붙는 조각이면 `/`).

    Args:
        template: include 접두사 템플릿.

    Returns:
        `/`로 끝나지 않는 접두사(루트는 `/`).
    """
    trimmed = template[:-1] if template.endswith("/") else template[: template.rfind("/")]
    return trimmed or "/"
