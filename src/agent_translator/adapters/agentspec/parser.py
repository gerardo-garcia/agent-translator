"""Agent Spec YAML/JSON -> IR."""
from __future__ import annotations

from typing import Any

from ...ir import AgentSpec, Collaboration, ModelSpec, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

LLM_PROVIDER = {"OpenAiConfig": "openai", "GeminiConfig": "google"}
TRANSPORT = {"SSETransport": "sse", "SSEmTLSTransport": "sse",
             "StreamableHTTPTransport": "streamable-http",
             "StreamableHTTPmTLSTransport": "streamable-http", "StdioTransport": "stdio"}


class Parser:
    def __init__(self, doc: dict, rep: Report):
        self.rep = rep
        self.doc = doc
        self.comps: dict[str, dict] = {}
        self._index(doc)
        self.agents: list[AgentSpec] = []
        self.servers: dict[str, dict] = {}
        self.used: set[str] = set()
        self.by_comp: dict[int, str] = {}

    def _index(self, x: Any) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("id"), str) and "component_type" in x:
                self.comps[x["id"]] = x
            for k, v in x.items():
                if k == "$referenced_components" and isinstance(v, dict):
                    for i, comp in v.items():
                        self.comps.setdefault(i, comp)
                self._index(v)
        elif isinstance(x, list):
            for v in x:
                self._index(v)

    def r(self, x: Any) -> Any:
        while isinstance(x, dict) and "$component_ref" in x:
            x = self.comps.get(x["$component_ref"], {})
        return x

    # ----------------------------------------------------------------- roots
    def parse(self) -> None:
        d = self.doc
        ct = d.get("component_type")
        if ct == "Agent":
            self.agent(d)
        elif ct == "ManagerWorkers":
            self.manager_workers(d)
        elif ct == "Swarm":
            self.swarm(d)
        else:
            self.rep.add(Severity.manual, "component-unsupported", ct or "?",
                         f"{ct!r} is not translated (flows, OCI agents and standalone A2A agents "
                         "have no IR equivalent)")

    def manager_workers(self, d: dict) -> None:
        mgr_c = self.r(d["group_manager"])
        workers = [self.r(w) for w in d.get("workers", [])]
        sub, remote, instr = [], [], {}
        for w in workers:
            if w.get("component_type") == "Agent":
                wid = self.agent(w)
                sub.append(wid)
                if w.get("description"):
                    instr[wid] = w["description"]
            elif w.get("component_type") == "A2AAgent":
                remote.append(self._a2a(w))
            else:
                self.rep.add(Severity.manual, "component-unsupported", w.get("name", "?"),
                             f"worker of type {w.get('component_type')!r} is not translated")
        if mgr_c.get("component_type") != "Agent":
            self.rep.add(Severity.manual, "component-unsupported", mgr_c.get("name", "?"),
                         "the manager is not an Agent")
            return
        mid = self.agent(mgr_c)
        m = next(a for a in self.agents if a.id == mid)
        m.sub_agents, m.remote_agents = sub, remote
        m.collaboration = Collaboration(mode="supervisor", instructions=instr)
        m.extensions.setdefault("agentspec", {})["root"] = "ManagerWorkers"

    def swarm(self, d: dict) -> None:
        first = self.r(d["first_agent"])
        rels = [(self.r(a), self.r(b)) for a, b in d.get("relationships", [])]
        comps: dict[str, dict] = {}
        for a in [first] + [x for pair in rels for x in pair]:
            comps.setdefault(a.get("id") or a.get("name"), a)
        ids = {k: self.agent(v) for k, v in comps.items()}
        by_id = {a.id: a for a in self.agents}
        mode = "handoff" if d.get("handoff", True) else "supervisor"
        for a, b in rels:
            src = by_id[ids[a.get("id") or a.get("name")]]
            dst = ids[b.get("id") or b.get("name")]
            if dst not in src.sub_agents:
                src.sub_agents.append(dst)
            src.collaboration = Collaboration(mode=mode)
        fid = ids[first.get("id") or first.get("name")]
        by_id[fid].extensions.setdefault("agentspec", {})["root"] = "Swarm"
        self.rep.add(Severity.info, "swarm-relationships", d.get("name", "Swarm"),
                     "relationships became sub_agents of the source agent; cycles are kept")

    def _a2a(self, w: dict) -> RemoteAgent:
        return RemoteAgent(id=c.slug(w.get("name", "a2a")), name=w.get("name", "a2a"),
                           description=w.get("description"), ref=w.get("agent_url"),
                           extensions={"agentspec": {"a2a": {k: v for k, v in w.items() if k in (
                               "connection_config", "session_parameters")}}})

    # ----------------------------------------------------------------- agent
    def agent(self, d: dict) -> str:
        key = id(d)
        if key in self.by_comp:
            return self.by_comp[key]
        name = d.get("name") or "agent"
        path = f"agents[{name}]"
        ax: dict[str, Any] = {}
        model = self.llm(self.r(d.get("llm_config")), ax, path)
        tools: list[Tool] = []
        for i, t in enumerate(d.get("tools") or []):
            self.tool(self.r(t), f"{path}.tools[{i}]", tools)
        for i, tb in enumerate(d.get("toolboxes") or []):
            self.toolbox(self.r(tb), f"{path}.toolboxes[{i}]", tools)
        for k in ("inputs", "outputs", "human_in_the_loop", "metadata", "transforms"):
            if d.get(k):
                ax[k] = d[k]
        prompt = d.get("system_prompt")
        if "{{" in (prompt or ""):
            self.rep.add(Severity.info, "prompt-placeholders", path,
                         "system_prompt uses {{input}} placeholders that other platforms do not fill")
        if not prompt:
            self.rep.add(Severity.warning, "instruction-missing", path, "empty system_prompt")
        aid = c.unique(name, self.used)
        self.by_comp[key] = aid
        self.agents.append(AgentSpec(id=aid, name=name, description=d.get("description"),
                                     instructions=prompt, model=model, tools=tools,
                                     collaboration=Collaboration(mode="none"),
                                     extensions={"agentspec": ax}))
        return aid

    def llm(self, cfg: dict, ax: dict, path: str) -> ModelSpec:
        if not cfg:
            self.rep.add(Severity.manual, "model-missing", path, "agent without llm_config")
            return ModelSpec()
        ct = cfg.get("component_type", "")
        mid = cfg.get("model_id")
        gen = cfg.get("default_generation_parameters") or {}
        params = {ir: gen[k] for k, ir in c.PARAMS.items() if k in gen}
        extra = {k: v for k, v in cfg.items() if k not in (
            "id", "name", "description", "metadata", "model_id", "default_generation_parameters")}
        ax["llm"] = {"component_type": ct, "name": cfg.get("name"), "model_id": mid, **extra}
        if set(gen) - set(c.PARAMS):
            ax["llm"]["generation_extra"] = {k: v for k, v in gen.items() if k not in c.PARAMS}
        self.rep.add(Severity.info, "model-unverified", path, f"model id {mid!r} copied as-is")
        return ModelSpec(provider=LLM_PROVIDER.get(ct) or c.vendor_of(mid), id=mid, params=params)

    def tool(self, t: dict, path: str, tools: list[Tool]) -> None:
        ct = t.get("component_type")
        name = t.get("name") or "tool"
        tid = c.unique(c.slug(name), {x.id for x in tools})
        conf = t.get("requires_confirmation")
        keep: dict[str, Any] = {"component_type": ct}
        if t.get("outputs"):
            keep["outputs"] = t["outputs"]
        if ct in ("ServerTool", "ClientTool"):
            tools.append(Tool(id=tid, kind=ToolKind.function, name=name,
                              description=t.get("description"),
                              parameters=c.props_to_schema(t.get("inputs")),
                              requires_confirmation=conf, extensions={"agentspec": keep}))
        elif ct == "RemoteTool":
            keep["remote"] = {k: t[k] for k in ("http_method", "data", "query_params", "headers",
                                                "api_spec_uri") if k in t}
            tools.append(Tool(id=tid, kind=ToolKind.function, name=name,
                              description=t.get("description"),
                              parameters=c.props_to_schema(t.get("inputs")),
                              impl_ref=t.get("url"), requires_confirmation=conf,
                              extensions={"agentspec": keep}))
            self.rep.add(Severity.info, "remote-tool-as-function", path,
                         "RemoteTool kept as a function whose impl_ref is its URL")
        else:
            self.rep.add(Severity.warning, "tool-unknown", path,
                         f"{ct!r} is not translated; tool dropped")

    def toolbox(self, tb: dict, path: str, tools: list[Tool]) -> None:
        if tb.get("component_type") != "MCPToolBox":
            self.rep.add(Severity.warning, "tool-unknown", path,
                         f"{tb.get('component_type')!r} is not translated; toolbox dropped")
            return
        tr = self.r(tb.get("client_transport")) or {}
        proto = TRANSPORT.get(tr.get("component_type"), "streamable-http")
        sid = c.unique(c.slug(tb.get("name") or "mcp"), set(self.servers))
        entry: dict[str, Any] = {"id": sid, "protocol": proto}
        if tr.get("url"):
            entry["url"] = tr["url"]
        if proto == "stdio":
            entry["description"] = " ".join([str(tr.get("command", ""))] + list(tr.get("args") or []))
            self.rep.add(Severity.manual, "mcp-stdio", path,
                         "stdio MCP servers run as local processes; a remote cloud agent needs HTTP/SSE")
        self.servers[sid] = entry
        names = [f if isinstance(f, str) else f.get("name") for f in (tb.get("tool_filter") or [])]
        keep = {"component_type": "MCPToolBox", "transport": {
            k: v for k, v in tr.items() if k not in ("id", "url", "component_type")}}
        tools.append(Tool(id=c.unique(f"mcp-{sid}", {x.id for x in tools}), kind=ToolKind.mcp,
                          name=tb.get("name"), description=tb.get("description"), server_ref=sid,
                          tool_names=[n for n in names if n],
                          requires_confirmation=tb.get("requires_confirmation"),
                          extensions={"agentspec": keep}))
