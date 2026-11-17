import inspect
import re
from pathlib import Path

import pytest

from agent_translator.adapters.azure import AzureAdapter

FIX = Path(__file__).parent.parent / "fixtures" / "azure"
ad = AzureAdapter()
FOUNDRY = sorted((FIX / "foundry").glob("*.yaml")) + [FIX / "synthetic" / "multi-agent-azure.yaml"]
FRAMEWORK = sorted((FIX / "agent-framework").glob("*.yaml")) + [
    FIX / "synthetic" / "agent-framework-tools.yaml"]
# service keys that are not part of the agent definition sent to the service
SERVICE_ONLY = {"host", "project", "uses", "name", "kind", "description", "displayName", "toolChoice",
                "toolboxes", "metadata", "protocols", "env", "container"}
DEFINITION = {"model", "instructions", "temperature", "top_p", "reasoning", "tools", "text",
              "structured_inputs", "harness", "skills", "rai_config"}


def codes(rep) -> set[str]:
    return {i.code for i in rep.items}


def _snake(k: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()


def validate_foundry(path: Path) -> int:
    """Check every prompt agent of an azure.yaml against the azure-ai-projects SDK classes."""
    m = pytest.importorskip("azure.ai.projects.models")
    from ruamel.yaml import YAML
    d = YAML(typ="safe", pure=True).load(path.read_text())
    n = 0
    for key, s in d["services"].items():
        if s.get("host") != "azure.ai.agent" or s.get("kind") != "prompt":
            continue
        unknown = set(s) - SERVICE_ONLY - DEFINITION
        assert not unknown, f"{key}: unknown fields {unknown}"
        tools = []
        for t in s.get("tools") or []:
            cls = m.Tool.__mapping__.get(t["type"])
            assert cls, f"{key}: unknown tool type {t['type']!r}"
            # azd accepts camelCase keys (e.g. defaultConfig) where the SDK uses snake_case
            tools.append(cls(**{_snake(k): v for k, v in t.items() if k != "type"}))  # TypeError = bad
        m.PromptAgentDefinition(**{**{k: v for k, v in s.items() if k in DEFINITION},
                                   "tools": tools or None})
        assert inspect.isclass(m.PromptAgentDefinition)
        n += 1
    return n


def validate_framework(path: Path) -> None:
    pytest.importorskip("agent_framework_declarative")
    from agent_framework_declarative._models import agent_schema_dispatch
    from ruamel.yaml import YAML
    agent_schema_dispatch(YAML(typ="safe", pure=True).load(path.read_text()))


def core(ir):
    out = []
    for a in sorted(ir.agents, key=lambda a: a.id):
        d = a.model_dump(mode="json", exclude={"extensions"})
        d["tools"] = [{k: v for k, v in t.items() if k not in ("id", "extensions")}
                      for t in d["tools"]]
        d["remote_agents"] = [{k: v for k, v in r.items() if k != "extensions"}
                              for r in d["remote_agents"]]
        out.append(d)
    return out
