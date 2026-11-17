"""Azure adapter: Foundry Agent Service and Microsoft Agent Framework declarative agents.

Two file formats, chosen on emit with `-O format=foundry` (default) or `-O format=agent-framework`:

* `foundry`: `azure.yaml` with `host: azure.ai.agent` / `kind: prompt` services (what
  `azd ai agent` deploys to the Foundry Agent Service); also a bare `kind: prompt` definition
  (YAML/JSON) as sent to the service.
* `agent-framework`: the `kind: Prompt` YAML loaded by Agent Framework's `AgentFactory`
  (one file per agent).
"""
from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

from ...ir import AgentIR, Resources
from ...models import adapt_ir
from ...report import Report, Severity
from ...yamlio import new_yaml
from . import common as c
from . import foundry, framework

FORMATS = ("foundry", "agent-framework")


def _resolve(path: Path) -> Path:
    """A directory (as written by `fetch azure`) stands for its single agent file."""
    if not path.is_dir():
        return path
    files = sorted(f for f in path.iterdir() if f.suffix in (".json", ".yaml", ".yml"))
    return files[0] if len(files) == 1 else path


def _load(path: Path):
    text = _resolve(path).read_text()
    return json.loads(text) if path.suffix == ".json" else c.yaml_safe().load(text)


def _dumper() -> YAML:
    y = new_yaml()
    y.default_flow_style = False
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


class AzureAdapter:
    name = "azure"
    versions = FORMATS

    def detect(self, path: Path) -> bool:
        path = _resolve(path)
        if not path.is_file() or path.suffix not in (".yaml", ".yml", ".json"):
            return False
        try:
            d = _load(path)
        except Exception:
            return False
        return isinstance(d, dict) and (foundry.is_service(d) or foundry.is_bare(d)
                                        or framework.is_agent(d))

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        path = _resolve(path)
        if not path.is_file():
            rep.add(Severity.error, "bad-input", str(path), "expected one agent file or a "
                    "directory containing exactly one .json/.yaml file")
            return AgentIR(agents=[]), rep
        d = _load(path)
        if isinstance(d, dict) and framework.is_agent(d):
            agent, servers = framework.parse(d, rep)
            return AgentIR(agents=[agent], resources=Resources(mcp_servers=servers)), rep
        p = foundry.Parser(rep, path.parent)
        p.parse(d if isinstance(d, dict) else {})
        if not p.agents:
            rep.add(Severity.error, "no-agents", str(path), "no prompt agent found")
        return AgentIR(agents=p.agents, resources=Resources(
            mcp_servers=list(p.servers.values()))), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "azure", options, rep)
        fmt = (options or {}).get("format", "foundry")
        if fmt not in FORMATS:
            rep.add(Severity.error, "bad-option", "format", f"{fmt!r}: choose one of {FORMATS}")
            return rep
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        out.mkdir(parents=True, exist_ok=True)
        y = _dumper()
        if fmt == "foundry":
            p = out / "azure.yaml"
            with p.open("w") as f:
                y.dump(foundry.emit(ir, rep), f)
            rep.add(Severity.info, "written", str(p), "Foundry azure.yaml")
        else:
            used: set[str] = set()
            for a in ir.agents:
                p = out / f"{c.unique(c.safe_name(a.id), used)}.yaml"
                with p.open("w") as f:
                    y.dump(framework.emit(a, ir, rep), f)
                rep.add(Severity.info, "written", str(p), "Agent Framework YAML")
        return rep
