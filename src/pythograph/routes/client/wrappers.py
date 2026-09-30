"""`http-wrappers` v1 선언 읽기와 래퍼 호출 인자 바인딩.

정본은 isthmus `docs/HTTP-WRAPPERS.md`다. 선언 파일에는 여러 언어의 래퍼가 함께 있을 수 있고 pythograph는
`"language": "python"` 항목만 적용한다. 다른 언어 항목도 스키마는 검사한다(모르는 필드는 선언 오류다 — 조용히 무시하면
낡은 선언이 호출 0건을 내어 "호출 없음"으로 읽힌다).

Python `owner` 규칙(pythograph id와 같은 모양):

- 메서드·생성자: 소유 클래스의 pythograph id(`api/client.py#ApiClient`). 래퍼 id는 `owner + "." + name`이고 생성자의
  `name`은 `__init__`이다(`kind: "constructor"` — 호출은 `ApiClient(...)`, 데이터 클래스처럼 `__init__`을 정의하지
  않아도 된다).
- 모듈 수준 함수: 모듈 파일 경로(`api/net.py`). 래퍼 id는 `owner + "#" + name`이다.

인자: `label`은 키워드 인자 이름, `index`는 호출에 쓴 위치 인자 자리(0부터, 메서드 호출의 수신자와 생성자의 `self`는
세지 않는다)다. 파이썬은 위치 인자가 키워드 인자보다 앞이므로 그 자리가 키워드면 쓰지 않는다는 계약 규칙과 같다.
그 자리 앞에 `*args`가 있거나 인자를 찾지 못했는데 `**kwargs`가 있으면 어느 값인지 모른다(dynamic).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pythograph.routes.client.compose import HTTP_VERBS

#: 선언 파일 최대 크기(바이트)다.
MAX_WRAPPERS_BYTES = 4 * 1024 * 1024

#: 선언 파일의 최대 항목 수다.
MAX_WRAPPERS = 10_000

#: 래퍼 항목에 올 수 있는 필드다.
_WRAPPER_FIELDS = frozenset(
    {
        "language",
        "kind",
        "owner",
        "name",
        "methodArg",
        "pathArg",
        "defaultMethod",
        "methodEnum",
        "pathAnchor",
        "service",
    }
)

#: 계약이 받는 언어다(`python`은 pythograph가 더한다).
_LANGUAGES = frozenset({"swift", "kotlin", "dart", "js", "python"})

#: 제어 문자다.
_CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f  ]")

#: 파이썬 모듈 경로 owner 모양이다.
_MODULE_OWNER = re.compile(r"[^#\s]+\.py")

#: 파이썬 클래스 owner 모양(`경로#점 경로`)이다.
_CLASS_OWNER = re.compile(r"[^#\s]+\.py#[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")

#: 인자를 찾았지만 어느 값인지 모른다는 표식이다.
BLOCKED = object()


class WrapperDeclarationError(Exception):
    """선언 파일이 스키마를 어겼다. 메시지는 항목 위치와 해결 방향을 담고 선언 원문은 싣지 않는다."""


@dataclass(frozen=True)
class ArgumentSpec:
    """인자 자리 선언(`methodArg`·`pathArg`)."""

    index: int | None = None
    label: str | None = None


@dataclass(frozen=True)
class WrapperDecl:
    """래퍼 선언 하나.

    Attributes:
        position: 파일 안 순번(`wrappers[n]`, limitation 문구용).
        language: 호출 측 언어.
        kind: `constructor`·`function`.
        owner: 소유 타입 또는 모듈 경로.
        name: 함수·메서드 이름(생성자면 `__init__`).
        method_arg: 동사 인자 자리(없으면 None).
        path_arg: 경로 인자 자리.
        default_method: 동사 인자를 생략했을 때의 동사.
        method_enum: enum case·상수 이름 → 동사.
        path_anchor: `root`·`base`.
        service: 래퍼 호출의 service.
    """

    position: int
    language: str
    kind: str
    owner: str
    name: str
    method_arg: ArgumentSpec | None
    path_arg: ArgumentSpec
    default_method: str | None
    method_enum: dict[str, str] = field(default_factory=dict)
    path_anchor: str = "base"
    service: str | None = None

    @property
    def target_id(self) -> str:
        """래퍼가 가리키는 pythograph id를 돌려준다.

        Returns:
            메서드·생성자는 `owner.name`, 모듈 함수는 `owner#name`.
        """
        return f"{self.owner}.{self.name}" if "#" in self.owner else f"{self.owner}#{self.name}"


@dataclass(frozen=True)
class CallArgument:
    """호출 인자 하나(쓴 순서).

    Attributes:
        label: 키워드 이름(위치 인자면 None, `**kwargs`도 None).
        value: 값(구문 노드 또는 벡터의 토큰).
        starred: `*args`·`**kwargs`인지.
    """

    label: str | None
    value: object
    starred: bool = False


@dataclass(frozen=True)
class MethodToken:
    """동사 인자 값의 분류.

    Attributes:
        kind: `literal`(문자열 리터럴), `enum`(enum case·상수 이름), `value`(그 밖).
        text: 리터럴 문자열 또는 case 이름.
    """

    kind: str
    text: str = ""


def load_wrappers(path: Path) -> list[WrapperDecl]:
    """선언 파일을 읽어 파이썬 래퍼를 돌려준다.

    Args:
        path: 선언 파일 경로.

    Returns:
        `language: "python"` 래퍼 목록.

    Raises:
        OSError: 파일을 읽지 못할 때.
        WrapperDeclarationError: 스키마 위반·크기 초과.
    """
    data = path.read_bytes()[: MAX_WRAPPERS_BYTES + 1]
    if len(data) > MAX_WRAPPERS_BYTES:
        raise WrapperDeclarationError("the wrappers file is larger than 4 MiB; split or trim it.")
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise WrapperDeclarationError("the wrappers file is not valid UTF-8 JSON; fix its syntax.") from error
    return parse_wrappers(document)


def parse_wrappers(document: object) -> list[WrapperDecl]:
    """선언 문서를 검사하고 파이썬 래퍼를 돌려준다.

    Args:
        document: JSON 값.

    Returns:
        파이썬 래퍼 목록.

    Raises:
        WrapperDeclarationError: 스키마 위반.
    """
    if not isinstance(document, dict) or set(document) != {"format", "version", "wrappers"}:
        raise WrapperDeclarationError('the wrappers file must have exactly "format", "version", and "wrappers".')
    if document["format"] != "http-wrappers" or document["version"] != 1:
        raise WrapperDeclarationError('the wrappers file must declare format "http-wrappers" version 1.')
    entries = document["wrappers"]
    if not isinstance(entries, list) or len(entries) > MAX_WRAPPERS:
        raise WrapperDeclarationError(f'"wrappers" must be an array of at most {MAX_WRAPPERS} entries.')
    declarations = [_parse_entry(position, entry) for position, entry in enumerate(entries)]
    return [declaration for declaration in declarations if declaration.language == "python"]


def _parse_entry(position: int, entry: object) -> WrapperDecl:
    """래퍼 항목 하나를 검사한다.

    Args:
        position: 항목 순번.
        entry: JSON 값.

    Returns:
        선언.

    Raises:
        WrapperDeclarationError: 스키마 위반.
    """
    where = f"wrappers[{position}]"
    if not isinstance(entry, dict):
        raise WrapperDeclarationError(f"{where} must be an object.")
    unknown = sorted(set(entry) - _WRAPPER_FIELDS)
    if unknown:
        raise WrapperDeclarationError(f"{where} has an unknown field; remove it or update pythograph.")
    language = _choice(entry, "language", _LANGUAGES, where)
    kind = _choice(entry, "kind", frozenset({"constructor", "function"}), where)
    owner = _text(entry, "owner", where)
    name = _text(entry, "name", where)
    method_arg = _argument(entry.get("methodArg"), f"{where}.methodArg") if "methodArg" in entry else None
    if "pathArg" not in entry:
        raise WrapperDeclarationError(f"{where} needs pathArg.")
    path_arg = _argument(entry["pathArg"], f"{where}.pathArg")
    default_method = _verb(entry.get("defaultMethod"), f"{where}.defaultMethod") if "defaultMethod" in entry else None
    if method_arg is None and default_method is None:
        raise WrapperDeclarationError(f"{where} needs methodArg or defaultMethod.")
    declaration = WrapperDecl(
        position=position,
        language=language,
        kind=kind,
        owner=owner,
        name=name,
        method_arg=method_arg,
        path_arg=path_arg,
        default_method=default_method,
        method_enum=_method_enum(entry.get("methodEnum", {}), where),
        path_anchor=_choice(entry, "pathAnchor", frozenset({"root", "base"}), where),
        service=_text(entry, "service", where) if "service" in entry else None,
    )
    if language == "python":
        _check_python_owner(declaration, where)
    return declaration


def _choice(entry: dict[str, object], key: str, allowed: frozenset[str], where: str) -> str:
    """닫힌 값 필드를 읽는다.

    Args:
        entry: 항목.
        key: 필드 이름.
        allowed: 받는 값.
        where: 위치 문구.

    Returns:
        값.

    Raises:
        WrapperDeclarationError: 없거나 받지 않는 값.
    """
    value = entry.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise WrapperDeclarationError(f"{where}.{key} must be one of {', '.join(sorted(allowed))}.")
    return value


def _text(entry: dict[str, object], key: str, where: str) -> str:
    """비어 있지 않은 문자열 필드를 읽는다.

    Args:
        entry: 항목.
        key: 필드 이름.
        where: 위치 문구.

    Returns:
        값.

    Raises:
        WrapperDeclarationError: 없거나 형식 위반.
    """
    value = entry.get(key)
    if not isinstance(value, str) or not value or len(value) > 1024 or _CONTROL.search(value):
        raise WrapperDeclarationError(f"{where}.{key} must be a non-empty string without control characters.")
    return value


def _argument(value: object, where: str) -> ArgumentSpec:
    """인자 자리 선언을 읽는다.

    Args:
        value: JSON 값.
        where: 위치 문구.

    Returns:
        인자 자리.

    Raises:
        WrapperDeclarationError: 형식 위반.
    """
    if not isinstance(value, dict) or not value or set(value) - {"index", "label"}:
        raise WrapperDeclarationError(f"{where} must be an object with index and/or label.")
    index = value.get("index")
    label = value.get("label")
    if index is not None and (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 255):
        raise WrapperDeclarationError(f"{where}.index must be an integer from 0 to 255.")
    if label is not None and (not isinstance(label, str) or not label.isidentifier()):
        raise WrapperDeclarationError(f"{where}.label must be an argument name.")
    return ArgumentSpec(index, label)


def _verb(value: object, where: str) -> str:
    """동사 필드를 읽는다.

    Args:
        value: JSON 값.
        where: 위치 문구.

    Returns:
        동사.

    Raises:
        WrapperDeclarationError: 계약 동사가 아닐 때.
    """
    if not isinstance(value, str) or value not in HTTP_VERBS:
        raise WrapperDeclarationError(f"{where} must be an upper-case HTTP verb such as GET.")
    return value


def _method_enum(value: object, where: str) -> dict[str, str]:
    """`methodEnum`을 읽는다.

    Args:
        value: JSON 값.
        where: 위치 문구.

    Returns:
        case 이름 → 동사.

    Raises:
        WrapperDeclarationError: 형식 위반.
    """
    if not isinstance(value, dict):
        raise WrapperDeclarationError(f"{where}.methodEnum must be an object.")
    return {str(key): _verb(verb, f"{where}.methodEnum") for key, verb in value.items()}


def _check_python_owner(declaration: WrapperDecl, where: str) -> None:
    """파이썬 래퍼의 owner·name 모양을 검사한다.

    Args:
        declaration: 선언.
        where: 위치 문구.

    Raises:
        WrapperDeclarationError: 모양 위반.
    """
    is_class = _CLASS_OWNER.fullmatch(declaration.owner) is not None
    is_module = _MODULE_OWNER.fullmatch(declaration.owner) is not None
    if declaration.kind == "constructor" and (not is_class or declaration.name != "__init__"):
        raise WrapperDeclarationError(
            f"{where}: a python constructor wrapper needs owner '<path>.py#<Class>' and name '__init__'."
        )
    if not (is_class or is_module) or not declaration.name.isidentifier():
        raise WrapperDeclarationError(
            f"{where}: a python owner is '<path>.py' for a module function or '<path>.py#<Class>' for a method."
        )


def find_argument(spec: ArgumentSpec, arguments: list[CallArgument]) -> CallArgument | object | None:
    """선언한 자리의 인자를 찾는다(`wrapper.method` 바인딩 규칙).

    Args:
        spec: 인자 자리 선언.
        arguments: 쓴 순서의 호출 인자.

    Returns:
        인자, 없으면 None, 있을 수 있지만 어느 값인지 모르면 `BLOCKED`.
    """
    if spec.label is not None:
        for argument in arguments:
            if argument.label == spec.label and not argument.starred:
                return argument
    if spec.index is not None:
        if any(argument.starred and argument.label is None for argument in arguments[: spec.index + 1]):
            return BLOCKED
        if spec.index < len(arguments):
            candidate = arguments[spec.index]
            if candidate.label is None and not candidate.starred:
                return candidate
    if any(argument.starred for argument in arguments):
        return BLOCKED
    return None


def bind_method(
    declaration: WrapperDecl, arguments: list[CallArgument], token: Callable[[object], MethodToken]
) -> str | None:
    """래퍼 호출의 동사를 정한다.

    Args:
        declaration: 래퍼 선언.
        arguments: 호출 인자.
        token: 인자 값을 동사 토큰으로 바꾸는 함수.

    Returns:
        동사, 모르면 None(`methodDynamic`).
    """
    if declaration.method_arg is None:
        return declaration.default_method
    argument = find_argument(declaration.method_arg, arguments)
    if argument is None:
        return declaration.default_method
    if not isinstance(argument, CallArgument):
        return None
    value = token(argument.value)
    if value.kind == "literal":
        return value.text if value.text in HTTP_VERBS else None
    if value.kind == "enum":
        return declaration.method_enum.get(value.text)
    return None
