"""Agent Spec adapter (https://github.com/oracle/agent-spec), YAML or JSON.

Reads `Agent`, `ManagerWorkers` and `Swarm` documents (with `$component_ref` /
`$referenced_components`); writes one file per root agent. Flows, OCI agents and standalone A2A
agents are reported, not translated.
"""
from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

from ...ir import AgentIR, Resources
from ...models import adapt_ir
from ...report import Report, Severity
from . import common as c
from .emitter import emit_doc
from .parser import Parser


def _load(path: Path):
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else c.yaml_safe().load(text)


class AgentSpecAdapter:
    name = "agentspec"

    def detect(self, path: Path) -> bool:
        if path.suffix not in (".yaml", ".yml", ".json"):
            return False
        try:
            d = _load(path)
        except Exception:
            return False
        return isinstance(d, dict) and "agentspec_version" in d and "component_type" in d

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        p = Parser(_load(path), rep)
        p.parse()
        if not p.agents:
            rep.add(Severity.error, "no-agents", str(path), "no Agent found")
        return AgentIR(agents=p.agents, resources=Resources(
            mcp_servers=list(p.servers.values()))), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "agentspec", options, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        out.mkdir(parents=True, exist_ok=True)
        children = {s for a in ir.agents for s in a.sub_agents}
        mode_flat = {a.id for a in ir.agents if a.collaboration.mode in (
            "sequential", "parallel", "loop")}
        roots = [a for a in ir.agents if a.id not in children]
        for a in ir.agents:                      # children of workflow agents get their own file
            if a.id in children and any(a.id in p.sub_agents for p in ir.agents if p.id in mode_flat):
                roots.append(a)
        y = YAML()
        y.default_flow_style = False
        y.indent(mapping=2, sequence=4, offset=2)
        y.width = 4096
        used: set[str] = set()
        for r in roots or ir.agents[:1]:
            p = out / f"{c.unique(c.slug(r.id), used)}.yaml"
            with p.open("w") as f:
                y.dump(emit_doc(ir, r, rep), f)
            rep.add(Severity.info, "written", str(p), "Agent Spec")
        return rep
