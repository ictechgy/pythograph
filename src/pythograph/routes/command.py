"""`routes --role server` 실행: 프레임워크 판별 → 추출 → 버전 확인 → 결과.

프로젝트에 Django와 Flask가 모두 있으면 추측하지 않고 사용법 오류로 `--framework`를 요구한다. 둘 다 없으면
사실 0건과 `route-coverage:` 한계를 낸다(사실 0건은 완전성의 증거가 아니다).
"""

from __future__ import annotations

from dataclasses import dataclass

from pythograph.exchange.methods import ROUTE_DECL_METHODS
from pythograph.routes.django.extract import DjangoOptions, extract_django
from pythograph.routes.django.settings import discover_settings_modules
from pythograph.routes.flask.extract import FlaskOptions, extract_flask, flask_detected
from pythograph.routes.model import Extraction, RouteDecl, ScopeRange
from pythograph.routes.versions import declared_majors, normalize_name, version_gap
from pythograph.source.project import Project, ReadFailure, skipped_roots_reason
from pythograph.source.symbols import SymbolTable

#: 계약 동사 집합 밖 method를 받는 등록의 limitation 문장 틀이다.
_OUTSIDE_METHODS = (
    "{count} route registrations accept HTTP methods the exchange format does not allow; "
    "declarations for those methods were not emitted"
)


class FrameworkChoiceError(Exception):
    """프레임워크를 정할 수 없다(둘 다 있음 또는 지정한 프레임워크 없음). 사용법 오류로 보고한다."""


@dataclass(frozen=True)
class RouteOptions:
    """routes 명령 옵션.

    Attributes:
        framework: `auto`·`django`·`flask`.
        settings_module: Django 설정 모듈(없으면 None).
        include_tests: 테스트 소스를 포함할지.
        dispatch: 강제 dispatch(`specificity`) 또는 None(프레임워크 기본).
    """

    framework: str
    settings_module: str | None
    include_tests: bool
    dispatch: str | None


def extract_routes(project: Project, options: RouteOptions) -> Extraction:
    """프로젝트의 서버 라우트 선언을 추출한다.

    Args:
        project: 분석 대상 프로젝트.
        options: 명령 옵션.

    Returns:
        추출 결과.

    Raises:
        FrameworkChoiceError: 프레임워크를 정할 수 없을 때.
    """
    symbols = SymbolTable(project)
    framework = _choose_framework(symbols, options)
    if framework == "django":
        dispatch = options.dispatch or "registration-order"
        extraction = extract_django(symbols, DjangoOptions(options.settings_module, options.include_tests, dispatch))
    elif framework == "flask":
        extraction = extract_flask(symbols, FlaskOptions(options.include_tests))
    else:
        extraction = Extraction()
        extraction.add_gap(
            "route-coverage:",
            "no Django settings module or Flask application was found, so no routes were extracted; for a Django "
            "project whose manage.py, wsgi.py, and asgi.py do not name the settings module, pass --settings <module>",
        )
    _drop_methods_outside_contract(extraction)
    _project_gaps(project, extraction)
    return extraction


def _drop_methods_outside_contract(extraction: Extraction) -> None:
    """계약 동사 집합 밖 method의 선언을 빼고, 그런 등록마다 템플릿 스코프의 `route-coverage:`를 단다.

    Flask `methods`·Django `http_method_names`·viewset 매핑·장식자 인자는 임의 동사(`PURGE`·`PROPFIND`)를 받는다.
    isthmus는 route-decl `method`가 계약 동사나 `ANY`가 아니면 문서 전체를 입력 오류로 거부하므로(GRAPH-EXCHANGE
    http 사실 절) 그런 선언은 내지 않는다. `ANY`로 바꾸면 그 핸들러가 받지 않는 method의 호출까지 거짓 match가 되므로
    바꾸지 않고, 그 등록의 템플릿에 닿는 요청을 가릴 수 있다는 한계로 남긴다. 두 프레임워크 추출기가 같은 규칙을
    쓰도록 공통 지점인 여기서 거른다.

    Args:
        extraction: 고칠 추출 결과.
    """
    kept: list[RouteDecl] = []
    dropped: dict[tuple[object, ...], list[RouteDecl]] = {}
    for decl in extraction.decls:
        if decl.method in ROUTE_DECL_METHODS:
            kept.append(decl)
        else:
            dropped.setdefault((decl.location, decl.path_anchor, decl.order), []).append(decl)
    extraction.decls = kept
    for decls in dropped.values():
        extraction.add_gap("route-coverage:", _OUTSIDE_METHODS, _templates_scope(decls))


def _templates_scope(decls: list[RouteDecl]) -> ScopeRange | None:
    """한 등록에서 뺀 선언들의 템플릿으로 한계 스코프를 만든다.

    스코프 `methods`에는 계약 밖 동사를 쓸 수 없으므로 템플릿만 싣는다. 템플릿을 증명하지 못한 등록(dynamic·base
    앵커)은 상한을 적을 수 없어 스코프를 생략한다(문서 전체 효과).

    Args:
        decls: 같은 등록(위치·앵커·등록 순번)에서 뺀 선언.

    Returns:
        템플릿 스코프 또는 None.
    """
    if any(decl.path_anchor != "root" or decl.shape.dynamic or not decl.shape.channel for decl in decls):
        return None
    return ScopeRange(templates=tuple(sorted({str(decl.shape.channel) for decl in decls})))


def _choose_framework(symbols: SymbolTable, options: RouteOptions) -> str | None:
    """추출할 프레임워크를 정한다.

    Args:
        symbols: 이름 해석기.
        options: 명령 옵션.

    Returns:
        `django`·`flask` 또는 None(둘 다 없음).

    Raises:
        FrameworkChoiceError: 둘 다 있거나 지정한 프레임워크가 없을 때.
    """
    if options.framework != "auto":
        return options.framework
    has_django = options.settings_module is not None or bool(discover_settings_modules(symbols))
    has_flask = flask_detected(symbols, options.include_tests)
    if has_django and has_flask:
        raise FrameworkChoiceError(
            "the project contains both Django and Flask applications; pass --framework django or --framework flask"
        )
    if has_django:
        return "django"
    return "flask" if has_flask else None


def _project_gaps(project: Project, extraction: Extraction) -> None:
    """순회 상한·링크·버전처럼 프로젝트 수준 공백을 더한다.

    Args:
        project: 프로젝트.
        extraction: 채울 결과.
    """
    if project.scan_capped:
        extraction.add_gap("route-coverage:", "the project scan stopped at its entry or depth limit")
    if project.scan_errors:
        extraction.add_gap(
            "route-coverage:",
            f"{project.scan_errors} directory entries could not be read; the project scan is incomplete",
        )
    if project.unencodable_names:
        extraction.add_gap(
            "route-coverage:",
            f"{project.unencodable_names} Python files have names that are not valid UTF-8 and were not analyzed",
        )
    if project.forbidden_names:
        extraction.add_gap(
            "route-coverage:",
            f"{project.forbidden_names} Python files have names with characters the exchange format forbids "
            "and were not analyzed",
        )
    if project.skipped_links:
        extraction.add_gap("route-coverage:", f"{project.skipped_links} symbolic links were not followed")
    if project.skipped_module_roots:
        extraction.add_gap("route-coverage:", skipped_roots_reason(project.skipped_module_roots))
    _failed_modules(project, extraction)
    if extraction.framework is None:
        return
    majors = declared_majors(project)
    checks = [("django", 5, "Django")] if extraction.framework == "django" else [("flask", 3, "Flask")]
    if "djangorestframework" in extraction.packages:
        checks.append(("djangorestframework", 3, "Django REST framework"))
    for package, expected, label in checks:
        message = version_gap(majors, normalize_name(package), expected, label)
        if message is not None:
            extraction.add_gap("route-framework-version-unknown:", message)


def _failed_modules(project: Project, extraction: Extraction) -> None:
    """파싱에 실패한 모듈을 이유별로 센다(경로는 싣지 않는다).

    Args:
        project: 프로젝트.
        extraction: 채울 결과.
    """
    for path, module in sorted(project.parsed_modules().items()):
        if isinstance(module, ReadFailure):
            del path
            extraction.add_gap("route-coverage:", "{count} Python files could not be analyzed: " + module.reason)
