"""Shared pieces of the CrewAI adapter: JSONC, instruction composition, model strings, refs."""
from __future__ import annotations

import json
import re
from typing import Any

from ruamel.yaml import YAML

from ...models import lookup
from ..bedrock.emitter import BEDROCK_MODEL

PERSONA_KEYS = ("role", "goal", "backstory")
# agent keys the IR carries itself; everything else is kept in extensions.crewai.settings
AGENT_MAPPED = {"role", "goal", "backstory", "llm", "tools", "mcps"}
# LiteLLM-style prefix <-> IR provider
PREFIX_PROVIDER = {"openai": "openai", "anthropic": "anthropic", "gemini": "google",
                   "vertex_ai": "google", "bedrock": "amazon", "azure": "openai"}
LLM_PARAMS = {"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
              "top_k": "top_k"}
DEFAULT_GOAL = "Carry out the role described in the backstory."


def yaml_safe() -> YAML:
    return YAML(typ="safe", pure=True)


def loads_jsonc(text: str) -> Any:
    """JSON with ``//`` and ``/* */`` comments and trailing commas (string-aware)."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        else:
            out.append(c)
        i += 1
    clean, j, in_str = [], 0, False
    s = "".join(out)
    while j < len(s):
        c = s[j]
        if in_str:
            clean.append(c)
            if c == "\\" and j + 1 < len(s):
                clean.append(s[j + 1])
                j += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            clean.append(c)
        elif c == ",":
            k = j + 1
            while k < len(s) and s[k] in " \t\r\n":
                k += 1
            if k < len(s) and s[k] in "}]":
                j += 1
                continue
            clean.append(c)
        else:
            clean.append(c)
        j += 1
    return json.loads("".join(clean))


def load_any(path) -> Any:
    text = path.read_text()
    if path.suffix in (".json", ".jsonc"):
        return loads_jsonc(text)
    return yaml_safe().load(text)


def clean(s: Any) -> str:
    return str(s).strip() if s is not None else ""


def compose(persona: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """The IR system prompt of a CrewAI agent: persona, then the tasks it was given."""
    role, goal, backstory = (clean(persona.get(k)) for k in PERSONA_KEYS)
    parts = []
    if role:
        parts.append(f"You are {role.rstrip('.')}.")
    if backstory:
        parts.append(backstory)
    if goal:
        parts.append(f"Your goal: {goal}")
    for t in tasks:
        parts.append(f"Task: {clean(t.get('description'))}\n"
                     f"Expected output: {clean(t.get('expected_output'))}")
    return "\n\n".join(parts)


def split_model(llm: Any) -> tuple[str | None, str | None, dict[str, Any], dict[str, Any]]:
    """(provider, id, params, extra) from a CrewAI ``llm`` value (string or object)."""
    if llm is None:
        return None, None, {}, {}
    extra: dict[str, Any] = {}
    params: dict[str, Any] = {}
    if isinstance(llm, dict):
        raw = llm.get("model")
        for k, v in llm.items():
            if k == "model":
                continue
            if k in LLM_PARAMS:
                params[LLM_PARAMS[k]] = v
            else:
                extra[k] = v
        if not isinstance(raw, str):
            return llm.get("provider"), None, params, extra
    else:
        raw = str(llm)
    prefix, sep, rest = raw.partition("/")
    if sep and prefix in PREFIX_PROVIDER:
        provider, mid = PREFIX_PROVIDER[prefix], rest
        if prefix == "bedrock":
            match = lookup(rest)
            provider = match.vendor if match else "amazon"
        return provider, mid, params, extra | ({"prefix": prefix} if prefix in ("azure",
                                                                              "vertex_ai") else {})
    if sep:
        return prefix, rest, params, extra
    match = lookup(raw)
    return (match.vendor if match else None), raw, params, extra


def model_string(provider: str | None, mid: str) -> str | None:
    """LiteLLM-style ``prefix/id`` for a model id; None when no prefix is known."""
    if BEDROCK_MODEL.match(mid):
        return f"bedrock/{mid}"
    match = lookup(mid)
    vendor = (match.vendor if match else provider) or ""
    prefix = {"anthropic": "anthropic", "openai": "openai", "google": "gemini"}.get(vendor)
    return f"{prefix}/{mid}" if prefix else None


def slug(s: str) -> str:
    n = re.sub(r"\W+", "_", s).strip("_").lower()
    return n if n and not n[0].isdigit() else f"a_{n}"
