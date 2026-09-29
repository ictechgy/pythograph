"""Django REST framework 라우터(`SimpleRouter`·`DefaultRouter`)와 `format_suffix_patterns`를 정적으로 펼친다.

DRF 3.18.1 `rest_framework/routers.py`와 `rest_framework/urlpatterns.py`를 그대로 옮긴 규칙이다:

- 등록마다 목록 경로(`get`→`list`, `post`→`create`), `detail=False` action 경로, 상세 경로(`get`→`retrieve`,
  `put`→`update`, `patch`→`partial_update`, `delete`→`destroy`), `detail=True` action 경로 순서다. viewset에
  없는 action은 빼고, 남는 동사가 없으면 경로를 만들지 않는다(`get_method_map`).
- extra action은 `inspect.getmembers` 순서, 곧 이름순이다(`ViewSetMixin.get_extra_actions`).
- 정규식 모드(기본 `use_regex_path=True`): `^{prefix}{trailing_slash}$`, lookup은
  `(?P<{lookup_url_kwarg or lookup_field}>{lookup_value_regex or "[^/.]+"})`. 경로 모드는 `path()` 문법과
  `lookup_value_converter`(기본 `str`)다. prefix가 비면 앞 `/`를 뗀다.
- `DefaultRouter`는 끝에 API 루트(`path("", APIRootView)`, GET)를 더하고 모든 패턴에 형식 접미사
  변형을 원본 바로 뒤에 더한다(`\\.(?P<format>[a-z0-9]+)/?$`, 경로 모드는 `drf_format_suffix` 변환기).
"""

from __future__ import annotations

import ast
from dataclasses import replace

from pythograph.routes.classes import ActionInfo, ClassInfo, analyze_class, attribute_literal
from pythograph.routes.django.urlconf import (
    DrfBinding,
    Endpoint,
    Entry,
    Include,
    Opaque,
    Piece,
    RouterBuilder,
    RouterState,
    node_location,
)
from pythograph.routes.model import Location
from pythograph.source.evaluate import Evaluator
from pythograph.source.symbols import SymbolTable

#: 목록 경로의 동사 → action 연결이다.
LIST_MAPPING = (("get", "list"), ("post", "create"))

#: 상세 경로의 동사 → action 연결이다.
DETAIL_MAPPING = (("get", "retrieve"), ("put", "update"), ("patch", "partial_update"), ("delete", "destroy"))

#: 기본 형식 접미사 정규식(정규식 패턴용)이다.
SUFFIX_REGEX = r"\.(?P<format>[a-z0-9]+)/?$"

#: 경로 패턴용 형식 접미사 변환기 이름과 정규식이다(`_get_format_path_converter`).
SUFFIX_CONVERTER = "drf_format_suffix"
SUFFIX_CONVERTER_REGEX = r"\.[a-z0-9]+/?"

#: API 루트 뷰의 점 경로다.
API_ROOT_VIEW = "rest_framework.routers.APIRootView"


class DrfRouterBuilder(RouterBuilder):
    """DRF 라우터 패턴 생성기."""

    def __init__(self, symbols: SymbolTable, evaluator: Evaluator, converters: dict[str, str | None]) -> None:
        """생성기를 만든다.

        Args:
            symbols: 이름 해석기.
            evaluator: 상수 평가기.
            converters: 변환기 등록부(형식 접미사 변환기를 더한다).
        """
        self.symbols = symbols
        self.evaluator = evaluator
        self.converters = converters

    def expand(self, router: RouterState, conditional: bool) -> list[Entry]:
        """라우터의 현재 등록으로 패턴 목록을 만든다.

        Args:
            router: 라우터 상태(kind·trailing_slash·use_regex가 확정된 것).
            conditional: 조건부 등록인지.

        Returns:
            패턴 목록.
        """
        entries: list[Entry] = []
        for prefix, module, viewset, location in router.registrations:
            entries.extend(self._registration(router, prefix, module, viewset, location, conditional))
        if router.kind == "default":
            binding = DrfBinding(router.location.path, None, (("get", "get"),), API_ROOT_VIEW)
            entries.append(
                Endpoint(Piece("route", "", True), router.location.path, None, router.location, conditional, binding)
            )
            entries = self._apply_suffix(entries, None)
        return entries

    def suffix(self, entries: list[Entry], call: ast.Call, path: str) -> list[Entry] | None:
        """사용자가 부른 `format_suffix_patterns(urlpatterns, suffix_required=False, allowed=None)`을 적용한다.

        Args:
            entries: 패턴 목록.
            call: 호출 식.
            path: 모듈 경로.

        Returns:
            변환한 목록, 인자를 확정하지 못하면 None.
        """
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        extra = call.args[1:]
        required_node = keywords.get("suffix_required", extra[0] if extra else None)
        allowed_node = keywords.get("allowed", extra[1] if len(extra) > 1 else None)
        required = False if required_node is None else self.evaluator.value(path, required_node)
        allowed = None if allowed_node is None else self.evaluator.string_list(path, allowed_node)
        if not isinstance(required, bool) or (allowed_node is not None and allowed is None):
            return None
        return self._apply_suffix(entries, allowed, suffix_required=required)

    def _registration(
        self,
        router: RouterState,
        prefix: str | None,
        module: str,
        viewset: ast.expr | None,
        location: Location,
        conditional: bool,
    ) -> list[Entry]:
        """등록 하나의 경로를 만든다.

        Args:
            router: 라우터 상태.
            prefix: 등록 prefix(정규식 조각), 모르면 None.
            module: viewset 식이 있는 모듈.
            viewset: viewset 식.
            location: 등록 위치.
            conditional: 조건부 등록인지.

        Returns:
            패턴 목록(확정할 수 없으면 prefix 아래 불투명 항목).
        """
        info = analyze_class(
            self.symbols,
            self.symbols.resolve_expr(module, viewset) if isinstance(viewset, (ast.Name, ast.Attribute)) else None,
        )
        opaque = self._opaque_registration(prefix, info, location, conditional)
        if opaque is not None:
            return [opaque]
        assert info is not None and prefix is not None
        lookup = self._lookup(router, info)
        if lookup is None:
            return [
                Opaque(
                    "{count} Django REST framework viewsets use lookup settings that are not literal",
                    "route-coverage:",
                    location,
                    conditional,
                )
            ]
        actions = sorted(info.actions, key=lambda action: action.name)
        entries: list[Entry] = []
        for url, mapping, where in self._routes(router, info, actions, location):
            text = self._format(router, url, prefix, lookup)
            binding = DrfBinding(module, viewset, tuple(mapping))
            entries.append(
                Endpoint(
                    Piece("regex" if router.use_regex else "route", text, True),
                    module,
                    viewset,
                    where,
                    conditional,
                    binding,
                )
            )
        return entries

    def _opaque_registration(
        self, prefix: str | None, info: ClassInfo | None, location: Location, conditional: bool
    ) -> Entry | None:
        """등록을 확정할 수 없으면 prefix 아래 불투명 항목을 만든다.

        Args:
            prefix: 등록 prefix.
            info: viewset 분석 결과.
            location: 등록 위치.
            conditional: 조건부 등록인지.

        Returns:
            불투명 항목(include 포함) 또는 None(확정 가능).
        """
        if (
            info is not None
            and not info.unknown_bases
            and prefix is not None
            and all(action.complete for action in info.actions)
        ):
            return None
        opaque = Opaque(
            "{count} Django REST framework router registrations could not be resolved statically",
            "route-coverage:",
            location,
            conditional,
        )
        if prefix is None or not _is_plain_prefix(prefix):
            return opaque
        return Include(Piece("route", prefix + "/", False), (opaque,), conditional)

    def _lookup(self, router: RouterState, info: ClassInfo) -> str | None:
        """상세 경로 lookup 조각을 만든다(`get_lookup_regex`).

        Args:
            router: 라우터 상태.
            info: viewset 분석 결과.

        Returns:
            lookup 조각, 속성이 리터럴이 아니면 None.
        """
        field = _string_attribute(info, "lookup_field", "pk")
        kwarg = _string_attribute(info, "lookup_url_kwarg", None) or field
        if field is None or kwarg is None:
            return None
        if router.use_regex:
            value = _string_attribute(info, "lookup_value_regex", "[^/.]+")
            return None if value is None else f"(?P<{kwarg}>{value})"
        converter = _string_attribute(info, "lookup_value_converter", None)
        if converter is None and "lookup_value_converter" not in info.attributes:
            converter = _string_attribute(info, "lookup_value_regex", "str")
        return None if converter is None else f"<{converter}:{kwarg}>"

    def _routes(
        self, router: RouterState, info: ClassInfo, actions: list[ActionInfo], location: Location
    ) -> list[tuple[str, list[tuple[str, str]], Location]]:
        """등록 하나의 (URL 틀, 동사 연결, 위치) 목록을 DRF 순서대로 만든다.

        Args:
            router: 라우터 상태.
            info: viewset 분석 결과.
            actions: 이름순 extra action.
            location: 등록 위치.

        Returns:
            경로 목록.
        """
        del router
        routes: list[tuple[str, list[tuple[str, str]], Location]] = []
        list_mapping = [(method, action) for method, action in LIST_MAPPING if action in info.names]
        detail_mapping = [(method, action) for method, action in DETAIL_MAPPING if action in info.names]
        if list_mapping:
            routes.append(("^{prefix}{trailing_slash}$", list_mapping, location))
        routes.extend(self._action_routes(actions, False, "^{prefix}/{url_path}{trailing_slash}$"))
        if detail_mapping:
            routes.append(("^{prefix}/{lookup}{trailing_slash}$", detail_mapping, location))
        routes.extend(self._action_routes(actions, True, "^{prefix}/{lookup}/{url_path}{trailing_slash}$"))
        return routes

    def _action_routes(
        self, actions: list[ActionInfo], detail: bool, template: str
    ) -> list[tuple[str, list[tuple[str, str]], Location]]:
        """extra action 경로를 만든다.

        Args:
            actions: 이름순 action.
            detail: 상세 action만 고를지.
            template: URL 틀.

        Returns:
            경로 목록.
        """
        routes: list[tuple[str, list[tuple[str, str]], Location]] = []
        for action in actions:
            if action.detail is not detail or not action.methods or action.url_path is None:
                continue
            url = template.replace("{url_path}", action.url_path.replace("{", "{{").replace("}", "}}"))
            index = self.symbols.index(action.owner.path)
            has_bom = index.module.has_bom if index is not None else False
            where = node_location(action.owner.path, action.location_node, has_bom)
            routes.append((url, sorted(action.methods.items()), where))
        return routes

    @staticmethod
    def _format(router: RouterState, url: str, prefix: str, lookup: str) -> str:
        """URL 틀에 값을 넣는다(`str.format`과 같은 규칙, 경로 모드는 `^`·`$`를 뗀다).

        Args:
            router: 라우터 상태.
            url: URL 틀.
            prefix: 등록 prefix.
            lookup: lookup 조각.

        Returns:
            정규식 또는 경로 문자열.
        """
        template = url if router.use_regex else url[1:-1]
        text = template.format(prefix=prefix, lookup=lookup, trailing_slash=router.trailing_slash)
        if not prefix:
            if router.use_regex and text.startswith("^/"):
                text = "^" + text[2:]
            elif not router.use_regex and text.startswith("/"):
                text = text[1:]
        return text

    def _apply_suffix(
        self, entries: list[Entry], allowed: list[str] | None, suffix_required: bool = False
    ) -> list[Entry]:
        """`apply_suffix_patterns`: 각 endpoint 뒤에 형식 접미사 변형을 더한다(include는 안쪽에 적용).

        Args:
            entries: 패턴 목록.
            allowed: 허용 접미사 목록(None이면 `[a-z0-9]+`).
            suffix_required: 원본을 빼고 접미사 변형만 남길지.

        Returns:
            변환한 목록.
        """
        regex_suffix, converter = self._suffix_forms(allowed)
        result: list[Entry] = []
        for entry in entries:
            if isinstance(entry, Include):
                result.append(
                    replace(entry, children=tuple(self._apply_suffix(list(entry.children), allowed, suffix_required)))
                )
                continue
            if not isinstance(entry, Endpoint):
                result.append(entry)
                continue
            if not suffix_required:
                result.append(entry)
            result.append(_suffixed(entry, regex_suffix, converter))
        return result

    def _suffix_forms(self, allowed: list[str] | None) -> tuple[str, str]:
        """정규식 접미사와 경로 모드 변환기 이름을 만들고 변환기를 등록부에 더한다.

        Args:
            allowed: 허용 접미사 목록.

        Returns:
            (정규식 접미사, 변환기 이름).
        """
        if allowed:
            pattern = allowed[0] if len(allowed) == 1 else "({})".format("|".join(allowed))
            converter_pattern = allowed[0] if len(allowed) == 1 else "(?:{})".format("|".join(allowed))
            regex_suffix = r"\.(?P<format>" + pattern + r")/?$"
            name = SUFFIX_CONVERTER + "_" + "_".join(allowed)
            self.converters.setdefault(name, r"\." + converter_pattern + "/?")
            return regex_suffix, name
        self.converters.setdefault(SUFFIX_CONVERTER, SUFFIX_CONVERTER_REGEX)
        return SUFFIX_REGEX, SUFFIX_CONVERTER


def _suffixed(entry: Endpoint, regex_suffix: str, converter: str) -> Endpoint:
    """endpoint 하나의 형식 접미사 변형을 만든다.

    Args:
        entry: 원본 endpoint.
        regex_suffix: 정규식 접미사.
        converter: 경로 모드 변환기 이름.

    Returns:
        변형 endpoint(같은 뷰·위치).
    """
    text = entry.piece.text
    if text is None:
        return entry
    if entry.piece.kind == "regex":
        new_text = text.rstrip("$").rstrip("/") + regex_suffix
    else:
        new_text = text.rstrip("$").rstrip("/") + f"<{converter}:format>"
    return replace(entry, piece=replace(entry.piece, text=new_text))


def _string_attribute(info: ClassInfo, name: str, default: str | None) -> str | None:
    """클래스 속성을 문자열로 읽는다.

    Args:
        info: 클래스 분석 결과.
        name: 속성 이름.
        default: 속성이 없을 때 값.

    Returns:
        문자열, 속성이 리터럴 문자열이 아니면 None.
    """
    value = attribute_literal(info, name)
    if value is None and name not in info.attributes:
        return default
    return value if isinstance(value, str) else None


def _is_plain_prefix(prefix: str) -> bool:
    """정규식 특수 문자가 없는 prefix인지 확인한다.

    Args:
        prefix: 등록 prefix.

    Returns:
        리터럴로 볼 수 있으면 True.
    """
    return bool(prefix) and not any(character in prefix for character in "^$.|?*+()[]{}\\")
