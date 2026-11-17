"""Shared helpers of the Azure adapter (Foundry azure.yaml and Agent Framework YAML)."""
from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML

# tool `type` values of the Foundry Agent Service (azure-ai-projects ToolType)
TOOL_TYPES = {
    "function", "file_search", "computer", "computer_use_preview", "web_search", "mcp",
    "code_interpreter", "programmatic_tool_calling", "image_generation", "local_shell", "shell",
    "custom", "namespace", "tool_search", "web_search_preview", "apply_patch", "a2a_preview",
    "bing_custom_search_preview", "browser_automation_preview", "fabric_dataagent_preview",
    "sharepoint_grounding_preview", "memory_search_preview", "work_iq_preview",
    "fabric_iq_preview", "toolbox_search_preview", "web_iq_preview",
    "github_copilot_toolset_preview", "a2a", "azure_ai_search", "azure_function",
    "bing_grounding", "browser_automation", "capture_structured_outputs", "openapi",
    "microsoft_copilot_toolset_preview"}
# built-in tool of another platform -> Foundry tool type
BUILTIN_MAP = {"google_search": "web_search", "AMAZON.CodeInterpreter": "code_interpreter",
               "code_interpreter": "code_interpreter", "web_search": "web_search"}
GENERIC = {"function", "openapi", "mcp", "azure_ai_search", "file_search", "a2a", "a2a_preview"}
PARAMS = {"temperature": "temperature", "top_p": "top_p"}
CHANGING = ("sequential", "parallel", "loop", "handoff", "supervisor", "router")


def yaml_safe() -> YAML:
    return YAML(typ="safe", pure=True)


def vendor_of(model: str | None) -> str | None:
    if not model:
        return None
    low = model.lower()
    for prefix, vendor in (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
                           ("claude", "anthropic"), ("gemini", "google"), ("llama", "meta"),
                           ("mistral", "mistral"), ("deepseek", "deepseek"), ("phi", "microsoft")):
        if low.startswith(prefix):
            return vendor
    return None


def safe_name(s: str) -> str:
    """Foundry agent names: letters, digits and hyphens, at most 63 characters."""
    n = re.sub(r"[^A-Za-z0-9-]+", "-", s).strip("-")[:63].strip("-")
    return n or "agent"


def unique(base: str, used: set[str]) -> str:
    k, cand = 2, base
    while cand in used:
        cand, k = f"{base}-{k}", k + 1
    used.add(cand)
    return cand


def server_id(label: str | None, url: str | None, fallback: str) -> str:
    raw = label or re.sub(r"^https?://", "", url or "") or fallback
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")[:48] or fallback


def az(x: Any) -> dict[str, Any]:
    return x.extensions.get("azure", {})


# --- Agent Framework PropertySchema <-> JSON Schema -------------------------------------
def schema_to_json(ps: Any) -> dict[str, Any] | None:
    """Agent Framework property schema (`properties: {name: {type|kind, required, ...}}`)."""
    if not isinstance(ps, dict):
        return None
    props = ps.get("properties")
    if isinstance(props, list):
        props = {p.get("name"): {k: v for k, v in p.items() if k != "name"} for p in props}
    if not isinstance(props, dict):
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {}
    req: list[str] = []
    for n, p in props.items():
        p = dict(p or {})
        j = {"type": p.get("type") or p.get("kind") or "string"}
        for k in ("description", "enum", "default"):
            if k in p:
                j[k] = p[k]
        if isinstance(p.get("properties"), (dict, list)):
            j.update(schema_to_json(p) or {})
            j["type"] = "object"
        if isinstance(p.get("items"), dict):
            j["items"] = {"type": p["items"].get("type") or p["items"].get("kind") or "string"}
        if p.get("required"):
            req.append(n)
        out[n] = j
    res: dict[str, Any] = {"type": "object", "properties": out}
    if req:
        res["required"] = req
    return res


def json_to_schema(js: dict[str, Any] | None) -> dict[str, Any]:
    props = (js or {}).get("properties") or {}
    req = set((js or {}).get("required") or [])
    out: dict[str, Any] = {}
    for n, p in props.items():
        d: dict[str, Any] = {"type": p.get("type", "string")}
        for k in ("description", "enum", "default"):
            if k in p:
                d[k] = p[k]
        if p.get("type") == "object" and "properties" in p:
            d.update(json_to_schema(p))
        if p.get("type") == "array" and isinstance(p.get("items"), dict):
            d["items"] = {"type": p["items"].get("type", "string")}
        if n in req:
            d["required"] = True
        out[n] = d
    return {"properties": out}
