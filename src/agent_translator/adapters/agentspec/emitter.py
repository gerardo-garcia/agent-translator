"""IR -> Agent Spec YAML (one file per root agent)."""
from __future__ import annotations

import re
from typing import Any

from ...ir import AgentIR, AgentSpec, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
HTTP = ("get", "post", "put", "patch", "delete")


class Doc:
    """State for one output file."""

    def __init__(self, ir: AgentIR, rep: Report):
        self.ir, self.rep = ir, rep
        self.by_id = {a.id: a for a in ir.agents}
        self.servers = {s["id"]: s for s in ir.resources.mcp_servers}
        self.version = "25.4.1"
        self.llms: dict[str, dict] = {}
        self.llm_uses: dict[str, int] = {}

    def bump(self, v: str) -> None:
        self.version = c.max_version(self.version, v)

    def base(self, ct: str, name: str, *key: str, desc: str | None = None) -> dict[str, Any]:
        return {"component_type": ct, "id": c.uid(*key), "name": name, "description": desc,
                "metadata": {}}

    # ----------------------------------------------------------------- agent
    def agent(self, a: AgentSpec) -> dict[str, Any]:
        path = f"agents[{a.id}]"
        ax = c.ext(a)
        d = self.base("Agent", a.name, "agent", a.id, desc=a.description)
        d["metadata"] = ax.get("metadata", {})
        prompt = a.instructions or ""
        if not prompt:
            self.rep.add(Severity.warning, "instruction-missing", f"{path}.instructions",
                         "system_prompt is required; left empty")
        names = list(dict.fromkeys(PLACEHOLDER.findall(prompt)))
        inputs = list(ax.get("inputs") or [])
        have = {p.get("title") for p in inputs}
        inputs += [{"title": n, "type": "string"} for n in names if n not in have]
        if re.search(r"(?<!\{)\{[A-Za-z_]\w*\}(?!\})", prompt):
            self.rep.add(Severity.warning, "instruction-placeholders", f"{path}.instructions",
                         "single-brace {variables} are literal text in Agent Spec "
                         "(it uses {{variable}} with declared inputs)")
        d["inputs"] = inputs
        d["outputs"] = list(ax.get("outputs") or [])
        d["llm_config"] = {"$llm": self.llm(a, path)}
        d["system_prompt"] = prompt
        tools, boxes = self.tools(a, path)
        d["tools"], d["toolboxes"] = tools, boxes
        d["human_in_the_loop"] = ax.get("human_in_the_loop", True)
        if a.memory.enabled:
            self.rep.add(Severity.lossy, "memory-dropped", path, "no memory concept in Agent Spec")
        if a.guardrails:
            self.rep.add(Severity.lossy, "guardrails-dropped", path,
                         "no guardrails concept in Agent Spec")
        return d

    # ------------------------------------------------------------------- llm
    def llm(self, a: AgentSpec, path: str) -> str:
        m, ax = a.model, c.ext(a).get("llm")
        gen = {k: m.params[ir] for k, ir in ((k, v) for k, v in c.PARAMS.items()) if ir in m.params}
        for k in sorted(set(m.params) - set(c.PARAMS.values())):
            self.rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                         f"Agent Spec generation parameters are temperature, top_p, max_tokens, not {k!r}")
        if ax and ax.get("model_id") == m.id:
            cfg = {k: v for k, v in ax.items() if k not in ("component_type", "name", "model_id",
                                                           "generation_extra")}
            ct = ax["component_type"]
            name = ax.get("name") or m.id
            gen = {**ax.get("generation_extra", {}), **gen}
            body = {"model_id": m.id, **cfg}
        elif not m.id:
            ct, name = "OpenAiCompatibleConfig", "REPLACE-WITH-MODEL"
            body = {"model_id": name, "url": "REPLACE-WITH-ENDPOINT"}
            self.rep.add(Severity.manual, "model-required", f"{path}.model",
                         "the source has no model id; set model_id and the endpoint url")
        elif m.provider == "openai":
            ct, name, body = "OpenAiConfig", m.id, {"model_id": m.id}
        elif m.provider == "google":
            ct, name = "GeminiConfig", m.id
            body = {"model_id": m.id, "auth": {"component_type": "GeminiAIStudioAuthConfig",
                                               "id": c.uid("gemini-auth"), "name": "gemini-auth",
                                               "description": None, "metadata": {}}}
            self.bump("26.1.2")
        else:
            ct, name = "OpenAiCompatibleConfig", m.id
            body = {"model_id": m.id, "url": "REPLACE-WITH-ENDPOINT"}
            self.rep.add(Severity.manual, "model-via-openai-compatible", f"{path}.model",
                         f"Agent Spec has no config for {m.provider or 'this'} model {m.id!r}: "
                         "emitted as an OpenAI-compatible endpoint to fill in (a gateway that "
                         "serves it); the id is not verified")
        key = f"{ct}/{name}/{sorted(body.items(), key=str)}/{sorted(gen.items())}"
        if key not in self.llms:
            cfg = self.base(ct, name, "llm", key)
            cfg.update(body)
            cfg["default_generation_parameters"] = gen or None
            self.llms[key] = cfg
        self.llm_uses[key] = self.llm_uses.get(key, 0) + 1
        return key

    # ----------------------------------------------------------------- tools
    def tools(self, a: AgentSpec, path: str) -> tuple[list[dict], list[dict]]:
        tools: list[dict] = []
        boxes: list[dict] = []
        for t in a.tools:
            tp = f"{path}.tools[{t.id}]"
            if t.kind is ToolKind.function:
                tools.append(self.function(t, a))
            elif t.kind is ToolKind.mcp:
                b = self.mcp(t, a, tp)
                if b:
                    boxes.append(b)
            elif t.kind is ToolKind.openapi:
                tools += self.openapi(t, a, tp)
            elif t.kind is ToolKind.knowledge:
                self.rep.add(Severity.lossy, "knowledge-as-tool", tp,
                             f"Agent Spec has no knowledge-base concept: {t.kb_ref or t.id!r} "
                             "became a search tool with no executor (implement it)")
                tools.append(self.server_tool(t, a, f"search_{c.slug(t.name or t.id)}",
                                              {"type": "object", "properties": {
                                                  "query": {"type": "string"}},
                                               "required": ["query"]}))
            elif t.kind is ToolKind.builtin and t.name == "AMAZON.UserInput":
                self.rep.add(Severity.info, "tool-implicit", tp,
                             "agents can ask the user without a tool")
            else:
                self.rep.add(Severity.lossy, "tool-dropped", tp,
                             f"{t.kind.value} tool {t.name or t.id!r} has no Agent Spec equivalent")
        return tools, boxes

    def confirm(self, d: dict, t: Tool, toolbox: bool = False) -> None:
        if t.requires_confirmation is not None:
            d["requires_confirmation"] = t.requires_confirmation
            self.bump("26.1.2" if toolbox else "25.4.2")

    def server_tool(self, t: Tool, a: AgentSpec, name: str, schema: dict | None,
                    ct: str = "ServerTool", outputs: list | None = None) -> dict:
        d = self.base(ct, name, "tool", a.id, t.id, desc=t.description)
        d["inputs"] = c.schema_to_props(schema)
        d["outputs"] = outputs or []
        self.confirm(d, t)
        return d

    def function(self, t: Tool, a: AgentSpec) -> dict:
        tx = c.ext(t)
        name = t.name or t.id
        ct = tx.get("component_type", "ServerTool")
        if ct == "RemoteTool" and tx.get("remote") and t.impl_ref:
            d = self.server_tool(t, a, name, t.parameters, "RemoteTool", tx.get("outputs"))
            d.update({"url": t.impl_ref, **tx["remote"]})
            return d
        return self.server_tool(t, a, name, t.parameters, "ClientTool" if ct == "ClientTool"
                                else "ServerTool", tx.get("outputs"))

    def mcp(self, t: Tool, a: AgentSpec, path: str) -> dict | None:
        s = self.servers.get(t.server_ref or "")
        if not s or not s.get("url"):
            self.rep.add(Severity.manual, "mcp-server-unknown", path,
                         f"MCP server {t.server_ref!r} has no URL; toolbox dropped")
            return None
        proto = str(s.get("protocol", ""))
        if proto == "stdio":
            self.rep.add(Severity.manual, "mcp-stdio", path, "stdio MCP server; toolbox dropped")
            return None
        tx = c.ext(t)
        ct = "SSETransport" if proto == "sse" else "StreamableHTTPTransport"
        tr = self.base(ct, "MCP Client", "transport", a.id, t.id)
        tr.update({"url": s["url"], "headers": None, **tx.get("transport", {})})
        d = self.base("MCPToolBox", t.name or t.id, "toolbox", a.id, t.id, desc=t.description)
        d["client_transport"] = tr
        d["tool_filter"] = t.tool_names or None
        self.confirm(d, t, toolbox=True)
        self.bump("25.4.2")
        return d

    def openapi(self, t: Tool, a: AgentSpec, path: str) -> list[dict]:
        spec = t.spec
        if not isinstance(spec, dict) or not spec.get("paths"):
            self.rep.add(Severity.manual, "schema-required", path, "no inline OpenAPI spec")
            return []
        servers = spec.get("servers") or []
        base = (servers[0].get("url", "") if servers else "").rstrip("/")
        if not base:
            self.rep.add(Severity.manual, "openapi-no-server", path,
                         "the spec has no `servers`: add the API URL to each RemoteTool")
        out = []
        for p, item in spec["paths"].items():
            for m in HTTP:
                op = (item or {}).get(m)
                if not isinstance(op, dict):
                    continue
                name = op.get("operationId") or f"{m}_{c.slug(p)}"
                props: dict[str, Any] = {}
                req: list[str] = []
                query: dict[str, str] = {}
                for prm in (item.get("parameters") or []) + (op.get("parameters") or []):
                    props[prm["name"]] = {"type": (prm.get("schema") or {}).get("type", "string"),
                                          **({"description": prm["description"]}
                                             if prm.get("description") else {})}
                    if prm.get("required") or prm.get("in") == "path":
                        req.append(prm["name"])
                    if prm.get("in") == "query":
                        query[prm["name"]] = "{{" + prm["name"] + "}}"
                body = ((op.get("requestBody") or {}).get("content", {}).get(
                    "application/json", {}).get("schema") or {})
                data: Any = {}
                for k, v in (body.get("properties") or {}).items():
                    props[k] = {kk: vv for kk, vv in v.items() if kk in ("type", "description")}
                    data[k] = "{{" + k + "}}"
                    if k in (body.get("required") or []):
                        req.append(k)
                url = base + re.sub(r"\{(\w+)\}", r"{{\1}}", p)
                d = self.server_tool(t, a, name, {"type": "object", "properties": props,
                                                  "required": req}, "RemoteTool")
                d["description"] = op.get("summary") or op.get("description") or t.description
                d["id"] = c.uid("tool", a.id, t.id, name)
                d.update({"url": url, "http_method": m.upper(), "api_spec_uri": None, "data": data,
                          "query_params": query, "headers": {}})
                out.append(d)
        return out


def emit_doc(ir: AgentIR, root: AgentSpec, rep: Report) -> dict[str, Any]:
    doc = Doc(ir, rep)
    mode = root.collaboration.mode
    urls = [r for r in root.remote_agents if r.ref and r.ref.startswith(("http://", "https://"))]
    for r in root.remote_agents:
        if r not in urls:
            rep.add(Severity.lossy, "remote-agent-dropped", f"agents[{root.id}].remote_agents[{r.id}]",
                    f"{r.name!r} ({r.ref or 'no locator'}) is not an A2A URL")
    kids = [doc.by_id[s] for s in root.sub_agents]
    if mode in ("supervisor", "router") and (kids or urls):
        if mode == "router":
            rep.add(Severity.lossy, "collaboration-mode", f"agents[{root.id}]",
                    "router mode does not exist in Agent Spec; using ManagerWorkers")
        top = doc.base("ManagerWorkers", root.name, "mw", root.id)
        top.update({"inputs": [], "outputs": []})
        doc.bump("26.1.0")
        top["group_manager"] = doc.agent(root)
        workers = []
        for k in kids:
            if k.sub_agents:
                rep.add(Severity.lossy, "nesting-dropped", f"agents[{k.id}]",
                        "ManagerWorkers has one level: this worker's own sub-agents are not included")
            workers.append(doc.agent(k))
        for r in urls:
            w = doc.base("A2AAgent", r.name, "a2a", root.id, r.id, desc=r.description)
            ax = c.ext(r).get("a2a", {})
            w.update({"inputs": [], "outputs": [], "agent_url": r.ref,
                      "connection_config": ax.get("connection_config") or {
                          "timeout": 30.0, "headers": None, "verify": True},
                      "session_parameters": ax.get("session_parameters") or {
                          "timeout": 60.0, "poll_interval": 60.0, "max_retries": 3}})
            workers.append(w)
        top["workers"] = workers
    elif mode == "handoff" and kids:
        top = doc.base("Swarm", root.name, "swarm", root.id)
        top.update({"inputs": [], "outputs": [], "handoff": True})
        doc.bump("26.1.0")
        seen: dict[str, dict] = {}
        edges: list[tuple[str, str]] = []
        todo = [root]
        while todo:
            a = todo.pop(0)
            if a.id in seen:
                continue
            seen[a.id] = doc.agent(a)
            for s in a.sub_agents:
                edges.append((a.id, s))
                todo.append(doc.by_id[s])
        top["first_agent"] = seen[root.id]
        top["relationships"] = [[seen[x], seen[y]] for x, y in edges]
    else:
        if mode in ("sequential", "parallel", "loop") and kids:
            rep.add(Severity.lossy, "collaboration-mode", f"agents[{root.id}]",
                    f"{mode} execution is a Flow in Agent Spec (not translated); the agent is "
                    "emitted alone and its sub-agents as separate files")
        elif root.sub_agents or root.remote_agents:
            rep.add(Severity.lossy, "collaboration-dropped", f"agents[{root.id}]",
                    "sub-agents could not be linked")
        top = doc.agent(root)
    return finalize(doc, top)


def finalize(doc: Doc, top: dict) -> dict[str, Any]:
    refs: dict[str, dict] = {}
    shared = {k for k, n in doc.llm_uses.items() if n > 1}

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            if "$llm" in x and len(x) == 1:
                cfg = doc.llms[x["$llm"]]
                if x["$llm"] in shared:
                    refs[cfg["id"]] = cfg
                    return {"$component_ref": cfg["id"]}
                return dict(cfg)
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    out = walk(top)
    if refs:
        out["$referenced_components"] = refs
    out["agentspec_version"] = doc.version
    return out
