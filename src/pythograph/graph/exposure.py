"""`bound` 값 흐름의 전체 프로그램 사실: 호출 지점 색인, 열린 자리, 속성 쓰기.

`bound` 간선은 "수신자 자리로 들어오는 관찰된 흐름이 모두 알려진 프로젝트 클래스 인스턴스"일 때만 낸다(tsograph
`value-flow.ts`와 같은 계약). 그 증명은 스캔한 프로젝트가 프로그램 전체라는 가정 위에 있으므로, 프로젝트 밖에서 값을
넣을 수 있는 자리를 먼저 찾아 **연다**(흐름을 모름으로 둔다). 이 모듈은 테스트가 아닌 소스를 한 번 훑어 다음을 모은다.

- 호출 지점 색인: 함수 → 호출 지점, `__init__` → 생성 지점(`K(...)`, `cls(...)`, `super().__init__(...)`,
  타입 모르는 수신자의 같은 이름 호출 `x.f(...)`).
- 열린 함수·생성자와 이유: 값으로 새어 나감(인자·대입·반환·장식자로 쓰임), 장식됨, 문자열에 이름이 나옴(설정의 점
  경로·`import_string`), 라이브러리의 공개 이름, 호출 지점 없음, 프레임워크·외부 기반 클래스(프레임워크가 생성한다),
  메타클래스, 동적 생성(`type(x)(...)`), 모듈 이름공간 노출(`globals()`·`sys.modules`·동적 import 뒤 계산된 이름 조회).
- 속성 쓰기(`obj.attr = v`, 리터럴 `setattr`)와 계산된 이름의 쓰기(`setattr(x, name, v)`, `x.__dict__`, `vars(x)`,
  `x.__class__ = …`): 대상 클래스를 알면 그 클래스의 속성·메서드 자리를 열고, 대상을 모르면 모델링하지 않은 틈으로 센다.

테스트 소스는 별도 프로그램이다(목이 흐름을 막지 않는다). 테스트가 아닌 모듈이 테스트 소스를 import하면 테스트 소스까지
프로그램으로 훑는다(tsograph와 같다). 스캔이 불완전하면(파싱하지 못한 파일·심볼릭 링크·파일 수 상한) `bound`를 끈다.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.scope import Resolver
from pythograph.graph.values import (
    ClassValue,
    ExternalResult,
    ExternalValue,
    FunctionValue,
    InstanceValue,
    MethodValue,
    ModuleValue,
    UnknownValue,
    Value,
)
from pythograph.source.project import Project, is_test_path
from pythograph.source.symbols import absolute_module

#: 문자열 언급으로 보는 식별자·점 경로 형식이다(`app.services.create`, `pkg.mod:main`).
_MENTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*")

#: 문자열 언급으로 보는 최대 길이다.
_MAX_MENTION_LENGTH = 512

#: 동적 import 함수(외부 점 경로)다. 리터럴 인자면 그 모듈이, 계산된 인자면 모든 모듈이 노출된다.
_DYNAMIC_IMPORTS = frozenset(
    {
        "importlib.import_module",
        "importlib.__import__",
        "builtins.__import__",
        "django.utils.module_loading.import_string",
        "django.utils.module_loading.import_module",
        "werkzeug.utils.import_string",
    }
)

#: 이름공간을 계산된 이름으로 훑는 함수다(첫 인자의 모든 멤버에 닿을 수 있다).
_COMPUTED_ACCESS = frozenset({"builtins.getattr", "builtins.dir", "inspect.getmembers", "builtins.vars"})

#: 첫 인자를 값으로 새게 하지 않는 내장 호출이다(속성·타입만 본다).
_RECEIVER_ARGUMENTS = frozenset(
    {
        "builtins.getattr",
        "builtins.hasattr",
        "builtins.setattr",
        "builtins.delattr",
        "builtins.vars",
        "builtins.dir",
        "builtins.callable",
        "builtins.type",
        "builtins.super",
        "inspect.getmembers",
    }
)

#: 코드 문자열을 실행하는 함수다(어떤 이름이든 부를 수 있다).
_CODE_EXECUTION = frozenset({"builtins.exec", "builtins.eval", "builtins.compile"})

#: 클래스 인자를 값으로 새게 하지 않는 내장 호출이다(두 번째 인자).
_TYPE_CHECKS = frozenset({"builtins.isinstance", "builtins.issubclass"})

#: 열린 자리 이유: 모든 모듈 이름공간이 계산된 이름으로 노출됐다.
ALL_MODULES = "<all-modules>"


@dataclass(frozen=True)
class CallSite:
    """함수·생성자 호출 지점 하나.

    Attributes:
        scope: 호출을 담은 범위.
        call: 호출 식.
        offset: 첫 실인자가 채우는 매개변수 순번(함수·명시적 `K.__init__(self, …)`는 0, 생성·`super()`는 1).
        shadowed: 호출을 감싼 람다 매개변수·컴프리헨션 변수 이름(실인자가 쓰면 바깥 범위로 풀 수 없다).
    """

    scope: Definition
    call: ast.Call
    offset: int
    shadowed: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AttributeWrite:
    """속성 쓰기 하나(`obj.attr = v`, 리터럴 `setattr`).

    Attributes:
        scope: 쓰기를 담은 범위.
        receiver: 수신자 식.
        value: 대입 값 식(모르면 None — 풀기·누적 대입·반복 변수).
        lambda_receiver: 수신자 뿌리가 람다 매개변수·컴프리헨션 변수인지(정적 값을 풀 수 없다).
        shadowed: 쓰기를 감싼 람다 매개변수·컴프리헨션 변수 이름(값 식이 쓰면 바깥 범위로 풀 수 없다).
    """

    scope: Definition
    receiver: ast.expr
    value: ast.expr | None
    lambda_receiver: bool = False
    shadowed: frozenset[str] = frozenset()


@dataclass
class ProgramFacts:
    """테스트가 아닌 소스 전체에서 모은 `bound` 전제 사실.

    Attributes:
        incomplete: 스캔이 불완전한 이유(없으면 None). 읽지 못한 파일이 모듈 수준 이름을 무엇이든 부를 수 있어
            라이브러리처럼 모듈 수준 이름을 모두 연다.
        library: 라이브러리로 본 이유(애플리케이션이면 None).
        whole: 테스트 소스도 프로그램으로 훑었는지.
        sites: 함수·`__init__` 정의 id → 호출 지점.
        open_functions: 함수·`__init__` 정의 id → 매개변수 자리를 연 이유.
        open_classes: 클래스 id → 생성자를 연 이유(생성 결과는 여전히 그 클래스 인스턴스다).
        dirty_classes: 클래스 id → 속성·메서드 자리를 연 이유(계산된 이름의 쓰기 대상).
        writes: 속성 이름 → 쓰기 목록.
        mentions: 문자열 리터럴에 나온 이름(점 경로의 마지막 조각 포함).
        exposed_modules: 이름공간이 새어 나간 모듈 경로(`ALL_MODULES`면 모든 모듈).
        computed_modules: 계산된 이름으로 멤버를 조회한 모듈 경로.
        computed_unknown: 타입 모르는 값을 계산된 이름으로 조회한 곳이 있는지.
        pending_writes: 정적 값으로 대상을 모르는 계산된 이름의 쓰기(흐름 질의기가 수신자 흐름으로 대상을 좁힌다).
        all_open: 모든 함수·생성자를 연 이유(`exec`·`eval`, 모듈 전체 노출 + 계산된 조회).
        dynamic_construction: 타입 모르는 값의 클래스로 생성하는 곳(`type(x)(...)`)이 있는지.
        dynamic_subclassing: 세 인자 `type(...)`·`types.new_class`로 클래스를 만드는 곳이 있는지(하위 클래스 목록이
            완전하지 않다).
    """

    incomplete: str | None = None
    library: str | None = None
    whole: bool = False
    sites: dict[str, list[CallSite]] = field(default_factory=dict)
    open_functions: dict[str, str] = field(default_factory=dict)
    open_classes: dict[str, str] = field(default_factory=dict)
    dirty_classes: dict[str, str] = field(default_factory=dict)
    writes: dict[str, list[AttributeWrite]] = field(default_factory=dict)
    mentions: set[str] = field(default_factory=set)
    exposed_modules: set[str] = field(default_factory=set)
    computed_modules: set[str] = field(default_factory=set)
    computed_unknown: bool = False
    pending_writes: list[AttributeWrite] = field(default_factory=list)
    all_open: str | None = None
    dynamic_construction: bool = False
    dynamic_subclassing: bool = False

    def open_function(self, definition_id: str, reason: str) -> None:
        """함수·`__init__`의 매개변수 자리를 연다(처음 이유를 남긴다).

        Args:
            definition_id: 정의 id.
            reason: 이유.
        """
        self.open_functions.setdefault(definition_id, reason)

    def open_class(self, definition_id: str, reason: str) -> None:
        """클래스 생성자를 연다(처음 이유를 남긴다).

        Args:
            definition_id: 클래스 id.
            reason: 이유.
        """
        self.open_classes.setdefault(definition_id, reason)

    def outside(self, definition: Definition, name: str | None = None) -> str | None:
        """프로젝트 밖(설치해 쓰는 코드, 읽지 못한 파일)이 이름으로 닿을 수 있는 정의면 그 이유를 돌려준다.

        Args:
            definition: 함수·클래스 정의, 또는 모듈 정의(`name`이 그 모듈 전역 이름).
            name: 모듈 전역 이름(모듈 정의일 때).

        Returns:
            `scan-incomplete`·`library-public`, 닿을 수 없으면 None.
        """
        if self.incomplete is not None and is_importable(definition):
            return "scan-incomplete"
        if self.library is not None and is_public(definition) and not (name and _private(name)):
            return "library-public"
        return None

    def add_site(self, target: Definition, site: CallSite) -> None:
        """호출 지점을 더한다.

        Args:
            target: 호출되는 함수·`__init__` 정의.
            site: 호출 지점.
        """
        self.sites.setdefault(target.id, []).append(site)


def detect_library(project: Project) -> str | None:
    """프로젝트 루트가 배포 가능한 패키지(라이브러리)인지 본다.

    외부 코드가 import해 공개 함수·클래스를 임의 인자로 부를 수 있으면 그 자리를 열어야 한다. `setup.py`·`setup.cfg`가
    있거나 `pyproject.toml`에 `[project]`·`[tool.poetry]` 표가 있으면 라이브러리다(`[build-system]`이 없어도 pip는
    setuptools로 설치한다). `[tool.poetry] package-mode = false`·`[tool.uv] package = false`는 설치하지 않는 앱이다.
    `pyproject.toml`을 읽지 못하면 라이브러리로 본다(열린 쪽이 안전하다).

    Args:
        project: 분석 대상 프로젝트.

    Returns:
        라이브러리로 본 이유, 애플리케이션이면 None.
    """
    for name in ("setup.py", "setup.cfg"):
        if project.is_file(name):
            return f"{name} at the project root"
    if not project.is_file("pyproject.toml"):
        return None
    text = project.read_text("pyproject.toml")
    if text is None:
        return "pyproject.toml could not be read"
    tables = _toml_tables(text)
    packaged = "project" in tables or "tool.poetry" in tables
    if packaged and not _application_flag(tables):
        return "pyproject.toml declares [project] or [tool.poetry]"
    return None


def _toml_tables(text: str) -> dict[str, list[str]]:
    """TOML 표 머리와 그 표의 줄을 모은다(표준 라이브러리 `tomllib`가 3.11부터라 줄 단위로 읽는다).

    Args:
        text: pyproject.toml 내용.

    Returns:
        표 이름 → 줄 목록.
    """
    tables: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        header = re.fullmatch(r"\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?", line)
        if header is not None:
            current = header.group(1).replace('"', "").replace(" ", "")
            tables.setdefault(current, [])
        else:
            tables[current].append(line)
    return tables


def _application_flag(tables: dict[str, list[str]]) -> bool:
    """설치하지 않는 앱이라는 표시(`package-mode = false`, `package = false`)가 있는지 본다.

    Args:
        tables: 표 이름 → 줄 목록.

    Returns:
        있으면 True.
    """
    poetry = any(
        re.fullmatch(r"\s*package-mode\s*=\s*false\s*(?:#.*)?", line) for line in tables.get("tool.poetry", [])
    )
    uv = any(re.fullmatch(r"\s*package\s*=\s*false\s*(?:#.*)?", line) for line in tables.get("tool.uv", []))
    return poetry or uv


def is_public(definition: Definition) -> bool:
    """정의가 외부에서 import할 수 있는 공개 이름인지 본다(경로·점 경로 어디에도 밑줄 이름이 없다).

    함수 안에 중첩된 정의는 공개가 아니다(모듈 속성이 아니다). 모듈 정의는 경로만 본다.

    Args:
        definition: 모듈·함수·클래스 정의.

    Returns:
        공개면 True.
    """
    parts = PurePosixPath(definition.path).with_suffix("").parts
    if any(_private(part) for part in parts if part != "__init__"):
        return False
    current = definition
    while current.kind != "module" and current.parent is not None:
        if _private(getattr(current.node, "name", "")) or current.parent.kind in ("function", "method"):
            return False
        current = current.parent
    return True


def is_importable(definition: Definition) -> bool:
    """정의가 모듈 속성으로 닿을 수 있는지 본다(함수 안에 중첩되지 않았다).

    Args:
        definition: 정의.

    Returns:
        그렇다면 True.
    """
    current = definition
    while current.parent is not None:
        if current.parent.kind in ("function", "method"):
            return False
        current = current.parent
    return True


def _private(name: str) -> bool:
    """밑줄로 시작하는 비공개 이름인지 본다(던더 이름은 공개로 본다).

    Args:
        name: 이름.

    Returns:
        비공개면 True.
    """
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def collect_program(project: Project, index: DefinitionIndex, resolver: Resolver, whole: bool) -> ProgramFacts:
    """프로그램 소스 전체를 훑어 `bound` 전제 사실을 모은다.

    Args:
        project: 분석 대상 프로젝트.
        index: 프로그램 선언 색인(`whole`이면 테스트 소스 포함).
        resolver: 그 색인의 값 해석기.
        whole: 테스트 소스도 프로그램인지(테스트가 아닌 모듈이 테스트 소스를 import할 때).

    Returns:
        모은 사실.
    """
    facts = ProgramFacts(library=detect_library(project), whole=whole)
    if project.scan_capped or project.skipped_links or project.unencodable_names or index.unparsed:
        facts.incomplete = "the scan is incomplete (unparsed files, skipped links, or the file limit)"
    names = _definition_names(index, whole)
    for definition in sorted(index.definitions.values(), key=lambda item: item.id):
        if whole or not is_test_path(definition.path):
            _ProgramVisitor(facts, resolver, definition, names).run()
    return facts


def tests_joined(project: Project, imports: list[tuple[Definition, ast.Import | ast.ImportFrom]]) -> bool:
    """테스트가 아닌 모듈이 테스트 소스를 import하는지 본다(그러면 테스트는 별도 프로그램이 아니다).

    Args:
        project: 분석 대상 프로젝트.
        imports: (모듈, import 문) 목록(`build.scan_modules`가 모은다).

    Returns:
        그렇다면 True.
    """
    if not any(is_test_path(path) for path in project.python_files()):
        return False
    return any(
        not is_test_path(module.path) and _imports_test_source(project, module, node) for module, node in imports
    )


def _imports_test_source(project: Project, module: Definition, node: ast.Import | ast.ImportFrom) -> bool:
    """import 문 하나가 프로젝트의 테스트 소스를 가리키는지 본다.

    Args:
        project: 분석 대상 프로젝트.
        module: import가 있는 모듈 정의.
        node: import 문.

    Returns:
        그렇다면 True.
    """
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:
        base = absolute_module(module.module.module, node.module, node.level)
        names = [] if base is None else [base, *(f"{base}.{alias.name}" for alias in node.names)]
    for name in names:
        if _maybe_test_module(project, name):
            path = project.resolve_module(name)
            if path is not None and is_test_path(path):
                return True
    return False


def _maybe_test_module(project: Project, name: str) -> bool:
    """점 경로가 테스트 소스로 풀릴 수 있는지 싸게 거른다(`is_test_path` 규칙의 이름 조각, 모듈 루트의 테스트 디렉터리).

    Args:
        project: 분석 대상 프로젝트.
        name: 모듈 점 경로.

    Returns:
        테스트 소스일 수 있으면 True.
    """
    parts = name.split(".")
    if any(
        part in ("tests", "test", "conftest") or part.startswith("test_") or part.endswith("_test") for part in parts
    ):
        return True
    return any(is_test_path(f"{root}/x.py") for root in project.module_roots if root)


@dataclass(frozen=True)
class DefinitionNames:
    """이름 기준 조회표(타입 모르는 수신자의 `x.f(...)`·`x.K`가 닿을 수 있는 정의).

    Attributes:
        functions: 이름 → 모듈 수준 함수 정의.
        classes: 이름 → 클래스 정의.
        inits: 모든 프로젝트 `__init__` 정의.
    """

    functions: dict[str, list[Definition]]
    classes: dict[str, list[Definition]]
    inits: list[Definition]


def _definition_names(index: DefinitionIndex, whole: bool) -> DefinitionNames:
    """이름 기준 조회표를 만든다.

    Args:
        index: 프로그램 선언 색인.
        whole: 테스트 소스도 프로그램인지.

    Returns:
        조회표.
    """
    functions: dict[str, list[Definition]] = {}
    classes: dict[str, list[Definition]] = {}
    inits: list[Definition] = []
    for definition in sorted(index.definitions.values(), key=lambda item: item.id):
        if is_test_path(definition.path) and not whole:
            continue
        name = getattr(definition.node, "name", "")
        if definition.kind == "class":
            classes.setdefault(name, []).append(definition)
        elif definition.kind == "function" and definition.parent is not None and definition.parent.kind == "module":
            functions.setdefault(name, []).append(definition)
        elif definition.kind == "method" and name == "__init__":
            inits.append(definition)
    return DefinitionNames(functions, classes, inits)


class _ProgramVisitor(ast.NodeVisitor):
    """정의 하나의 자기 범위를 역할(피호출·인자·수신자·값)에 따라 훑어 사실을 모은다."""

    def __init__(self, facts: ProgramFacts, resolver: Resolver, scope: Definition, names: DefinitionNames) -> None:
        """방문자를 만든다.

        Args:
            facts: 채울 사실.
            resolver: 값 해석기.
            scope: 범위 정의.
            names: 이름 기준 조회표.
        """
        self.facts = facts
        self.resolver = resolver
        self.scope = scope
        self.names = names
        self.shadowed: list[set[str]] = []
        self.mentions_enabled = True

    def run(self) -> None:
        """자기 범위 문장을 방문한다(첫 문장의 docstring은 문자열 언급에서 뺀다)."""
        body = getattr(self.scope.node, "body", [])
        statements = list(body) if isinstance(body, list) else []
        if statements and _is_docstring(statements[0]):
            statements = statements[1:]
        for statement in statements:
            self.visit(statement)

    def _hidden(self) -> frozenset[str]:
        """지금 감싼 람다 매개변수·컴프리헨션 변수 이름 전부다.

        Returns:
            이름 집합.
        """
        return frozenset(name for names in self.shadowed for name in names)

    def _is_shadowed(self, expr: ast.expr) -> bool:
        """식의 뿌리 이름이 람다 매개변수·컴프리헨션 변수인지 본다.

        Args:
            expr: 식.

        Returns:
            가려졌으면 True.
        """
        root = expr
        while isinstance(root, (ast.Attribute, ast.Subscript, ast.Call)):
            root = root.func if isinstance(root, ast.Call) else root.value
        return isinstance(root, ast.Name) and any(root.id in names for names in self.shadowed)

    def _value(self, expr: ast.expr) -> Value:
        """가려지지 않은 식의 정적 값을 푼다(람다 매개변수·컴프리헨션 변수는 모름).

        Args:
            expr: 식.

        Returns:
            값.
        """
        if self._is_shadowed(expr):
            return UnknownValue("lambda-parameter")
        return self.resolver.value(self.scope, expr)

    # --- 정의 머리·람다·컴프리헨션 ---

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """중첩 함수: 장식자·기본값만 이 범위에서 평가된다."""
        self._function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """중첩 비동기 함수."""
        self._function_header(node)

    def _function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """장식자(값으로 새는 사용)와 기본값을 방문한다.

        Args:
            node: 정의 노드.
        """
        for decorator in node.decorator_list:
            self._escaping(decorator)
        arguments = node.args
        for default in [*arguments.defaults, *[item for item in arguments.kw_defaults if item is not None]]:
            self.visit(default)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """중첩 클래스: 장식자·키워드(메타클래스)는 값 사용, 기반은 상속이라 새지 않는다."""
        for decorator in node.decorator_list:
            self._escaping(decorator)
        for base in node.bases:
            self._receiver(base.value if isinstance(base, ast.Subscript) else base)
        for keyword in node.keywords:
            self._escaping(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """람다 본문은 이 범위의 일부다. 매개변수는 가린다."""
        arguments = node.args
        names = {item.arg for item in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
        names.update(item.arg for item in (arguments.vararg, arguments.kwarg) if item is not None)
        for default in [*arguments.defaults, *[item for item in arguments.kw_defaults if item is not None]]:
            self.visit(default)
        self.shadowed.append(names)
        self._escaping(node.body)
        self.shadowed.pop()

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """컴프리헨션은 이 범위의 일부다. 반복 변수는 가린다.

        Args:
            node: 컴프리헨션 노드.
        """
        names = {
            child.id
            for generator in node.generators
            for child in ast.walk(generator.target)
            if isinstance(child, ast.Name)
        }
        self.shadowed.append(names)
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        for element in elements:
            self._escaping(element)
        self.shadowed.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """리스트 컴프리헨션."""
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """집합 컴프리헨션."""
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """제너레이터 식."""
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """사전 컴프리헨션."""
        self._comprehension(node)

    # --- 문장 ---

    def visit_Assign(self, node: ast.Assign) -> None:
        """대입: 속성 대상은 쓰기, `__all__`의 문자열은 언급이 아니다."""
        is_all = any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        for target in node.targets:
            self._store(target, node.value)
        self._escaping_without_mentions(node.value) if is_all else self._escaping(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """주석 대입: 주석은 값 사용이 아니다."""
        if node.value is not None:
            self._store(node.target, node.value)
            self._escaping(node.value)
        elif isinstance(node.target, ast.Attribute):
            self._receiver(node.target.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """누적 대입: 속성 대상은 값 모르는 쓰기다."""
        self._store(node.target, None)
        is_all = isinstance(node.target, ast.Name) and node.target.id == "__all__"
        self._escaping_without_mentions(node.value) if is_all else self._escaping(node.value)

    def visit_For(self, node: ast.For) -> None:
        """반복 변수가 속성이면 값 모르는 쓰기다."""
        self._store(node.target, None)
        self.visit(node.iter)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """비동기 반복."""
        self._store(node.target, None)
        self.visit(node.iter)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    def visit_withitem(self, node: ast.withitem) -> None:
        """`with … as obj.attr`는 값 모르는 쓰기다."""
        self.visit(node.context_expr)
        if node.optional_vars is not None:
            self._store(node.optional_vars, None)

    def visit_Return(self, node: ast.Return) -> None:
        """반환 값은 값 사용이다."""
        if node.value is not None:
            self._escaping(node.value)

    def visit_Expr(self, node: ast.Expr) -> None:
        """식 문장(호출 등)."""
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        """삭제는 흐름을 더하지 않는다(수신자만 방문한다)."""
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                self._receiver(target.value)
            elif isinstance(target, ast.Subscript):
                self.visit(target.value)
                self.visit(target.slice)

    def _store(self, target: ast.expr, value: ast.expr | None) -> None:
        """대입 대상을 처리한다: 속성은 쓰기, 풀기 안의 속성은 값 모르는 쓰기, 첨자는 수신자 방문.

        Args:
            target: 대입 대상.
            value: 대입 값(모르면 None).
        """
        if isinstance(target, ast.Attribute):
            self._attribute_write(target, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._store(element, None)
        elif isinstance(target, ast.Starred):
            self._store(target.value, None)
        elif isinstance(target, ast.Subscript):
            self._subscript_store(target)

    def _subscript_store(self, target: ast.Subscript) -> None:
        """첨자 대입: `x.__dict__[k] = v`·`globals()[k] = v`·`sys.modules[k] = m`은 계산된 이름의 쓰기다.

        Args:
            target: 첨자 대상.
        """
        container = target.value
        if isinstance(container, ast.Attribute) and container.attr == "__dict__":
            self._computed_write(container.value)
        self.visit(container)
        self.visit(target.slice)

    def _attribute_write(self, target: ast.Attribute, value: ast.expr | None) -> None:
        """속성 쓰기를 기록한다. `__class__`·`__dict__` 쓰기는 계산된 이름의 쓰기다.

        Args:
            target: 속성 대상.
            value: 대입 값(모르면 None).
        """
        if target.attr in ("__class__", "__dict__"):
            self._computed_write(target.value)
        else:
            write = AttributeWrite(self.scope, target.value, value, self._is_shadowed(target.value), self._hidden())
            self.facts.writes.setdefault(target.attr, []).append(write)
        self._receiver(target.value)

    def _computed_write(self, receiver: ast.expr) -> None:
        """이름을 모르는 속성 쓰기: 대상 클래스를 알면 그 클래스를 열고, 모르면 흐름으로 좁히도록 남긴다.

        Args:
            receiver: 쓰기 대상 식.
        """
        value = self._value(receiver)
        if isinstance(value, (InstanceValue, ClassValue)):
            for definition in self._related(value):
                self.facts.dirty_classes.setdefault(definition.id, "computed-attribute-write")
        elif isinstance(value, ModuleValue):
            self.facts.computed_modules.add(value.path)
            self.facts.exposed_modules.add(value.path)
        elif not isinstance(value, (ExternalValue, FunctionValue, MethodValue)):
            write = AttributeWrite(self.scope, receiver, None, self._is_shadowed(receiver), self._hidden())
            self.facts.pending_writes.append(write)

    def _related(self, value: InstanceValue | ClassValue) -> list[Definition]:
        """값이 가리킬 수 있는 프로젝트 클래스다(정확하지 않으면 하위 클래스 포함).

        Args:
            value: 인스턴스·클래스 값.

        Returns:
            클래스 정의 목록.
        """
        classes = [value.definition]
        if not value.exact or isinstance(value, ClassValue):
            classes.extend(self.resolver.linearizer.subclasses(value.definition))
        return classes

    # --- 식 ---

    def visit_Constant(self, node: ast.Constant) -> None:
        """문자열 리터럴의 식별자·점 경로를 언급으로 모은다(설정의 `"app.middleware.Audit"`, `import_string`)."""
        if self.mentions_enabled and isinstance(node.value, str) and len(node.value) <= _MAX_MENTION_LENGTH:
            text = node.value.strip()
            if _MENTION.fullmatch(text):
                self.facts.mentions.add(re.split(r"[.:]", text)[-1])

    def visit_Name(self, node: ast.Name) -> None:
        """값 위치의 이름: 함수·클래스·모듈이 값으로 샌다."""
        if isinstance(node.ctx, ast.Load):
            self._escape_value(self._value(node))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """값 위치의 속성 접근."""
        if isinstance(node.ctx, ast.Store):
            self._attribute_write(node, None)
        elif isinstance(node.ctx, ast.Load):
            self._escaping(node)
        else:
            self._receiver(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """첨자 읽기: `sys.modules[...]`는 모듈 이름공간 노출이다."""
        if not self._sys_modules(node):
            self.visit(node.value)
        self.visit(node.slice)

    def visit_Compare(self, node: ast.Compare) -> None:
        """비교는 값을 새게 하지 않는다: `type(x) is K`·`x.__class__ == K`·`f is None`의 이름·속성은 수신자다."""
        for operand in [node.left, *node.comparators]:
            inner = _probe_inner(operand)
            if inner is not None:
                self._receiver(inner)
            elif isinstance(operand, (ast.Name, ast.Attribute)):
                self._receiver(operand)
            else:
                self.visit(operand)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """`except K`의 예외 클래스(튜플 포함)는 값으로 새지 않는다."""
        if node.type is not None:
            self._type_check_argument(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:
        """호출: 피호출 식을 호출 지점으로 분류하고 인자를 값 사용으로 방문한다."""
        callee = self._callee(node)
        dotted = callee.dotted if isinstance(callee, ExternalValue) else ""
        self._special_call(node, dotted)
        for position, argument in enumerate(node.args):
            inner = argument.value if isinstance(argument, ast.Starred) else argument
            if dotted in _TYPE_CHECKS and position == 1:
                self._type_check_argument(inner)
            elif dotted in _RECEIVER_ARGUMENTS and (position == 0 or dotted == "builtins.super"):
                self._receiver(inner)
            else:
                self._escaping(inner)
        for keyword in node.keywords:
            self._escaping(keyword.value)

    def _callee(self, node: ast.Call) -> Value:
        """피호출 식을 방문하고 값을 풀어 호출 지점으로 분류한다.

        Args:
            node: 호출.

        Returns:
            피호출 값.
        """
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "__class__":
            self._class_probe_call(node, func.value)
            self._receiver(func.value)
            return UnknownValue("dynamic-construction")
        if isinstance(func, ast.Attribute):
            self._receiver(func.value)
        elif isinstance(func, ast.Call) and _is_type_call(func):
            self._class_probe_call(node, func.args[0])
            self._receiver(func.args[0])
            return UnknownValue("dynamic-construction")
        elif not isinstance(func, ast.Name):
            self.visit(func)
        value = self._value(func)
        self._classify_site(node, value)
        return value

    def _class_probe_call(self, node: ast.Call, inner: ast.expr) -> None:
        """`type(x)(...)`·`x.__class__(...)`: x의 클래스를 알면 생성 지점, 모르면 모든 생성자를 연다.

        Args:
            node: 생성 호출.
            inner: x 식.
        """
        value = self._value(inner)
        if isinstance(value, InstanceValue):
            for definition in self._related(value):
                self._construct_site(node, definition)
        else:
            self.facts.dynamic_construction = True

    def _classify_site(self, node: ast.Call, value: Value) -> None:
        """피호출 값에 따라 함수·생성자 호출 지점을 더한다.

        Args:
            node: 호출.
            value: 피호출 값.
        """
        if isinstance(value, FunctionValue):
            self.facts.add_site(value.definition, CallSite(self.scope, node, 0, self._hidden()))
        elif isinstance(value, ClassValue):
            for definition in self._related(value) if not value.exact else [value.definition]:
                self._construct_site(node, definition)
        elif isinstance(value, MethodValue) and _node_name(value.member) == "__init__":
            self._explicit_init(node, value)
        elif isinstance(value, (UnknownValue, ExternalResult)) and isinstance(node.func, ast.Attribute):
            self._untyped_site(node, node.func.attr)

    def _construct_site(self, node: ast.Call, definition: Definition) -> None:
        """클래스 생성 지점: MRO로 찾은 프로젝트 `__init__`에 오프셋 1로 더한다.

        Args:
            node: 생성 호출.
            definition: 생성하는 클래스.
        """
        member = self.resolver.linearizer.lookup(definition, "__init__")
        if member.kind == "method" and member.definition is not None:
            self.facts.add_site(member.definition, CallSite(self.scope, node, 1, self._hidden()))

    def _explicit_init(self, node: ast.Call, value: MethodValue) -> None:
        """`super().__init__(...)`(오프셋 1)·`K.__init__(self, ...)`(오프셋 0). 인자 있는 `super(...)`는 연다.

        Args:
            node: 호출.
            value: `__init__` 메서드 값.
        """
        func = node.func
        receiver = func.value if isinstance(func, ast.Attribute) else None
        if isinstance(receiver, ast.Call) and _is_super_with_arguments(receiver):
            for entry in self.resolver.linearizer.mro(value.receiver):
                if entry.definition is not None:
                    init = self.resolver.index.class_members(entry.definition).methods.get("__init__")
                    if init is not None:
                        self.facts.open_function(init.id, "super-with-arguments")
            return
        explicit = receiver is not None and isinstance(self._value(receiver), ClassValue)
        self.facts.add_site(value.member, CallSite(self.scope, node, 0 if explicit else 1, self._hidden()))

    def _untyped_site(self, node: ast.Call, name: str) -> None:
        """타입 모르는 수신자의 `x.name(...)`: 같은 이름의 모듈 함수·클래스·`__init__`에 닿을 수 있다(인자는 관찰된다).

        Args:
            node: 호출.
            name: 속성 이름.
        """
        if name == "__init__":
            for init in self.names.inits:
                self.facts.open_function(init.id, "untyped-init-call")
            return
        for function in self.names.functions.get(name, []):
            self.facts.add_site(function, CallSite(self.scope, node, 0, self._hidden()))
        for definition in self.names.classes.get(name, []):
            self._construct_site(node, definition)

    def _special_call(self, node: ast.Call, dotted: str) -> None:
        """이름공간·속성을 동적으로 다루는 내장 호출을 처리한다.

        Args:
            node: 호출.
            dotted: 피호출 외부 점 경로(외부가 아니면 빈 문자열).
        """
        if dotted == "builtins.setattr" and len(node.args) >= 3:
            self._setattr(node)
        elif dotted in _COMPUTED_ACCESS and node.args:
            self._computed_access(node, dotted)
        elif dotted == "builtins.globals" or (
            dotted in ("builtins.locals", "builtins.vars") and self.scope.kind == "module"
        ):
            self._expose_module(self.scope.path, computed=True)
        elif (dotted == "builtins.type" and len(node.args) == 3) or dotted == "types.new_class":
            self.facts.dynamic_subclassing = True
        elif dotted in _CODE_EXECUTION:
            self.facts.all_open = self.facts.all_open or "code-execution"
        elif dotted in _DYNAMIC_IMPORTS and node.args:
            self._dynamic_import(node.args[0])

    def _setattr(self, node: ast.Call) -> None:
        """`setattr(x, "name", v)`는 속성 쓰기, 계산된 이름은 계산된 쓰기다.

        Args:
            node: setattr 호출.
        """
        name = node.args[1]
        if (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and name.value not in ("__class__", "__dict__")
        ):
            write = AttributeWrite(
                self.scope, node.args[0], node.args[2], self._is_shadowed(node.args[0]), self._hidden()
            )
            self.facts.writes.setdefault(name.value, []).append(write)
        else:
            self._computed_write(node.args[0])

    def _computed_access(self, node: ast.Call, dotted: str) -> None:
        """계산된 이름의 조회(`getattr(x, name)`, `vars(x)`, `dir(x)`, `inspect.getmembers(x)`).

        리터럴 이름의 `getattr`은 해석기가 멤버로 푼다. 모듈을 훑으면 그 모듈 멤버가 열리고, 타입 모르는 값을 훑으면
        노출된 모듈 이름공간의 멤버가 열린다. `vars(x)`는 `x.__dict__`라 계산된 쓰기이기도 하다.

        Args:
            node: 호출.
            dotted: 피호출 점 경로.
        """
        if dotted == "builtins.getattr" and len(node.args) >= 2:
            name = node.args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                return
        if dotted == "builtins.vars":
            self._computed_write(node.args[0])
        value = self._value(node.args[0])
        if isinstance(value, ModuleValue):
            self.facts.computed_modules.add(value.path)
        elif isinstance(value, (UnknownValue, ExternalResult)):
            self.facts.computed_unknown = True

    def _dynamic_import(self, argument: ast.expr) -> None:
        """동적 import: 리터럴이면 그 모듈, 계산된 이름이면 모든 모듈 이름공간이 새어 나간다.

        Args:
            argument: 모듈 이름 인자.
        """
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            path = self.resolver.symbols.project.resolve_module(argument.value)
            if path is not None:
                self._expose_module(path, computed=False)
            return
        self.facts.exposed_modules.add(ALL_MODULES)

    def _expose_module(self, path: str, computed: bool) -> None:
        """모듈 이름공간 노출을 기록한다.

        Args:
            path: 모듈 경로.
            computed: 계산된 이름으로 훑는지(`globals()`).
        """
        self.facts.exposed_modules.add(path)
        if computed:
            self.facts.computed_modules.add(path)

    def _sys_modules(self, node: ast.Subscript) -> bool:
        """`sys.modules[__name__]`은 자기 모듈, 그 밖의 `sys.modules[...]`는 모든 모듈 노출이다.

        Args:
            node: 첨자 식.

        Returns:
            `sys.modules` 첨자였으면 True.
        """
        if not _is_sys_modules(self._value(node.value)):
            return False
        if isinstance(node.slice, ast.Name) and node.slice.id == "__name__":
            self._expose_module(self.scope.path, computed=False)
        else:
            self.facts.exposed_modules.add(ALL_MODULES)
        return True

    def _type_check_argument(self, expr: ast.expr) -> None:
        """`isinstance(x, K)`의 K(튜플 포함)는 값으로 새지 않는다.

        Args:
            expr: 두 번째 인자.
        """
        elements = expr.elts if isinstance(expr, ast.Tuple) else [expr]
        for element in elements:
            self._receiver(element)

    def _receiver(self, expr: ast.expr) -> None:
        """수신자 위치(속성 접근의 왼쪽, 기반 클래스): 값으로 새지 않고 안쪽만 방문한다.

        Args:
            expr: 수신자 식.
        """
        if isinstance(expr, ast.Attribute):
            if expr.attr == "__dict__":
                self._computed_write(expr.value)
            if _is_sys_modules(self._value(expr)):
                self.facts.exposed_modules.add(ALL_MODULES)
            self._receiver(expr.value)
        elif isinstance(expr, ast.Subscript):
            self.visit_Subscript(expr)
        elif not isinstance(expr, ast.Name):
            self.visit(expr)
        elif _is_sys_modules(self._value(expr)):
            self.facts.exposed_modules.add(ALL_MODULES)

    def _escaping(self, expr: ast.expr) -> None:
        """값 위치의 식: 이름·속성이면 가리키는 정의가 값으로 샌다, 그 밖은 안쪽을 방문한다.

        Args:
            expr: 식.
        """
        if isinstance(expr, ast.Name):
            self.visit_Name(expr)
        elif isinstance(expr, ast.Attribute):
            self._escaping_attribute(expr)
        elif _is_class_probe(expr):
            self._class_probe_escape(expr)
        elif isinstance(expr, ast.Call):
            self.visit_Call(expr)
            self._escape_value(self._value(expr))
        else:
            self.visit(expr)

    def _escaping_attribute(self, expr: ast.Attribute) -> None:
        """값 위치의 속성: `x.__dict__`는 계산된 쓰기, 그 밖은 가리키는 정의가 샌다.

        Args:
            expr: 속성 식.
        """
        if expr.attr == "__dict__":
            self._computed_write(expr.value)
        self._receiver(expr.value)
        self._untyped_member(expr)
        self._escape_value(self._value(expr))

    def _escaping_without_mentions(self, expr: ast.expr) -> None:
        """문자열 언급을 모으지 않고 값 식을 방문한다(`__all__`).

        Args:
            expr: 식.
        """
        self.mentions_enabled = False
        try:
            self._escaping(expr)
        finally:
            self.mentions_enabled = True

    def _class_probe_escape(self, expr: ast.expr) -> None:
        """값으로 쓴 `type(x)`·`x.__class__`: x의 클래스를 알면 그 생성자, 모르면 모든 생성자를 연다.

        Args:
            expr: 탐침 식.
        """
        inner = _probe_inner(expr)
        assert inner is not None
        value = self._value(inner)
        if isinstance(value, InstanceValue):
            for definition in self._related(value):
                self.facts.open_class(definition.id, "dynamic-construction")
        else:
            self.facts.dynamic_construction = True
        self._receiver(inner)

    def _untyped_member(self, expr: ast.Attribute) -> None:
        """타입 모르는 수신자의 `x.name` 값 사용: 같은 이름의 모듈 함수·클래스가 새어 나갈 수 있다.

        호출(`x.name(...)`)은 호출 지점으로, 수신자 위치(`x.name.y`)는 새지 않는 것으로 따로 다룬다.

        Args:
            expr: 값 위치의 속성 식.
        """
        value = self._value(expr.value)
        if not isinstance(value, (UnknownValue, ExternalResult)):
            return
        if expr.attr == "__init__":
            for init in self.names.inits:
                self.facts.open_function(init.id, "untyped-reference")
        for function in self.names.functions.get(expr.attr, []):
            self.facts.open_function(function.id, "untyped-reference")
        for definition in self.names.classes.get(expr.attr, []):
            self.facts.open_class(definition.id, "untyped-reference")

    def _escape_value(self, value: Value) -> None:
        """값으로 쓴 정의를 연다: 함수·`__init__`(매개변수), 클래스(생성자), 모듈(이름공간).

        Args:
            value: 식의 값.
        """
        if _is_sys_modules(value):
            self.facts.exposed_modules.add(ALL_MODULES)
        elif isinstance(value, FunctionValue):
            self.facts.open_function(value.definition.id, "referenced")
        elif isinstance(value, ClassValue):
            for definition in self._related(value) if not value.exact else [value.definition]:
                self.facts.open_class(definition.id, "referenced")
        elif isinstance(value, MethodValue) and _node_name(value.member) == "__init__":
            self.facts.open_function(value.member.id, "referenced")
        elif isinstance(value, ModuleValue):
            self.facts.exposed_modules.add(value.path)


def _is_sys_modules(value: Value) -> bool:
    """값이 `sys.modules`인지 본다.

    Args:
        value: 값.

    Returns:
        그렇다면 True.
    """
    return isinstance(value, ExternalValue) and value.dotted == "sys.modules"


def _is_docstring(statement: ast.stmt) -> bool:
    """문장이 docstring인지 본다.

    Args:
        statement: 첫 문장.

    Returns:
        문자열 식 문장이면 True.
    """
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _is_type_call(node: ast.expr) -> bool:
    """`type(x)` 한 인자 호출인지 본다(이름으로만 — 가려진 `type`은 드물고 열린 쪽으로 간다).

    Args:
        node: 식.

    Returns:
        그렇다면 True.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and len(node.args) == 1
        and not node.keywords
    )


def _is_class_probe(node: ast.expr) -> bool:
    """`type(x)` 또는 `x.__class__`인지 본다.

    Args:
        node: 식.

    Returns:
        그렇다면 True.
    """
    return _probe_inner(node) is not None


def _probe_inner(node: ast.expr) -> ast.expr | None:
    """`type(x)`·`x.__class__`의 x 식이다.

    Args:
        node: 식.

    Returns:
        x 식, 탐침이 아니면 None.
    """
    if isinstance(node, ast.Attribute) and node.attr == "__class__":
        return node.value
    if isinstance(node, ast.Call) and _is_type_call(node):
        return node.args[0]
    return None


def _is_super_with_arguments(node: ast.Call) -> bool:
    """인자 있는 `super(X, y)` 호출인지 본다.

    Args:
        node: 호출.

    Returns:
        그렇다면 True.
    """
    return isinstance(node.func, ast.Name) and node.func.id == "super" and bool(node.args)


def _node_name(definition: Definition) -> str:
    """정의 이름을 돌려준다.

    Args:
        definition: 정의.

    Returns:
        이름.
    """
    return getattr(definition.node, "name", "")
