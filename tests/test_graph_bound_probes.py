"""`bound` 건전성 탐침: 무작위 합성 프로그램을 실행해 관찰한 수신자 클래스가 `bound` 대상에 모두 있는지 본다.

pythograph 제품 코드는 분석 대상을 실행하지 않는다. 이 테스트만 **테스트가 생성한 합성 프로그램**을 격리한 모듈 객체로
실행해 정답(런타임에 수신자로 들어온 클래스)을 얻는다. 탐침은 흐름을 숨기는 기법을 섞는다: `*args`·`**kwargs` 펼치기,
`functools.partial`, `map`, 고차 함수, `getattr(sys.modules[__name__], …)`, `globals()`, `exec`, 감싸는 장식자, `global`
대입, 모듈 몽키패치, 리터럴·계산된 `setattr`, `__dict__`·`vars()` 쓰기, `type(h)(…)`·`h.__class__(…)`, `nonlocal`.
수신자 흐름이 열린(대상을 모르는) 계산된 `setattr`은 문서화한 모델링 밖의 틈(`bound-assumptions:`)이라 만들지
않는다 — 보관 변수를 탐침마다 따로 둔다.

판정: 호출 지점에 `bound` 간선이 있거나 direct로 확정했으면(한 번 대입한 모듈 전역·클래스 본문 속성의 정확한 값),
런타임에 관찰한 모든 클래스 X의 `X.run` 정점이 그 호출 정점의 대상에 있어야 한다(빠진 구현이 없다). 탐침이 의미
있도록 전체에서 `bound`로 이은 탐침과 기법 때문에 열린 탐침이 충분히 나와야 한다.
"""

from __future__ import annotations

import random
import sys
import textwrap
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 모든 탐침 프로그램이 공유하는 머리다(클래스 넷, 팩토리, 고차 함수, 감싸는 장식자).
PRELUDE = """
import copy
import functools
import sys

LOG = []
NAME = "r"


class A:
    def run(self):
        return "A"


class B(A):
    def run(self):
        return "B"


class C(A):
    pass


class D:
    def run(self):
        return "D"


def make_a():
    return A()


def make_bc(flag):
    return B() if flag else C()


def apply(function, value):
    return function(value)


def wrap(function):
    def wrapper(*args, **kwargs):
        return function(D())

    return wrapper
"""

#: 수신자로 흘려보낼 값 식이다.
VALUES = ["A()", "B()", "C()", "D()", "make_a()", "make_bc(True)", "make_bc(False)", "None"]

#: 매개변수 탐침에 숨은 흐름을 넣는 기법이다(`{p}`는 탐침 함수 이름).
PARAM_TRICKS = [
    "{p}(*[D()])",
    '{p}(**{{"r": D()}})',
    "functools.partial({p}, D())()",
    "list(map({p}, [D()]))",
    "apply({p}, D())",
    'getattr(sys.modules[__name__], "{p}")(D())',
    'globals()["{p}"](D())',
    'exec("{p}(D())", globals())',
    "wrap({p})(A())",
    'handlers = {{"k": {p}}}; handlers["k"](D())',
]

#: 모듈 전역 탐침의 기법이다(`{g}`는 전역 이름, `{s}`는 전역 설정 함수 이름).
GLOBAL_TRICKS = [
    "{s}()",
    'setattr(sys.modules[__name__], "{g}", D())',
    'globals()["{g}"] = D()',
    "sys.modules[__name__].{g} = D()",
]

#: 속성 탐침의 기법이다(`{h}`는 탐침마다 다른 보관 변수, `{k}`는 보관 클래스 이름).
ATTRIBUTE_TRICKS = [
    "{h}.r = D()",
    'setattr({h}, "r", D())',
    "setattr({h}, NAME, D())",
    '{h}.__dict__["r"] = D()',
    'vars({h})["r"] = D()',
    "{h} = {k}(*[D()])",
    "{h} = type({h})(D())",
    "{h} = {h}.__class__(D())",
    "{h} = copy.copy({h})",
]


@dataclass
class Program:
    """생성한 탐침 프로그램.

    Attributes:
        definitions: 모듈 수준 정의 소스 조각.
        main: `main()` 본문 줄.
        probes: 탐침 기록 이름 → 호출을 담은 정점 id의 점 경로.
    """

    definitions: list[str] = field(default_factory=list)
    main: list[str] = field(default_factory=list)
    probes: dict[str, str] = field(default_factory=dict)

    def source(self) -> str:
        """프로그램 소스를 만든다.

        Returns:
            소스.
        """
        body = "\n".join(f"    {line}" for line in self.main) or "    pass"
        return PRELUDE + "\n\n" + "\n\n\n".join(self.definitions) + f"\n\n\ndef main():\n{body}\n"


def _record(label: str, receiver: str, indent: str) -> str:
    """탐침 줄: 수신자 클래스를 기록하고 메서드를 부른다.

    Args:
        label: 기록 이름.
        receiver: 수신자 식.
        indent: 들여쓰기.

    Returns:
        소스 조각.
    """
    return (
        f"{indent}if {receiver} is not None:\n"
        f'{indent}    LOG.append(("{label}", {receiver}.__class__.__name__))\n'
        f"{indent}    {receiver}.run()"
    )


def _param_probe(program: Program, index: int, generator: random.Random) -> None:
    """함수 매개변수 탐침(모든 호출 지점의 실인자 흐름).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    name = f"probe_{index}"
    decorator = "@wrap\n" if generator.random() < 0.1 else ""
    program.definitions.append(f"{decorator}def {name}(r):\n{_record(name, 'r', '    ')}")
    program.probes[name] = name
    for _ in range(generator.randint(1, 3)):
        program.main.append(f"{name}({generator.choice(VALUES)})")
    if generator.random() < 0.5:
        program.main.append(generator.choice(PARAM_TRICKS).format(p=name))


def _local_probe(program: Program, index: int, generator: random.Random) -> None:
    """두 번 묶인 지역 이름 탐침(가끔 `nonlocal`로 다시 묶는다).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    name = f"probe_{index}"
    first, second = generator.choice(VALUES), generator.choice(VALUES)
    lines = [f"def {name}(flag):", f"    r = {first}", "    if flag:", f"        r = {second}"]
    if generator.random() < 0.3:
        lines += [
            "",
            "    def reset():",
            "        nonlocal r",
            "        r = D()",
            "",
            "    if flag == 2:",
            "        reset()",
        ]
        program.main.append(f"{name}(2)")
    lines.append(_record(name, "r", "    "))
    program.definitions.append("\n".join(lines))
    program.probes[name] = name
    program.main += [f"{name}(True)", f"{name}(False)"]


def _global_probe(program: Program, index: int, generator: random.Random) -> None:
    """조건부 모듈 전역 탐침(`global` 대입·몽키패치 기법).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    name, variable, setter = f"probe_{index}", f"g_{index}", f"set_g_{index}"
    flag = generator.choice(["True", "False"])
    first, second, third = (generator.choice(VALUES) for _ in range(3))
    program.definitions.append(
        f"if {flag}:\n    {variable} = {first}\nelse:\n    {variable} = {second}\n\n\n"
        f"def {setter}():\n    global {variable}\n    {variable} = {third}\n\n\n"
        f"def {name}():\n{_record(name, variable, '    ')}"
    )
    program.probes[name] = name
    program.main.append(f"{name}()")
    if generator.random() < 0.6:
        program.main += [generator.choice(GLOBAL_TRICKS).format(g=variable, s=setter), f"{name}()"]


def _attribute_probe(program: Program, index: int, generator: random.Random) -> None:
    """`__init__` 매개변수로 받은 인스턴스 속성 탐침(속성 쓰기 기법).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    holder = f"Holder_{index}"
    label = f"{holder}.probe"
    program.definitions.append(
        f"class {holder}:\n    def __init__(self, r):\n        self.r = r\n\n"
        f"    def probe(self):\n{_record(label, 'self.r', '        ')}"
    )
    program.probes[label] = label
    variable = f"h_{index}"
    program.main += [f"{variable} = {holder}({generator.choice(VALUES)})", f"{variable}.probe()"]
    if generator.random() < 0.6:
        trick = generator.choice(ATTRIBUTE_TRICKS).format(h=variable, k=holder)
        program.main += [trick, f"{variable}.probe()"]


def _factory_probe(program: Program, index: int, generator: random.Random) -> None:
    """팩토리 반환 값 탐침.

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    name, factory = f"probe_{index}", f"factory_{index}"
    first, second = generator.choice(VALUES), generator.choice(VALUES)
    program.definitions.append(
        f"def {factory}(flag):\n    if flag:\n        return {first}\n    return {second}\n\n\n"
        f"def {name}(flag):\n    r = {factory}(flag)\n{_record(name, 'r', '    ')}"
    )
    program.probes[name] = name
    program.main += [f"{name}(True)", f"{name}(False)"]


#: 한 번 대입한 모듈 전역 탐침의 기법이다(`{g}`는 전역 이름, `{s}`는 전역 설정 함수 이름).
SINGLE_GLOBAL_TRICKS = [
    "{s}()",
    'setattr(sys.modules[__name__], "{g}", D())',
    'globals()["{g}"] = D()',
    "sys.modules[__name__].{g} = D()",
]

#: 클래스 본문 속성 탐침의 기법이다(`{k}`는 클래스 이름, `{b}`는 인스턴스 변수).
CLASS_ATTRIBUTE_TRICKS = ["{k}.r = D()", "{b}.r = D()", 'setattr({k}, "r", D())', 'setattr({b}, "r", D())']


def _single_global_probe(program: Program, index: int, generator: random.Random) -> None:
    """한 번 대입한 모듈 전역 탐침(direct가 정확한 값으로 보는 자리 — 다시 쓰이면 모른다).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    name, variable, setter = f"probe_{index}", f"g_{index}", f"set_g_{index}"
    first, second = generator.choice(VALUES), generator.choice(VALUES)
    program.definitions.append(
        f"{variable} = {first}\n\n\n"
        f"def {setter}():\n    global {variable}\n    {variable} = {second}\n\n\n"
        f"def {name}():\n{_record(name, variable, '    ')}"
    )
    program.probes[name] = name
    program.main.append(f"{name}()")
    if generator.random() < 0.6:
        program.main += [generator.choice(SINGLE_GLOBAL_TRICKS).format(g=variable, s=setter), f"{name}()"]


def _class_attribute_probe(program: Program, index: int, generator: random.Random) -> None:
    """클래스 본문 속성 탐침(클래스·인스턴스 쓰기가 가릴 수 있다).

    Args:
        program: 채울 프로그램.
        index: 탐침 번호.
        generator: 난수 생성기.
    """
    holder, variable = f"Box_{index}", f"b_{index}"
    label = f"{holder}.probe"
    program.definitions.append(
        f"class {holder}:\n    r = {generator.choice(VALUES)}\n\n"
        f"    def probe(self):\n{_record(label, 'self.r', '        ')}"
    )
    program.probes[label] = label
    program.main += [f"{variable} = {holder}()", f"{variable}.probe()"]
    if generator.random() < 0.6:
        trick = generator.choice(CLASS_ATTRIBUTE_TRICKS).format(k=holder, b=variable)
        program.main += [trick, f"{variable}.probe()"]


#: 탐침 종류다.
PROBES = [
    _param_probe,
    _local_probe,
    _global_probe,
    _attribute_probe,
    _factory_probe,
    _single_global_probe,
    _class_attribute_probe,
]


def generate(seed: int) -> Program:
    """탐침 프로그램 하나를 만든다.

    Args:
        seed: 난수 씨앗.

    Returns:
        프로그램.
    """
    generator = random.Random(seed)
    program = Program()
    for index in range(generator.randint(2, 5)):
        generator.choice(PROBES)(program, index, generator)
    return program


def observe(source: str, name: str) -> dict[str, set[str]]:
    """합성 프로그램을 격리한 모듈로 실행해 탐침별 수신자 클래스를 모은다.

    Args:
        source: 프로그램 소스.
        name: 모듈 이름(`sys.modules`에 잠시 등록한다).

    Returns:
        기록 이름 → 클래스 이름 집합.
    """
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, f"{name}.py", "exec"), module.__dict__)  # 테스트가 만든 합성 코드만 실행한다
        module.main()
    finally:
        del sys.modules[name]
    observed: dict[str, set[str]] = {}
    for label, class_name in module.LOG:
        observed.setdefault(label, set()).add(class_name)
    return observed


def call_targets(graph: CallGraph, source: str, evidence: str) -> set[str]:
    """정점에서 `.run`으로 가는 간선의 도착 정점이다.

    Args:
        graph: 그래프.
        source: 출발 정점 id.
        evidence: 근거 등급.

    Returns:
        도착 id 집합.
    """
    return {
        edge.target
        for edge in graph.edges
        if edge.source == source and edge.evidence == evidence and edge.target.endswith(".run")
    }


#: 모든 씨앗에 걸친 집계(탐침이 헛돌지 않는지 확인한다).
TALLY = {"bound": 0, "direct": 0, "open": 0}


@pytest.mark.parametrize("seed", range(150))
def test_bound_edges_cover_every_runtime_receiver(make_project: MakeProject, seed: int) -> None:
    """`bound` 간선이 있는 호출 지점은 런타임에 들어온 모든 수신자 클래스의 구현을 잇는다."""
    program = generate(seed)
    source = program.source()
    graph = build_graph(Project.open(make_project({"m.py": textwrap.dedent(source)})), False)
    observed = observe(source, f"pythograph_probe_{seed}")
    for label, dotted in program.probes.items():
        node = f"m.py#{dotted}"
        bound, direct = call_targets(graph, node, "bound"), call_targets(graph, node, "direct")
        if not bound and not direct:
            TALLY["open"] += 1
            continue
        TALLY["bound" if bound else "direct"] += 1
        # bound로 이었거나 direct로 확정한(정확한 수신자) 호출은 런타임 수신자를 모두 덮어야 한다.
        missing = {name for name in observed.get(label, set()) if f"m.py#{name}.run" not in bound | direct}
        assert not missing, (seed, label, missing, sorted(bound | direct), source)
    for node in graph.nodes:
        assert node.unresolved["direct"] >= node.unresolved["bound"] >= node.unresolved["candidates"]


def test_probes_exercise_both_outcomes() -> None:
    """탐침이 의미 있다: `bound`로 이은 탐침과 열린 탐침이 모두 충분히 나온다(앞 테스트 뒤에 실행된다)."""
    if TALLY["bound"] + TALLY["open"] == 0:
        pytest.skip("탐침 테스트를 함께 실행할 때만 의미가 있다")
    assert TALLY["bound"] >= 100
    assert TALLY["open"] >= 100
