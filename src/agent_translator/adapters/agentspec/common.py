"""Helpers of the Agent Spec adapter (https://github.com/oracle/agent-spec)."""
from __future__ import annotations

import re
import uuid
from typing import Any

from ruamel.yaml import YAML

NS = uuid.UUID("5d0d3b0e-6f0e-4a55-9d6e-1f6d7a1b2c3d")
AGENTIC = {"Agent", "ManagerWorkers", "Swarm", "A2AAgent", "OciAgent"}
PARAMS = {"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_tokens"}


def yaml_safe() -> YAML:
    return YAML(typ="safe", pure=True)


def uid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-") or "x"


def unique(base: str, used: set[str]) -> str:
    k, cand = 2, base
    while cand in used:
        cand, k = f"{base}-{k}", k + 1
    used.add(cand)
    return cand


def vendor_of(model: str | None) -> str | None:
    if not model:
        return None
    low = model.lower()
    for prefix, vendor in (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
                           ("claude", "anthropic"), ("gemini", "google")):
        if low.startswith(prefix):
            return vendor
    return None


def ext(x: Any) -> dict[str, Any]:
    return x.extensions.get("agentspec", {})


def props_to_schema(props: Any) -> dict[str, Any]:
    """Agent Spec property list -> JSON Schema object (a property is required unless it has a default)."""
    out: dict[str, Any] = {}
    req: list[str] = []
    for p in props or []:
        p = dict(p)
        title = p.pop("title", None)
        if title is None:
            continue
        any_of = p.get("anyOf")
        optional = "default" in p
        if (isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of
                and p.get("default", 1) is None):
            inner = next(x for x in any_of if x != {"type": "null"})
            p = {**{k: v for k, v in p.items() if k not in ("anyOf", "default")}, **inner}
        if not optional:
            req.append(title)
        out[title] = p
    schema: dict[str, Any] = {"type": "object", "properties": out}
    if req:
        schema["required"] = req
    return schema


def schema_to_props(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    out = []
    for name, s in props.items():
        d = {"title": name, **{k: v for k, v in (s or {}).items() if k != "title"}}
        if name not in req and "default" not in d:
            # an optional parameter is nullable (a typed default of None is not valid otherwise)
            d = {"title": name, "anyOf": [{k: v for k, v in d.items() if k != "title"},
                                          {"type": "null"}], "default": None}
        out.append(d)
    return out


def max_version(*vs: str) -> str:
    return max(vs, key=lambda v: tuple(int(x) for x in v.split(".")))
