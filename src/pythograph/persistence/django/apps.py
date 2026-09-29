"""Django 앱 라벨 해석: `INSTALLED_APPS` 원소 → (앱 이름, 라벨), 모듈 → 담는 앱의 라벨.

규칙은 Django 5.2.17 소스로 확인했다:

- `AppConfig.create(entry)`: 원소가 모듈이면 그 `apps` 하위 모듈에서 `default = False`가 아닌 `AppConfig`
  하위 클래스가 정확히 하나면 그것을, 아니면 `default = True`인 것 하나를 쓰고, 없으면 기본 `AppConfig`(이름 =
  원소)다. 원소가 클래스 경로면 그 클래스다. 클래스의 `name`이 앱 이름이다(`django/apps/config.py`).
- 라벨은 클래스 `label` 속성, 없으면 앱 이름의 마지막 조각이다(`AppConfig.__init__`).
- 모델의 앱은 모듈 경로가 앱 이름과 같거나 `앱 이름.`으로 시작하는 앱 중 가장 긴 이름이다
  (`Apps.get_containing_app_config`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.persistence.django.settings import DjangoProjectSettings
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable

#: AppConfig 상속 사슬을 따라가는 최대 깊이다.
MAX_APPCONFIG_DEPTH = 16

#: 자동 AppConfig 선택을 확정하지 못했다는 표식이다.
_UNDECIDED = "undecided"

#: Django `AppConfig`의 외부 점 경로다.
_APPCONFIG = ("django.apps.AppConfig", "django.apps.config.AppConfig")


@dataclass(frozen=True)
class InstalledApp:
    """설치한 앱 하나.

    Attributes:
        name: 앱 이름(모듈 점 경로).
        label: 앱 라벨(확정하지 못하면 None).
    """

    name: str
    label: str | None


class DjangoApps:
    """`INSTALLED_APPS`로 모듈의 앱 라벨을 푼다."""

    def __init__(self, symbols: SymbolTable, settings: DjangoProjectSettings) -> None:
        """해석기를 만든다.

        Args:
            symbols: 이름 해석기.
            settings: 읽은 Django 설정.
        """
        self.symbols = symbols
        self.settings = settings
        self.apps: list[InstalledApp] = []
        self.complete = settings.found and settings.apps_complete
        for entry in settings.installed_apps:
            app = self._resolve_entry(entry)
            if app is None:
                self.complete = False
            else:
                self.apps.append(app)

    def label_for_module(self, module: str) -> str | None:
        """모듈을 담는 앱의 라벨을 돌려준다.

        목록이 불완전하면 모듈이 알려진 앱의 `models` 모듈(`앱.models`, `앱.models.x`)일 때만 인정한다. 그 밖은
        모르는 앱이 더 안쪽에 있을 수 있어 None이다.

        Args:
            module: 모델을 정의한 모듈 점 경로.

        Returns:
            라벨 또는 None.
        """
        candidates = [app for app in self.apps if module == app.name or module.startswith(app.name + ".")]
        if not candidates:
            return None
        chosen = max(candidates, key=lambda app: len(app.name))
        if not self.complete and not (module == f"{chosen.name}.models" or module.startswith(f"{chosen.name}.models.")):
            return None
        return chosen.label

    def _resolve_entry(self, entry: str) -> InstalledApp | None:
        """`INSTALLED_APPS` 원소 하나를 앱으로 푼다.

        Args:
            entry: 원소 문자열.

        Returns:
            앱, 프로젝트 안 원소를 확정하지 못하면 None. 프로젝트 밖 원소는 이름의 마지막 조각을 라벨로 둔다.
        """
        if self.symbols.project.resolve_module(entry) is not None:
            config = self._default_config(entry)
            if isinstance(config, str):
                return None
            return InstalledApp(entry, entry.rpartition(".")[2]) if config is None else self._from_config(config)
        module, _, name = entry.rpartition(".")
        module_path = self.symbols.project.resolve_module(module) if module else None
        if module_path is not None:
            symbol = self.symbols.resolve_name(module_path, name)
            if isinstance(symbol, ProjectSymbol) and self._is_app_config(symbol, 0):
                return self._from_config(symbol)
            return None
        if name[:1].isupper():
            # 외부 AppConfig 클래스 경로의 이름은 소스를 읽어야 안다. 프로젝트 모듈을 담지 않으므로 넘긴다.
            return InstalledApp(entry, None)
        return InstalledApp(entry, name)

    def _default_config(self, entry: str) -> ProjectSymbol | None | str:
        """앱 모듈의 `apps` 하위 모듈에서 자동으로 고를 AppConfig를 찾는다.

        Args:
            entry: 앱 모듈 이름.

        Returns:
            고른 클래스, 없으면 None, 확정할 수 없으면 `_UNDECIDED`.
        """
        apps_path = self.symbols.project.resolve_module(f"{entry}.apps")
        index = self.symbols.index(apps_path) if apps_path else None
        if index is None or apps_path is None:
            return None
        configs = [
            ProjectSymbol(apps_path, statement.name, statement)
            for statement in index.module.tree.body
            if isinstance(statement, ast.ClassDef)
        ]
        # `inspect.getmembers`는 import한 클래스도 후보로 본다. 프로젝트 클래스는 따라가고, 이름이 `Config`로 끝나는
        # 외부 클래스는 AppConfig인지 알 수 없어 고르지 않는다(앱을 확정하지 못함).
        for name in index.imports:
            imported = self.symbols.resolve_name(apps_path, name)
            if isinstance(imported, ProjectSymbol) and isinstance(imported.node, ast.ClassDef):
                configs.append(imported)
            elif (
                isinstance(imported, ExternalSymbol)
                and imported.dotted.endswith("Config")
                and imported.dotted not in _APPCONFIG
            ):
                return _UNDECIDED
        configs = [config for config in configs if self._is_app_config(config, 0)]
        enabled = [config for config in configs if self._attribute(config, "default", 0) is not False]
        if len(enabled) == 1:
            return enabled[0]
        defaults = [config for config in enabled if self._attribute(config, "default", 0) is True]
        return defaults[0] if len(defaults) == 1 else None

    def _from_config(self, config: ProjectSymbol) -> InstalledApp | None:
        """AppConfig 클래스에서 앱 이름과 라벨을 읽는다.

        Args:
            config: AppConfig 클래스.

        Returns:
            앱, `name`을 리터럴로 읽지 못하면 None.
        """
        name = self._attribute(config, "name", 0)
        if not isinstance(name, str):
            return None
        label = self._attribute(config, "label", 0)
        if label is None:
            return InstalledApp(name, name.rpartition(".")[2])
        return InstalledApp(name, label if isinstance(label, str) else None)

    def _is_app_config(self, symbol: Symbol | None, depth: int) -> bool:
        """클래스가 Django `AppConfig`를 상속하는지 본다.

        Args:
            symbol: 해석 결과.
            depth: 상속 깊이.

        Returns:
            상속하면 True.
        """
        if isinstance(symbol, ExternalSymbol):
            return symbol.dotted in _APPCONFIG
        if not isinstance(symbol, ProjectSymbol) or not isinstance(symbol.node, ast.ClassDef):
            return False
        if depth > MAX_APPCONFIG_DEPTH:
            return False
        return any(
            self._is_app_config(self.symbols.resolve_expr(symbol.path, base), depth + 1) for base in symbol.node.bases
        )

    def _attribute(self, config: ProjectSymbol, name: str, depth: int) -> object:
        """클래스 속성을 사슬을 따라 리터럴로 읽는다.

        Args:
            config: 클래스.
            name: 속성 이름.
            depth: 상속 깊이.

        Returns:
            값, 없으면 None, 리터럴이 아니면 `...`.
        """
        node = config.node
        assert isinstance(node, ast.ClassDef)
        for statement in node.body:
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets, value = [statement.target], statement.value
            else:
                continue
            if value is not None and any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return _literal(value)
        if depth > MAX_APPCONFIG_DEPTH:
            return ...
        for base in node.bases:
            resolved = self.symbols.resolve_expr(config.path, base)
            if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                found = self._attribute(resolved, name, depth + 1)
                if found is not None:
                    return found
        return None


def _literal(node: ast.expr) -> object:
    """리터럴 식만 값으로 바꾼다.

    Args:
        node: 식.

    Returns:
        값, 리터럴이 아니면 `...`.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return ...
