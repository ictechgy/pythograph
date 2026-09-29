"""라우트 추출의 내부 데이터 모델.

프레임워크 추출기(Django·Flask)는 이 모델로 선언과 공백을 내고, 문서 조립기
(`pythograph.exchange.document`)가 bridge-facts v1 JSON으로 바꾼다. 추출기와 직렬화를 나눠
프레임워크 규칙이 교환 형식의 세부(키 이름·정렬·상한)에 섞이지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Location:
    """사실 위치. 프로젝트 상대 POSIX 경로, 1부터 시작하는 줄과 UTF-8 바이트 열이다."""

    path: str
    line: int
    column: int


@dataclass(frozen=True)
class ParamConstraint:
    """경로 파라미터 제약(`paramConstraints` 원소).

    `segment`는 템플릿 세그먼트의 0부터 시작하는 인덱스이고, `kind`는 `int`·`uuid`·`slug`·`path`·
    `regex` 중 하나다. `pattern`은 `regex` 전용 정보 필드다.
    """

    segment: int
    kind: str
    pattern: str | None = None


@dataclass(frozen=True)
class RouteShape:
    """경로 부분의 판정 결과(정규 템플릿 또는 dynamic).

    `channel`은 dynamic이 아니면 정규 템플릿, dynamic이면 원문 표현(또는 None)이다.
    `trailing_slash`는 `strict`·`optional`·None(unknown)이다. `coverage_reason`은 dynamic으로 만든
    이유(limitation 문구용)다.
    """

    channel: str | None
    dynamic: bool
    trailing_slash: str | None = None
    constraints: tuple[ParamConstraint, ...] = ()
    catch_all_prefix: bool = False
    coverage_reason: str | None = None


@dataclass(frozen=True)
class RouteOrder:
    """registration-order 문서의 등록 순서(`order`). 같은 `group` 안에서 `index`가 작을수록 먼저다."""

    group: str
    index: int


@dataclass(frozen=True)
class RouteDecl:
    """route-decl 사실 하나의 내용."""

    method: str
    shape: RouteShape
    path_anchor: str
    location: Location
    qualified_name: str
    usr: str | None
    test_source: bool = False
    order: RouteOrder | None = None


@dataclass(frozen=True)
class ScopeRange:
    """한계가 가릴 수 있는 요청의 상한(`limitationScopes` 항목에서 인덱스를 뺀 것)."""

    templates: tuple[str, ...] = ()
    template_prefixes: tuple[str, ...] = ()
    template_suffixes: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()

    def to_json(self) -> dict[str, list[str]]:
        """스코프를 계약의 JSON 모양으로 바꾼다. 빈 필드는 싣지 않는다.

        Returns:
            `templates`·`templatePrefixes`·`templateSuffixes`·`methods` 중 값이 있는 필드.
        """
        pairs = (
            ("templates", self.templates),
            ("templatePrefixes", self.template_prefixes),
            ("templateSuffixes", self.template_suffixes),
            ("methods", self.methods),
        )
        return {name: sorted(set(values)) for name, values in pairs if values}


@dataclass(frozen=True)
class Gap:
    """분석 공백 하나(limitation의 재료).

    같은 `prefix`·`message`이고 스코프가 없는 공백은 하나의 문장으로 모아 개수를 센다. 스코프가 있는
    공백은 스코프마다 따로 문장이 된다(스코프는 항목 하나에만 붙는다).

    Attributes:
        prefix: 계약의 limitation 접두사(`route-coverage:` 등, 콜론 포함).
        message: 접두사 뒤 문장 틀. `{count}`가 있으면 개수로 채운다.
        scope: 증명한 요청 상한. 없으면 문서 전체 효과다.
    """

    prefix: str
    message: str
    scope: ScopeRange | None = None


@dataclass
class Extraction:
    """추출기 하나의 결과."""

    decls: list[RouteDecl] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    dispatch: str | None = None
    framework: str | None = None
    packages: set[str] = field(default_factory=set)

    def add_gap(self, prefix: str, message: str, scope: ScopeRange | None = None) -> None:
        """공백을 하나 더한다.

        Args:
            prefix: limitation 접두사.
            message: 문장 틀.
            scope: 증명한 요청 상한(없으면 None).
        """
        self.gaps.append(Gap(prefix, message, scope))
