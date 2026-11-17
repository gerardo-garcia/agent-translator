"""ADK Agent Config YAML (root_agent.yaml and referenced files) -> IR."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...ir import AgentSpec, Collaboration, ModelSpec, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import common as c


class Parser:
    def __init__(self, rep: Report):
        self.rep = rep
        self.agents: dict[str, AgentSpec] = {}
        self.by_file: dict[Path, str] = {}
        self.servers: dict[str, dict] = {}     # id -> mcp server entry
        self._busy: set[Path] = set()

    # ----------------------------------------------------------------- files
    def parse_file(self, path: Path) -> str | None:
        path = path.resolve()
        if path in self.by_file:
            return self.by_file[path]
        if path in self._busy:
            self.rep.add(Severity.error, "agent-cycle", str(path), "agent files reference each other")
            return None
        if not path.is_file():
            self.rep.add(Severity.manual, "agent-file-missing", str(path),
                         "referenced agent file does not exist")
            return None
        self._busy.add(path)
        try:
            d = c.yaml_safe().load(path.read_text())
            if not isinstance(d, dict) or "name" not in d:
                self.rep.add(Severity.error, "not-an-agent", str(path), "no agent definition")
                return None
            aid = self._agent(d, path) or None
            if aid:
                self.by_file[path] = aid
            return aid
        finally:
            self._busy.discard(path)

    def _ref(self, ref: Any, base: Path, where: str) -> tuple[str | None, str | None]:
        """(agent id, code locator) for an AgentRefConfig."""
        if isinstance(ref, dict) and ref.get("config_path"):
            return self.parse_file(base.parent / ref["config_path"]), None
        if isinstance(ref, dict) and ref.get("code"):
            return None, ref["code"]
        self.rep.add(Severity.warning, "agent-ref-unknown", where, f"unrecognised reference {ref!r}")
        return None, None

    # ----------------------------------------------------------------- agent
    def _agent(self, d: dict, path: Path) -> str:
        name = d["name"]
        where = f"{path.name}:{name}"
        cls = c.short_class(d.get("agent_class") or c.LLM_CLASS)
        ax: dict[str, Any] = {}
        if cls not in c.KNOWN_CLASSES and "edges" in d:
            self.rep.add(Severity.manual, "workflow-graph-unsupported", where,
                         f"agent_class {d.get('agent_class')!r} (graph workflow) is not translated; "
                         "the agent files it references are parsed as standalone agents")
            for ref in self._yaml_refs(d.get("edges")):
                self.parse_file(path.parent / ref)
            return ""
        if cls not in c.KNOWN_CLASSES:
            ax["agentClass"] = d.get("agent_class")
            self.rep.add(Severity.manual, "agent-class-unsupported", where,
                         f"agent_class {d.get('agent_class')!r} is not translated; "
                         "its fields are kept in extensions.adk")

        model, mx = self._model(d, where)
        ax.update(mx)
        gcc = d.get("generate_content_config") or {}
        if gcc:
            params = {c.PARAMS[k]: v for k, v in gcc.items() if k in c.PARAMS}
            model.params.update(params)
            rest = {k: v for k, v in gcc.items() if k not in c.PARAMS}
            if rest:
                ax["generateContentConfig"] = rest
                self.rep.add(Severity.warning, "generation-config-adk-only", where,
                             f"{sorted(rest)} kept in extensions.adk only")

        tools: list[Tool] = []
        sub: list[str] = []
        remote: list[RemoteAgent] = []
        agent_tools: dict[str, dict] = {}
        for i, t in enumerate(d.get("tools") or []):
            self._tool(t, i, name, path, f"{where}.tools[{i}]", tools, sub, remote, agent_tools)
        transfer: list[str] = []
        for i, ref in enumerate(d.get("sub_agents") or []):
            aid, code = self._ref(ref, path, f"{where}.sub_agents[{i}]")
            if aid:
                transfer.append(aid)
            elif code:
                remote.append(self._code_agent(code))
        sub = transfer + [s for s in sub if s not in transfer]
        if agent_tools:
            ax["agentTools"] = agent_tools

        mode = "none"
        if cls in c.WORKFLOW_CLASSES:
            mode = c.WORKFLOW_CLASSES[cls]
        elif transfer or any(r.extensions.get("adk", {}).get("transfer") for r in remote):
            mode = "handoff"
        elif sub or remote:
            mode = "supervisor"
        for r in remote:
            r.extensions.get("adk", {}).pop("transfer", None)

        leftover = {k: v for k, v in d.items() if k not in c.MAPPED_KEYS}
        if leftover:
            ax["fields"] = leftover
            cbs = sorted(k for k in leftover if k.endswith("_callbacks"))
            if cbs:
                self.rep.add(Severity.warning, "callbacks-adk-only", where,
                             f"{cbs} are Python callbacks; kept in extensions.adk only")
            other = sorted(set(leftover) - set(cbs))
            if other:
                self.rep.add(Severity.info, "adk-fields-preserved", where,
                             f"{other} kept in extensions.adk")
        instruction = d.get("instruction")
        if isinstance(instruction, str):
            ph = sorted(set(c.PLACEHOLDER.findall(instruction)))
            if ph:
                self.rep.add(Severity.info, "instruction-placeholders", where,
                             f"ADK state placeholders {ph} are not understood by other platforms")
        elif cls == c.LLM_CLASS:
            self.rep.add(Severity.warning, "instruction-missing", where, "LlmAgent without instruction")

        aid = name
        n = 2
        while aid in self.agents:
            aid, n = f"{name}-{n}", n + 1
        self.agents[aid] = AgentSpec(
            id=aid, name=name, description=d.get("description"),
            instructions=instruction if isinstance(instruction, str) else None, model=model,
            tools=tools, sub_agents=sub, remote_agents=remote,
            collaboration=Collaboration(mode=mode, max_iterations=d.get("max_iterations")),
            extensions={"adk": {"agentClass": ax.pop("agentClass", cls), **ax}})
        return aid

    def _yaml_refs(self, v: Any):
        if isinstance(v, str) and v.endswith((".yaml", ".yml")):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from self._yaml_refs(x)
        elif isinstance(v, list):
            for x in v:
                yield from self._yaml_refs(x)

    def _model(self, d: dict, where: str) -> tuple[ModelSpec, dict]:
        if d.get("model_code"):
            self.rep.add(Severity.manual, "model-code", where,
                         f"model is built in Python ({d['model_code'].get('name')}); id unknown")
            return ModelSpec(), {"modelCode": d["model_code"]}
        raw = d.get("model")
        if not raw:
            self.rep.add(Severity.info, "model-inherited", where,
                         "no model set: ADK inherits it from the parent agent or its default")
            return ModelSpec(), {}
        vendor, mid = c.vendor_of(raw)
        self.rep.add(Severity.info, "model-unverified", where, f"model id {raw!r} copied as-is")
        return ModelSpec(provider=vendor, id=mid), {"modelString": raw}

    def _code_agent(self, code: str) -> RemoteAgent:
        n = code.rsplit(".", 1)[-1]
        self.rep.add(Severity.manual, "agent-code-reference", code,
                     "sub-agent is defined in Python; kept as a remote agent reference")
        return RemoteAgent(id=n, name=n, ref=f"python:{code}",
                           extensions={"adk": {"code": code, "transfer": True}})

    # ----------------------------------------------------------------- tools
    def _tool(self, t: dict, i: int, agent: str, path: Path, where: str, tools: list[Tool],
              sub: list[str], remote: list[RemoteAgent], agent_tools: dict) -> None:
        name, args = t.get("name", ""), dict(t.get("args") or {})
        short = name.rsplit(".", 1)[-1]
        used = {x.id for x in tools}

        def tid(base: str) -> str:
            k, cand = 2, base
            while cand in used:
                cand, k = f"{base}-{k}", k + 1
            return cand

        if name in c.MCP_TOOLSETS:
            tools.append(self._mcp(args, tid(f"mcp-{i + 1}"), agent, where))
        elif name == "AgentTool":
            aid, code = self._ref(args.get("agent"), path, where)
            extra = {k: v for k, v in args.items() if k != "agent"}
            if aid:
                sub.append(aid)
                agent_tools[aid] = extra
            elif code:
                r = self._code_agent(code)
                r.extensions["adk"].pop("transfer")
                r.extensions["adk"]["agentTool"] = extra
                remote.append(r)
        elif name == "LongRunningFunctionTool":
            fn = str(args.get("func", ""))
            rest = {k: v for k, v in args.items() if k != "func"}
            tools.append(Tool(id=tid(c.py_name(fn.rsplit(".", 1)[-1])), kind=ToolKind.function,
                              name=fn.rsplit(".", 1)[-1], impl_ref=fn,
                              extensions={"adk": {"longRunning": True, **({"args": rest}
                                                                          if rest else {})}}))
            self._unknown_sig(where, fn)
        elif name in c.KNOWLEDGE_TOOLS:
            ref = args.get("data_store_id") or args.get("rag_corpora")
            tools.append(Tool(id=tid(c.py_name(name)), kind=ToolKind.knowledge, name=name,
                              kb_ref=ref if isinstance(ref, str) else None,
                              extensions={"adk": {"tool": name, "args": args}}))
        elif "." in name:
            tools.append(Tool(id=tid(c.py_name(short)), kind=ToolKind.function, name=short,
                              impl_ref=name,
                              extensions={"adk": {"args": args}} if args else {}))
            if args:
                self.rep.add(Severity.warning, "tool-factory", where,
                             f"{name} receives args {sorted(args)}: it may be a toolset/factory, "
                             "not a plain function")
            else:
                self._unknown_sig(where, name)
        else:
            if name not in c.BUILTIN_TOOLS:
                self.rep.add(Severity.warning, "tool-unknown", where,
                             f"{name!r} is not a tool this adapter knows; kept as a built-in")
            tools.append(Tool(id=tid(c.py_name(name)), kind=ToolKind.builtin, name=name,
                              extensions={"adk": {"args": args}} if args else {}))

    def _unknown_sig(self, where: str, fn: str) -> None:
        self.rep.add(Severity.warning, "function-signature-unknown", where,
                     f"{fn}: parameters come from Python code and are not in the YAML")

    def _mcp(self, args: dict, tool_id: str, agent: str, where: str) -> Tool:
        keys = ("streamable_http_connection_params", "sse_connection_params",
                "stdio_connection_params", "stdio_server_params")
        key = next((k for k in keys if args.get(k)), None)
        conn = args.get(key, {}) if key else {}
        entry: dict[str, Any] = {}
        if key in ("streamable_http_connection_params", "sse_connection_params"):
            url = conn.get("url")
            sid = re.sub(r"\W+", "-", re.sub(r"^https?://", "", str(url))).strip("-")[:40] or tool_id
            entry = {"id": sid, "url": url,
                     "protocol": "sse" if key.startswith("sse") else "streamable-http"}
        else:
            params = conn.get("server_params", conn) if key else {}
            cmd = " ".join([str(params.get("command", ""))] + [str(a) for a in params.get("args", [])])
            last = (params.get("args") or [params.get("command") or tool_id])[-1]
            sid = re.sub(r"\W+", "-", str(last)).strip("-")[:40] or tool_id
            entry = {"id": sid, "protocol": "stdio", "description": cmd.strip() or None}
            self.rep.add(Severity.manual, "mcp-stdio", where,
                         "stdio MCP servers run as local processes; a remote cloud agent "
                         "needs an HTTP/SSE endpoint instead")
        entry = {k: v for k, v in entry.items() if v is not None}
        self.servers.setdefault(entry["id"], entry)
        rest = {k: v for k, v in args.items() if k not in ("tool_filter",)}
        flt = args.get("tool_filter")
        return Tool(id=tool_id, kind=ToolKind.mcp, server_ref=entry["id"],
                    tool_names=list(flt) if isinstance(flt, list) else [],
                    extensions={"adk": {"args": rest}} if rest else {})
