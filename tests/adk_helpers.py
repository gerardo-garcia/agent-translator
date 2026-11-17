import json
from pathlib import Path

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from agent_translator.adapters.adk import AdkAdapter
from agent_translator.adapters.adk import common as c

FIX = Path(__file__).parent.parent / "fixtures" / "adk"
ad = AdkAdapter()
_SCHEMA = json.loads((FIX / "schemas" / "AgentConfig.json").read_text())
_V: dict[str, Draft202012Validator] = {}
_DEF = {"LlmAgent": "LlmAgentConfig", "SequentialAgent": "SequentialAgentConfig",
        "ParallelAgent": "ParallelAgentConfig", "LoopAgent": "LoopAgentConfig"}


def roots() -> list[Path]:
    """Every agent folder of the fixtures (official samples and synthetic ones)."""
    return sorted((FIX / "samples").rglob("root_agent.yaml")) + sorted(
        (FIX / "synthetic").rglob("root_agent.yaml"))


def agent_files(root: Path) -> list[Path]:
    return sorted(root.parent.glob("**/*.yaml"))


def validate_agent_yaml(path: Path) -> bool:
    """Validate against the official schema; False if the file is not a schema-covered class."""
    d = YAML(typ="safe", pure=True).load(path.read_text())
    cls = c.short_class(d.get("agent_class") or "LlmAgent")
    if "edges" in d:
        return False                         # graph workflows are outside the published schema
    name = _DEF.get(cls, "BaseAgentConfig")  # custom classes: only the base fields are checked
    if name not in _V:
        _V[name] = Draft202012Validator({"$defs": _SCHEMA["$defs"], "$ref": f"#/$defs/{name}"})
    errs = list(_V[name].iter_errors(d))
    assert not errs, f"{path.name}: {errs[0].message} at {list(errs[0].path)}"
    return True


def codes(rep) -> set[str]:
    return {i.code for i in rep.items}


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
