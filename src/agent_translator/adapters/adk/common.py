"""Shared pieces of the ADK adapter."""
from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML

WORKFLOW_CLASSES = {"SequentialAgent": "sequential", "ParallelAgent": "parallel",
                    "LoopAgent": "loop"}
MODE_CLASS = {v: k for k, v in WORKFLOW_CLASSES.items()}
LLM_CLASS = "LlmAgent"
KNOWN_CLASSES = {LLM_CLASS, *WORKFLOW_CLASSES}
MCP_TOOLSETS = {"McpToolset", "MCPToolset"}
BUILTIN_TOOLS = {"google_search", "url_context", "exit_loop", "load_memory", "preload_memory",
                 "load_artifacts", "get_user_choice"}
KNOWLEDGE_TOOLS = {"VertexAiSearchTool", "VertexAiRagRetrieval"}
# generate_content_config keys with a portable meaning -> IR param
PARAMS = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k",
          "max_output_tokens": "max_tokens"}
MAPPED_KEYS = {"name", "description", "agent_class", "model", "model_code", "instruction",
               "tools", "sub_agents", "generate_content_config", "max_iterations"}
PLACEHOLDER = re.compile(r"\{[A-Za-z_][\w:.?]*\}")


def yaml_safe() -> YAML:
    return YAML(typ="safe", pure=True)


def short_class(cls: str) -> str:
    return cls.rsplit(".", 1)[-1] if cls.startswith("google.adk.") else cls


def vendor_of(model: str) -> tuple[str | None, str]:
    """(provider, id) for an ADK model string (Gemini id or LiteLLM-style ``vendor/id``)."""
    if "/" in model:
        v, rest = model.split("/", 1)
        return {"vertex_ai": "google", "gemini": "google", "bedrock": "amazon",
                "azure": "openai"}.get(v, v), rest
    low = model.lower()
    for prefix, vendor in (("gemini", "google"), ("claude", "anthropic"), ("gpt", "openai"),
                           ("o1", "openai"), ("o3", "openai"), ("llama", "meta")):
        if low.startswith(prefix):
            return vendor, model
    return None, model


def py_name(s: str) -> str:
    n = re.sub(r"\W+", "_", s).strip("_")
    if not n or n[0].isdigit():
        n = f"f_{n}"
    return n


def is_py_path(s: Any) -> bool:
    return isinstance(s, str) and bool(re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_]\w*)+", s))


def adk(x: Any) -> dict[str, Any]:
    return x.extensions.get("adk", {})
