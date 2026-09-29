"""`routes --role server` 실행: 프레임워크 판별 → 추출 → 버전 확인 → 결과.

프로젝트에 Django와 Flask가 모두 있으면 추측하지 않고 사용법 오류로 `--framework`를 요구한다. 둘 다 없으면
사실 0건과 `route-coverage:` 한계를 낸다(사실 0건은 완전성의 증거가 아니다).
"""

from __future__ import annotations

from dataclasses import dataclass

from pythograph.routes.django.extract import DjangoOptions, extract_django
from pythograph.routes.django.settings import discover_settings_modules
from pythograph.routes.flask.extract import FlaskOptions, extract_flask, flask_detected
from pythograph.routes.model import Extraction
from pythograph.routes.versions import declared_majors, normalize_name, version_gap
from pythograph.source.project import Project, ReadFailure
from pythograph.source.symbols import SymbolTable


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
    _project_gaps(project, extraction)
    return extraction


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
    if project.unencodable_names:
        extraction.add_gap(
            "route-coverage:",
            f"{project.unencodable_names} Python files have names that are not valid UTF-8 and were not analyzed",
        )
    if project.skipped_links:
        extraction.add_gap("route-coverage:", f"{project.skipped_links} symbolic links were not followed")
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
