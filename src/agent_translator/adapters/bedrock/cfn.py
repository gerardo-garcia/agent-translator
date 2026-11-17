"""CloudFormation helpers: YAML with short-form intrinsics, reference analysis, name sanitising.

Intrinsics are kept in their JSON long form (``{"Ref": x}``, ``{"Fn::Sub": ...}``) so they survive
inside the IR's JSON-serialisable ``extensions``.
"""
from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode
from ruamel.yaml.scalarstring import LiteralScalarString

AGENT = "AWS::Bedrock::Agent"
ALIAS = "AWS::Bedrock::AgentAlias"
GUARDRAIL = "AWS::Bedrock::Guardrail"


def _intrinsic(constructor, suffix: str, node):
    if isinstance(node, ScalarNode):
        val: Any = constructor.construct_scalar(node)
    elif isinstance(node, MappingNode):
        val = constructor.construct_mapping(node, deep=True)
    else:
        val = constructor.construct_sequence(node, deep=True)
    if suffix in ("Ref", "Condition"):
        return {suffix: val}
    if suffix == "GetAtt" and isinstance(val, str):
        val = val.split(".", 1)
    return {f"Fn::{suffix}": val}


def loader() -> YAML:
    y = YAML(typ="safe", pure=True)
    y.constructor.add_multi_constructor("!", _intrinsic)
    return y


def is_intrinsic(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and (
        next(iter(v)) in ("Ref", "Condition") or next(iter(v)).startswith("Fn::"))


def _walk(v: Any):
    yield v
    if isinstance(v, dict):
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def refs_in(v: Any) -> set[str]:
    """Names a value depends on. Non-local dependencies are reported as ``kind:name`` tokens."""
    out: set[str] = set()
    for x in _walk(v):
        if not is_intrinsic(x):
            continue
        (k, a), = x.items()
        if k == "Ref":
            out.add(a)
        elif k == "Fn::GetAtt":
            out.add(a[0] if isinstance(a, list) else str(a).split(".")[0])
        elif k == "Fn::Sub":
            s, local = (a, {}) if isinstance(a, str) else (a[0], a[1])
            out.update(m.split(".")[0] for m in re.findall(r"\$\{([^}!]+)\}", s)
                       if m.split(".")[0] not in local)
        else:
            out.add(f"{k}:")
    return out


def is_pseudo(name: str) -> bool:
    return name.startswith("AWS::")


def resolve_text(v: Any, params: dict[str, Any], keep_pseudo: bool = False) -> str | None:
    """Plain string for a literal, a parameter with a default, or a Sub over such parameters."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and set(v) == {"Ref"}:
        d = (params.get(v["Ref"]) or {}).get("Default")
        return None if d is None else str(d)
    if isinstance(v, dict) and set(v) == {"Fn::Sub"} and isinstance(v["Fn::Sub"], str):
        ok = True

        def sub(m: re.Match) -> str:
            nonlocal ok
            if keep_pseudo and is_pseudo(m.group(1)):
                return m.group(0)
            d = (params.get(m.group(1)) or {}).get("Default")
            ok = ok and d is not None
            return str(d)
        s = re.sub(r"\$\{([^}!]+)\}", sub, v["Fn::Sub"])
        return s if ok else None
    return None


_SEP = re.compile(r"[-_]{2,}")


def safe_name(s: str, limit: int = 100) -> str:
    """Fit Bedrock's ``^([0-9a-zA-Z][_-]?){1,100}$`` (names of agents, groups, functions)."""
    s = re.sub(r"[^0-9a-zA-Z_-]+", "-", s)
    s = _SEP.sub(lambda m: m.group(0)[0], s).strip("-_")
    return (s[:limit].rstrip("-_")) or "agent"


def logical_id(s: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", s)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return out or "Agent"


def lit(v: Any) -> Any:
    """Multi-line strings as block literals when dumping."""
    if isinstance(v, str) and "\n" in v:
        return LiteralScalarString(v)
    if isinstance(v, dict):
        return {k: lit(x) for k, x in v.items()}
    if isinstance(v, list):
        return [lit(x) for x in v]
    return v


def dumper() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y
