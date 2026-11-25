"""CrewAI definitions -> IR.

Three inputs share one builder: a JSON crew project (``crew.jsonc`` + ``agents/``), a standalone
agent (``agent.yaml``/``agent.json``) and the classic ``config/agents.yaml`` (+ ``tasks.yaml``).
A crew becomes a root agent (sequential workflow, or supervisor for a hierarchical process) over
one IR agent per CrewAI agent; the tasks are folded into each agent's instructions and kept
verbatim in ``extensions.crewai`` so a CrewAI -> CrewAI round trip is lossless.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...ir import AgentIR, AgentSpec, Collaboration, Memory, ModelSpec, Resources, Tool, ToolKind
from ...report import Report, Severity
from . import common as c


def _is_agent_body(d: Any) -> bool:
    return isinstance(d, dict) and all(isinstance(d.get(k), str) for k in c.PERSONA_KEYS)


def is_classic_agents(d: Any) -> bool:
    return isinstance(d, dict) and bool(d) and all(_is_agent_body(v) for v in d.values())


def is_crew(d: Any) -> bool:
    return (isinstance(d, dict) and isinstance(d.get("tasks"), list)
            and isinstance(d.get("agents"), (list, dict)) and "schema" not in d
            and "methods" not in d)


def kind_of(d: Any) -> str | None:
    if is_crew(d):
        return "crew"
    if _is_agent_body(d):
        return "agent"
    if is_classic_agents(d):
        return "classic"
    return None


class Parser:
    def __init__(self, rep: Report):
        self.rep = rep
        self.servers: dict[str, dict[str, Any]] = {}

    # ----------------------------------------------------------- entry points
    def parse_file(self, path: Path) -> list[AgentSpec]:
        d = c.load_any(path)
        kind = kind_of(d)
        if kind == "crew":
            return self.crew(d, path)
        if kind == "agent":
            key = c.slug(path.parent.name) if path.stem == "agent" else path.stem
            return self.crew({"name": key, "agents": {key: d}, "tasks": []}, path,
                             standalone=True)
        if kind == "classic":
            return self.classic(d, path)
        self.rep.add(Severity.error, "unrecognised", str(path), "not a CrewAI definition")
        return []

    def classic(self, agents: dict[str, Any], path: Path) -> list[AgentSpec]:
        tasks: list[dict[str, Any]] = []
        tf = next((p for p in (path.with_name("tasks.yaml"), path.with_name("tasks.yml"))
                   if p.exists()), None)
        if tf:
            for name, body in (c.load_any(tf) or {}).items():
                if isinstance(body, dict):
                    tasks.append({"name": name, **body})
        self.rep.add(Severity.info, "python-config-not-read", str(path),
                     "classic projects keep tools, models, the process and agent/task wiring in "
                     "crew.py, which is not read: assuming a sequential process")
        return self.crew({"name": path.parent.parent.name or "crew", "agents": agents,
                          "tasks": tasks, "process": "sequential"}, path)

    def crew(self, d: dict[str, Any], path: Path, standalone: bool = False) -> list[AgentSpec]:
        agents = self._agent_bodies(d, path)
        name = c.slug(str(d.get("name") or path.parent.name or "crew"))
        tasks = [t for t in d.get("tasks") or [] if isinstance(t, dict)]
        process = str(d.get("process") or "sequential")
        by_agent: dict[str, list[dict[str, Any]]] = {}
        unassigned: list[dict[str, Any]] = []
        for t in tasks:
            ref = t.get("agent")
            if isinstance(ref, str) and ref in agents:
                by_agent.setdefault(ref, []).append(t)
            else:
                if ref:
                    self.rep.add(Severity.warning, "task-agent-unknown",
                                 f"{path.name}:{t.get('name')}", f"task refers to agent {ref!r}")
                unassigned.append(t)
        crew_memory = bool(d.get("memory"))
        specs = {n: self.agent(n, b, by_agent.get(n, []), crew_memory, path)
                 for n, b in agents.items()}
        if standalone or (len(specs) == 1 and process != "hierarchical"):
            return list(specs.values())
        crew_ext = {k: v for k, v in d.items() if k not in ("agents", "tasks", "name")}
        crew_ext["tasks"] = tasks
        crew_ext["name"] = d.get("name")
        crew_ext["agentOrder"] = list(agents)
        if process == "hierarchical":
            return self._hierarchical(name, d, specs, unassigned, crew_ext, path)
        order = list(dict.fromkeys(
            t["agent"] for t in tasks if isinstance(t.get("agent"), str) and t["agent"] in specs))
        for n in specs:
            if n not in order:
                self.rep.add(Severity.warning, "agent-without-task", f"{path.name}:{n}",
                             "the agent has no task, so it never runs in a sequential crew; "
                             "kept after the others")
                order.append(n)
        if any(t.get("async_execution") for t in tasks):
            self.rep.add(Severity.lossy, "async-execution-dropped", path.name,
                         "tasks with async_execution run in parallel in CrewAI; the IR workflow "
                         "is sequential")
        root = AgentSpec(
            id=self._unique(name, specs), name=str(d.get("name") or name),
            description=None,
            model=ModelSpec(), sub_agents=order,
            collaboration=Collaboration(mode="sequential"),
            memory=Memory(enabled=crew_memory),
            extensions={"crewai": {"crew": crew_ext}})
        return [root, *specs.values()]

    def _hierarchical(self, name, d, specs, unassigned, crew_ext, path) -> list[AgentSpec]:
        mgr = d.get("manager_agent")
        text = ""
        if unassigned:
            text = "\n\n".join(f"Task: {c.clean(t.get('description'))}\n"
                               f"Expected output: {c.clean(t.get('expected_output'))}"
                               for t in unassigned)
        if isinstance(mgr, str) and mgr in specs:
            root = specs.pop(mgr)
            crew_ext["managerKind"] = "agent"
            crew_ext["managerAgent"] = mgr
            if text:
                root.instructions = f"{root.instructions}\n\n{text}".strip()
        else:
            if mgr is not None and not isinstance(mgr, str):
                self.rep.add(Severity.manual, "manager-python-ref", path.name,
                             "manager_agent is a Python reference: replaced by a manager model")
            prov, mid, params, _ = c.split_model(d.get("manager_llm"))
            if not mid:
                self.rep.add(Severity.warning, "manager-model-missing", path.name,
                             "hierarchical process without manager_llm or manager_agent")
            root = AgentSpec(id=self._unique(name, specs), name=str(d.get("name") or name),
                             instructions=text or None,
                             model=ModelSpec(provider=prov, id=mid, params=params))
            crew_ext["managerKind"] = "llm"
        root.sub_agents = list(specs)
        root.collaboration = Collaboration(mode="supervisor")
        root.memory = Memory(enabled=bool(d.get("memory")))
        root.extensions = {**root.extensions,
                           "crewai": {**root.extensions.get("crewai", {}), "crew": crew_ext}}
        return [root, *specs.values()]

    @staticmethod
    def _unique(name: str, specs: dict[str, Any]) -> str:
        return name if name not in specs else f"{name}_crew"

    def _agent_bodies(self, d: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
        raw = d.get("agents")
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        for item in raw or []:
            if isinstance(item, dict):
                n = item.get("name")
                if isinstance(n, str):
                    out[n] = {k: v for k, v in item.items() if k != "name"}
                elif len(item) == 1:
                    k, v = next(iter(item.items()))
                    out[str(k)] = dict(v)
                continue
            if not isinstance(item, str):
                continue
            body = self._read_agent(path.parent / "agents", item)
            if body is None:
                self.rep.add(Severity.error, "agent-file-missing", f"{path.name}:{item}",
                             f"agents/{item}.jsonc (or .json) not found")
            else:
                out[item] = body
        mgr = d.get("manager_agent")
        if isinstance(mgr, str) and mgr not in out:
            body = self._read_agent(path.parent / "agents", mgr)
            if body is not None:
                out[mgr] = body
        return out

    @staticmethod
    def _read_agent(folder: Path, name: str) -> dict[str, Any] | None:
        for ext in (".jsonc", ".json", ".yaml", ".yml"):
            f = folder / f"{name}{ext}"
            if f.exists():
                body = c.load_any(f)
                return body if isinstance(body, dict) else None
        return None

    # ------------------------------------------------------------- one agent
    def agent(self, name: str, body: dict[str, Any], tasks: list[dict[str, Any]],
              crew_memory: bool, path: Path) -> AgentSpec:
        loc = f"{path.name}:{name}"
        persona = {k: body.get(k) for k in c.PERSONA_KEYS}
        settings = {k: v for k, v in body.items() if k not in c.AGENT_MAPPED}
        flat = {**settings, **(settings.get("settings") or {})}
        prov, mid, params, extra = c.split_model(body.get("llm"))
        if not mid and body.get("llm") is not None:
            self.rep.add(Severity.warning, "llm-unreadable", loc, f"cannot read llm {body['llm']!r}")
        if not body.get("llm"):
            self.rep.add(Severity.info, "model-missing", loc,
                         "no llm: CrewAI falls back to its default model")
        tools: list[Tool] = []
        for ref in body.get("tools") or []:
            tools.append(self.tool(ref, loc))
        for ref in body.get("mcps") or []:
            t = self.mcp(ref, loc)
            if t:
                tools.append(t)
        for key in ("apps", "knowledge_sources", "skills"):
            if body.get(key) or flat.get(key):
                self.rep.add(Severity.lossy, f"{key}-dropped", loc,
                             f"`{key}` has no IR equivalent; kept in the CrewAI extension only")
        if flat.get("allow_delegation"):
            self.rep.add(Severity.lossy, "delegation-not-translated", loc,
                         "allow_delegation lets the agent hand work to its crewmates at run "
                         "time; the IR models the crew structure instead")
        guard = settings.get("guardrail")
        guardrails = [{"instruction": guard}] if isinstance(guard, str) else []
        if guard is not None and not guardrails:
            self.rep.add(Severity.manual, "guardrail-python-ref", loc,
                         "a Python guardrail cannot be translated")
        ext = {"persona": persona, "tasks": tasks, "settings": settings, "llmExtra": extra}
        return AgentSpec(
            id=name, name=c.clean(persona["role"]) or name,
            description=c.clean(persona["goal"]) or None,
            instructions=c.compose(persona, tasks) or None,
            model=ModelSpec(provider=prov, id=mid, params=params),
            tools=tools, guardrails=guardrails,
            memory=Memory(enabled=bool(crew_memory or flat.get("memory"))),
            extensions={"crewai": ext})

    # ------------------------------------------------------------------ tools
    def tool(self, ref: Any, loc: str) -> Tool:
        if isinstance(ref, dict):
            nm = str(ref.get("name") or ref.get("tool_type") or "tool")
            return Tool(id=c.slug(nm), kind=ToolKind.builtin, name=nm,
                        extensions={"crewai": {"def": ref}})
        s = str(ref)
        if s.startswith("custom:"):
            fn = s[7:]
            return Tool(id=fn, kind=ToolKind.function, name=fn, impl_ref=s,
                        description=None)
        name = s.rsplit(":", 1)[-1]
        if s.startswith("platform:"):
            self.rep.add(Severity.manual, "platform-tool", loc,
                         f"{s} is a CrewAI platform app and has no equivalent elsewhere")
        return Tool(id=c.slug(name), kind=ToolKind.builtin, name=name,
                    extensions={"crewai": {"ref": s}})

    def mcp(self, ref: Any, loc: str) -> Tool | None:
        if isinstance(ref, dict):
            url, tool_names, headers = ref.get("url"), [], ref.get("headers")
            sse = ref.get("streamable") is False
        else:
            base, _, picked = str(ref).partition("#")
            url, tool_names, headers, sse = base, [picked] if picked else [], None, False
        if not url:
            self.rep.add(Severity.warning, "mcp-unreadable", loc, f"cannot read MCP ref {ref!r}")
            return None
        is_url = bool(re.match(r"https?://", str(url)))
        sid = c.slug(urlparse(url).netloc + urlparse(url).path) if is_url else c.slug(str(url))
        server = self.servers.setdefault(sid, {"id": sid})
        if is_url:
            server.update(url=url, protocol="sse" if sse else "streamable-http")
        else:
            self.rep.add(Severity.manual, "mcp-integration-slug", loc,
                         f"MCP integration {url!r} is a CrewAI platform connection with no URL")
        if headers:
            server["headers"] = headers
            self.rep.add(Severity.warning, "mcp-headers", loc,
                         "MCP headers (possibly credentials) were read from the source")
        return Tool(id=f"mcp_{sid}", kind=ToolKind.mcp, server_ref=sid, tool_names=tool_names)


def parse(path: Path, rep: Report) -> AgentIR:
    p = Parser(rep)
    agents = p.parse_file(path)
    return AgentIR(agents=agents, resources=Resources(mcp_servers=list(p.servers.values())))
