"""설치한 Django·DRF·Flask 소스를 ast로 읽어 호출 그래프의 프레임워크 훅 표를 만든다.

사용법: python dump_framework_hooks.py --site-packages <스크래치 가상 환경의 site-packages> [--check]

`pythograph graph`는 프레임워크 구현을 실행·import하지 않는다. 대신 이 스크립트가 공식 배포본 소스
(Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3)를 표준 라이브러리 `ast`로만 읽어, 알려진 기반 클래스마다
다음을 기록한다.

- `mro`: 프레임워크 클래스들의 C3 선형화(표의 키 목록). 표에 없는 조상은 싣지 않는다(`object` 등).
- `methods`: 클래스 본문이 정의한 메서드마다, 그 본문(중첩 함수 포함)이 첫 매개변수(`self`/`cls`)나 `self`라는
  지역 이름으로 호출하는 속성 이름(`calls`, `cls(...)`는 `__init__`), 호출하지 않고 읽는 이름(`reads`,
  `getattr(self, "리터럴")` 포함), `super().X` 이름(`super`), property 여부(`property`).
- `attributes`: 클래스 본문의 대입 이름(프레임워크 기본값). 런타임은 이 이름을 프레임워크 값으로 보고 멈춘다.
- `aliases`: 공개 재수출 경로(`django.views.generic.ListView`) → 정의 경로.

결과는 `src/pythograph/graph/framework_table.py`(생성 파일)에 파이썬 리터럴로 쓴다. `--check`는 다시 만든 내용이
저장소 파일과 같은지만 확인한다.
"""

import argparse
import ast
import pprint
import sys
from pathlib import Path

# 이 스크립트가 있는 디렉터리다.
HERE = Path(__file__).resolve().parent
# 생성 파일 경로다.
OUTPUT = HERE.parent.parent / "src" / "pythograph" / "graph" / "framework_table.py"

# 표에 싣는 시작 클래스(정의 경로)다. 조상은 자동으로 따라간다.
START_CLASSES = (
    "django.views.generic.base.View",
    "django.views.generic.base.TemplateView",
    "django.views.generic.base.RedirectView",
    "django.views.generic.list.ListView",
    "django.views.generic.detail.DetailView",
    "django.views.generic.edit.FormView",
    "django.views.generic.edit.CreateView",
    "django.views.generic.edit.UpdateView",
    "django.views.generic.edit.DeleteView",
    "django.views.generic.dates.ArchiveIndexView",
    "django.views.generic.dates.YearArchiveView",
    "django.views.generic.dates.MonthArchiveView",
    "django.views.generic.dates.WeekArchiveView",
    "django.views.generic.dates.DayArchiveView",
    "django.views.generic.dates.TodayArchiveView",
    "django.views.generic.dates.DateDetailView",
    "django.contrib.auth.mixins.LoginRequiredMixin",
    "django.contrib.auth.mixins.PermissionRequiredMixin",
    "django.contrib.auth.mixins.UserPassesTestMixin",
    "django.contrib.messages.views.SuccessMessageMixin",
    "django.contrib.auth.views.LoginView",
    "django.contrib.auth.views.LogoutView",
    "django.contrib.auth.views.PasswordChangeView",
    "django.contrib.auth.views.PasswordChangeDoneView",
    "django.contrib.auth.views.PasswordResetView",
    "django.contrib.auth.views.PasswordResetDoneView",
    "django.contrib.auth.views.PasswordResetConfirmView",
    "django.contrib.auth.views.PasswordResetCompleteView",
    "rest_framework.views.APIView",
    "rest_framework.generics.GenericAPIView",
    "rest_framework.generics.CreateAPIView",
    "rest_framework.generics.ListAPIView",
    "rest_framework.generics.RetrieveAPIView",
    "rest_framework.generics.DestroyAPIView",
    "rest_framework.generics.UpdateAPIView",
    "rest_framework.generics.ListCreateAPIView",
    "rest_framework.generics.RetrieveUpdateAPIView",
    "rest_framework.generics.RetrieveDestroyAPIView",
    "rest_framework.generics.RetrieveUpdateDestroyAPIView",
    "rest_framework.viewsets.ViewSetMixin",
    "rest_framework.viewsets.ViewSet",
    "rest_framework.viewsets.GenericViewSet",
    "rest_framework.viewsets.ModelViewSet",
    "rest_framework.viewsets.ReadOnlyModelViewSet",
    "rest_framework.mixins.ListModelMixin",
    "rest_framework.mixins.CreateModelMixin",
    "rest_framework.mixins.RetrieveModelMixin",
    "rest_framework.mixins.UpdateModelMixin",
    "rest_framework.mixins.DestroyModelMixin",
    "rest_framework.serializers.BaseSerializer",
    "rest_framework.serializers.Serializer",
    "rest_framework.serializers.ModelSerializer",
    "rest_framework.serializers.HyperlinkedModelSerializer",
    "rest_framework.serializers.ListSerializer",
    "flask.views.View",
    "flask.views.MethodView",
)

# 재수출 경로를 찾을 공개 모듈이다.
ALIAS_MODULES = (
    "django.views",
    "django.views.generic",
    "rest_framework.generics",
    "rest_framework.viewsets",
    "rest_framework.views",
    "rest_framework.mixins",
    "rest_framework.serializers",
    "flask.views",
)

# 표에 싣는 패키지 접두사다. 이 밖의 조상(builtins 등)은 싣지 않는다.
PACKAGES = ("django.", "rest_framework.", "flask.")


class Source:
    """site-packages 안의 모듈을 파싱하고 이름을 정의 경로로 푼다."""

    def __init__(self, root):
        """
        :param root: site-packages 경로
        """
        self.root = Path(root)
        self.trees = {}

    def tree(self, module):
        """모듈을 파싱한다(캐시). 없으면 None이다.

        :param module: 점 경로
        :returns: ast.Module 또는 None
        """
        if module not in self.trees:
            base = self.root / module.replace(".", "/")
            path = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
            self.trees[module] = ast.parse(path.read_text(encoding="utf-8")) if path.is_file() else None
        return self.trees[module]

    def is_package(self, module):
        """모듈이 패키지인지 돌려준다.

        :param module: 점 경로
        :returns: 패키지면 True
        """
        return (self.root / module.replace(".", "/") / "__init__.py").is_file()

    def absolute(self, module, statement):
        """상대 import를 절대 이름으로 푼다.

        :param module: import가 있는 모듈
        :param statement: ImportFrom
        :returns: 절대 모듈 이름
        """
        if statement.level == 0:
            return statement.module
        parts = module.split(".") if self.is_package(module) else module.split(".")[:-1]
        parts = parts[: len(parts) - (statement.level - 1)]
        return ".".join(parts + ([statement.module] if statement.module else []))

    def resolve(self, module, name, depth=0):
        """모듈 수준 이름을 정의 경로로 푼다.

        :param module: 모듈
        :param name: 이름(점 경로 가능)
        :param depth: 재귀 깊이
        :returns: 정의 경로 또는 None
        """
        tree = self.tree(module)
        if tree is None or depth > 16:
            return None
        head, _, rest = name.partition(".")
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef) and statement.name == head and not rest:
                return f"{module}.{head}"
            if isinstance(statement, ast.ImportFrom):
                base = self.absolute(module, statement)
                for alias in statement.names:
                    if (alias.asname or alias.name) == head:
                        target = f"{base}.{alias.name}"
                        if self.tree(target) is not None:
                            return self.resolve(target, rest, depth + 1) if rest else None
                        found = self.resolve(base, alias.name, depth + 1)
                        return found if not rest else None
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if (alias.asname or alias.name.split(".")[0]) == head and rest:
                        target = alias.name if alias.asname else head
                        return self.resolve_dotted(f"{target}.{rest}", depth + 1)
        return None

    def resolve_dotted(self, dotted, depth=0):
        """`pkg.mod.Name` 형태를 푼다.

        :param dotted: 점 경로
        :param depth: 재귀 깊이
        :returns: 정의 경로 또는 None
        """
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:cut])
            if self.tree(module) is not None:
                return self.resolve(module, ".".join(parts[cut:]), depth + 1)
        return None

    def class_node(self, dotted):
        """정의 경로의 ClassDef를 찾는다.

        :param dotted: 정의 경로
        :returns: (모듈, ClassDef)
        """
        module, _, name = dotted.rpartition(".")
        for statement in self.tree(module).body:
            if isinstance(statement, ast.ClassDef) and statement.name == name:
                return module, statement
        raise LookupError(dotted)


def base_name(node):
    """기반 클래스 식을 점 경로 문자열로 바꾼다.

    :param node: 식
    :returns: 점 경로 또는 None
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = base_name(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def bases(source, dotted):
    """클래스의 기반 클래스 정의 경로 목록이다(패키지 밖 조상은 뺀다).

    :param source: 소스
    :param dotted: 정의 경로
    :returns: 정의 경로 목록
    """
    module, node = source.class_node(dotted)
    result = []
    for base in node.bases:
        name = base_name(base)
        found = source.resolve(module, name) if name else None
        if found is not None and found.startswith(PACKAGES):
            result.append(found)
    return result


def c3(source, dotted, cache):
    """C3 선형화다.

    :param source: 소스
    :param dotted: 정의 경로
    :param cache: 결과 캐시
    :returns: 정의 경로 목록
    """
    if dotted not in cache:
        parents = bases(source, dotted)
        sequences = [list(c3(source, parent, cache)) for parent in parents] + [list(parents)]
        result = [dotted]
        while any(sequences):
            for sequence in sequences:
                if not sequence:
                    continue
                head = sequence[0]
                if not any(head in other[1:] for other in sequences):
                    break
            else:
                raise ValueError(f"inconsistent MRO for {dotted}")
            result.append(head)
            for other in sequences:
                if other and other[0] == head:
                    other.pop(0)
        cache[dotted] = result
    return cache[dotted]


def method_names(function):
    """메서드 본문이 self로 호출·읽기하는 속성 이름과 super 이름을 모은다.

    :param function: 함수 정의
    :returns: (호출 이름 집합, 읽기 이름 집합, super 이름 집합)
    """
    positional = function.args.posonlyargs + function.args.args
    first = positional[0].arg if positional else None
    receivers = {"self"} | ({first} if first else set())
    callees = {id(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
    calls, reads, supers = set(), set(), set()
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in receivers:
            if isinstance(node.ctx, ast.Load):
                (calls if id(node) in callees else reads).add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in receivers:
                if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    reads.add(node.args[1].value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == first == "cls":
            calls.add("__init__")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "super"
        ):
            supers.add(node.attr)
    return calls, reads - calls, supers


def is_property(function):
    """property·cached_property 장식자가 있는지 본다.

    :param function: 함수 정의
    :returns: property면 True
    """
    for decorator in function.decorator_list:
        name = decorator.attr if isinstance(decorator, ast.Attribute) else getattr(decorator, "id", "")
        if name in ("property", "cached_property"):
            return True
    return False


def class_entry(source, dotted, mro):
    """클래스 하나의 표 항목을 만든다.

    :param source: 소스
    :param dotted: 정의 경로
    :param mro: 선형화
    :returns: 항목 dict
    """
    _, node = source.class_node(dotted)
    methods, attributes = {}, set()
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls, reads, supers = method_names(statement)
            entry = {"calls": sorted(calls), "reads": sorted(reads), "super": sorted(supers)}
            if is_property(statement):
                entry["property"] = True
            methods[statement.name] = entry
        elif isinstance(statement, ast.Assign):
            attributes.update(target.id for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            attributes.add(statement.target.id)
    return {"mro": list(mro), "methods": dict(sorted(methods.items())), "attributes": sorted(attributes)}


def aliases(source, known):
    """공개 재수출 경로를 모은다.

    :param source: 소스
    :param known: 표의 정의 경로 집합
    :returns: 공개 경로 → 정의 경로
    """
    result = {}
    for module in ALIAS_MODULES:
        for statement in source.tree(module).body:
            names = []
            if isinstance(statement, ast.ImportFrom):
                names = [alias.asname or alias.name for alias in statement.names]
            elif isinstance(statement, ast.ClassDef):
                names = [statement.name]
            for name in names:
                found = source.resolve(module, name)
                if found in known and f"{module}.{name}" != found:
                    result[f"{module}.{name}"] = found
    return dict(sorted(result.items()))


def build(site_packages):
    """표 전체를 만든다.

    :param site_packages: site-packages 경로
    :returns: (클래스 표, 별칭 표)
    """
    source = Source(site_packages)
    cache = {}
    for dotted in START_CLASSES:
        c3(source, dotted, cache)
    classes = {dotted: class_entry(source, dotted, mro) for dotted, mro in sorted(cache.items())}
    return classes, aliases(source, set(classes))


def render(classes, alias_table):
    """생성 파일 내용을 만든다.

    :param classes: 클래스 표
    :param alias_table: 별칭 표
    :returns: 파이썬 소스 문자열
    """
    header = (
        '"""생성 파일: 프레임워크 기반 클래스의 선형화·메서드가 읽는 self 속성·클래스 속성 표.\n\n'
        "`experiments/graph/dump_framework_hooks.py`가 Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3 설치본\n"
        "소스를 ast로만 읽어 만든다. 직접 고치지 않는다. 의미는 `docs/GRAPH.md`의 프레임워크 디스패치 절을 본다.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
    )
    body = (
        "#: 정의 경로 → {mro, methods: {이름: {calls, reads, super, property?}}, attributes}.\n"
        f"FRAMEWORK_CLASSES: dict[str, Any] = {pprint.pformat(classes, width=116, sort_dicts=True)}\n\n"
        "#: 공개 재수출 경로 → 정의 경로.\n"
        f"FRAMEWORK_ALIASES: dict[str, str] = {pprint.pformat(alias_table, width=116, sort_dicts=True)}\n"
    )
    return header + body


def main():
    """표를 만들어 쓰거나 확인한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    text = render(*build(arguments.site_packages))
    if arguments.check:
        same = OUTPUT.read_text(encoding="utf-8") == text
        print("framework table is up to date" if same else "framework table differs; regenerate it")
        sys.exit(0 if same else 1)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
