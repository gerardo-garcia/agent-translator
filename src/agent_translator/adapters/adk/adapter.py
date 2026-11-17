"""Google ADK adapter (Agent Config YAML: root_agent.yaml and the files it references)."""
from __future__ import annotations

from pathlib import Path

from ...ir import AgentIR, Resources
from ...models import adapt_ir
from ...report import Report, Severity
from . import common as c
from . import emitter
from .parser import Parser


class AdkAdapter:
    name = "adk"

    def detect(self, path: Path) -> bool:
        if path.suffix not in (".yaml", ".yml"):
            return False
        try:
            d = c.yaml_safe().load(path.read_text())
        except Exception:
            return False
        if not isinstance(d, dict) or not isinstance(d.get("name"), str):
            return False
        if {"apiVersion", "Resources", "component_type", "AWSTemplateFormatVersion", "kind",
                "services"} & set(d):
            return False
        return bool({"agent_class", "instruction", "model", "sub_agents", "tools", "edges"} & set(d))

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        p = Parser(rep)
        p.parse_file(path)
        if not p.agents:
            rep.add(Severity.error, "no-agents", str(path), "no ADK agent found")
        return AgentIR(agents=list(p.agents.values()),
                       resources=Resources(mcp_servers=list(p.servers.values()))), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "adk", options, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        for f in emitter.emit(ir, out, rep):
            rep.add(Severity.info, "written", str(f), "ADK")
        return rep
