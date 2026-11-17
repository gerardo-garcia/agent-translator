from pathlib import Path

import pytest
from ruamel.yaml import YAML

from agent_translator.adapters.agentspec import AgentSpecAdapter

FIX = Path(__file__).parent.parent / "fixtures" / "agentspec"
ad = AgentSpecAdapter()
AGENTS = ["autogen_to_agentspec", "ext_code_assistant_tutorial", "ext_ops_assistant_tutorial_agent",
          "howto_ag_ui", "howto_agent_with_remote_tools", "howto_agents", "howto_mcp_agent",
          "math_homework_agent", "simple_agent_with_rag_tool", "howto_managerworkers",
          "howto_swarm"]
UNSUPPORTED = ["howto_a2aagent", "howto_flowbuilder"]


def codes(rep) -> set[str]:
    return {i.code for i in rep.items}


def validate(path: Path, drop_a2a_workers: bool = False) -> object:
    """Load with Oracle's pyagentspec (skipped if it is not installed).

    `drop_a2a_workers`: pyagentspec 26.3.1 cannot load the A2AAgent format that the official
    example `howto_a2aagent.yaml` uses, so documents with A2A workers are checked without them.
    """
    pytest.importorskip("pyagentspec")
    from pyagentspec.serialization import AgentSpecDeserializer
    text = path.read_text()
    text = text.replace("agentspec_version: 26.4.0", "agentspec_version: 26.1.0")  # see SOURCES.md
    if drop_a2a_workers:
        d = YAML(typ="safe", pure=True).load(text)
        d["workers"] = [w for w in d["workers"] if w["component_type"] != "A2AAgent"]
        if not d["workers"]:                      # only the manager is left to check
            d = {**d["group_manager"], "agentspec_version": d["agentspec_version"],
                 **({"$referenced_components": d["$referenced_components"]}
                    if "$referenced_components" in d else {})}
        import io
        buf = io.StringIO()
        YAML().dump(d, buf)
        text = buf.getvalue()
    return AgentSpecDeserializer().from_yaml(text)


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
