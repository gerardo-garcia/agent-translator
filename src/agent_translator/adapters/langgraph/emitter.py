"""IR -> LangGraph project: one self-contained module per root agent, ``langgraph.json``,
``requirements.txt``.

Mapping: an LLM agent is ``langchain.agents.create_agent``; a supervisor's sub-agents become tools
that invoke them; sequential/parallel workflow agents are a ``StateGraph`` of the sub-agents.
Anything LangGraph cannot express from the IR is reported, never dropped quietly.
"""
from __future__ import annotations

import ast
import itertools
import json

from ...ir import AgentIR, AgentSpec, Tool, ToolKind
from ...models import lookup
from ...report import Report, Severity
from ..adk.common import py_name
from ..adk.emitter import _stub
from ..bedrock.emitter import BEDROCK_MODEL

PROVIDERS = {  # vendor -> (init_chat_model prefix, pip package)
    "anthropic": ("anthropic", "langchain-anthropic"),
    "openai": ("openai", "langchain-openai"),
    "google": ("google_genai", "langchain-google-genai"),
    "bedrock": ("bedrock_converse", "langchain-aws"),
}
# IR param -> init_chat_model kwarg; top_k only exists on some chat models
PARAMS = {"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
          "top_k": "top_k"}
TOP_K_VENDORS = {"anthropic", "google"}
ENV_VARS = {  # vendor key -> environment variables its chat model reads
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GOOGLE_API_KEY"],
    "bedrock": ["AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
}
RESERVED = {"graph", "make_graph", "init_chat_model", "create_agent", "tool", "StateGraph",
            "MessagesState", "START", "END", "MultiServerMCPClient", "Optional", "os"}
WORKFLOW = {"sequential", "parallel", "loop"}


def lit(s: str) -> str:
    """Python string literal; readable triple quotes for multi-line text when that is safe."""
    if ("\n" in s and '"""' not in s and "\\" not in s and not s.endswith('"')
            and s.replace("\n", " ").isprintable()):
        return f'"""{s}"""'
    return repr(s)


def _model_str(a: AgentSpec, rep: Report, path: str) -> tuple[str | None, str | None]:
    """(init_chat_model string, vendor key) for the agent's model."""
    m = a.model
    if not m.id:
        rep.add(Severity.manual, "model-required", f"{path}.model",
                "the source has no model: set the AGENT_MODEL environment variable "
                "(init_chat_model format, e.g. 'anthropic:<id>')")
        return None, None
    if BEDROCK_MODEL.match(m.id):
        key = "bedrock"
    else:
        match = lookup(m.id)
        key = (match.vendor if match else m.provider) or ""
    if key not in PROVIDERS:
        rep.add(Severity.manual, "model-unmapped", f"{path}.model",
                f"model {m.id!r} ({m.provider}) has no known LangChain provider; copied as-is: "
                "prefix it with the right provider (init_chat_model 'provider:id')")
        return m.id, None
    return f"{PROVIDERS[key][0]}:{m.id}", key


class Module:
    """One root agent and everything it needs, rendered as one Python module."""

    def __init__(self, root: AgentSpec, ir: AgentIR, rep: Report):
        self.root, self.ir, self.rep = root, ir, rep
        self.by_id = {a.id: a for a in ir.agents}
        self.servers = {s["id"]: s for s in ir.resources.mcp_servers}
        self.names: set[str] = set(RESERVED)
        self.var: dict[str, str] = {}
        self.visiting: set[str] = set()
        self.stubs: list[str] = []
        self.stub_of: dict[str, str] = {}
        self.body: list[str] = []
        self.mcp_used: dict[str, dict] = {}      # server id -> connection
        self.packages = {"langgraph", "langchain"}
        self.imports: set[str] = set()
        self.workflow = False
        self.env: set[str] = set()
        self.reachable = self._reachable(root)
        self.is_async = any(self._mcp_conn(t) for a in self.reachable for t in a.tools)

    # ------------------------------------------------------------ helpers
    def _reachable(self, root: AgentSpec) -> list[AgentSpec]:
        seen: dict[str, AgentSpec] = {}
        todo = [root]
        while todo:
            a = todo.pop()
            if a.id in seen:
                continue
            seen[a.id] = a
            todo += [self.by_id[s] for s in a.sub_agents]
            todo += [self.by_id[t.agent_ref] for t in a.tools
                     if t.kind is ToolKind.agent and t.agent_ref in self.by_id]
        return list(seen.values())

    def unique(self, base: str) -> str:
        base = py_name(base)
        n, i = base, 2
        while n in self.names:
            n, i = f"{base}_{i}", i + 1
        self.names.add(n)
        return n

    def _mcp_conn(self, t: Tool) -> dict | None:
        if t.kind is not ToolKind.mcp:
            return None
        s = self.servers.get(t.server_ref or "")
        if not s or not s.get("url"):
            return None
        sse = "sse" in str(s.get("protocol", "")).lower()
        return {"transport": "sse" if sse else "streamable_http", "url": s["url"]}

    # -------------------------------------------------------------- tools
    def _function_stub(self, t: Tool, path: str, why: str) -> str:
        if t.id in self.stub_of:
            return self.stub_of[t.id]
        fn = self.unique(t.name or t.id)
        self.stub_of[t.id] = fn
        self.stubs.append(_stub(fn, t))
        self.rep.add(Severity.manual, "function-stub-generated", path,
                     f"{fn}() in the generated module is a stub: implement it ({why})")
        return fn

    def _delegate_tool(self, parent: AgentSpec, child: AgentSpec, how: str | None, path: str) -> str:
        fn = self.unique(f"call_{child.id}")
        desc = how or child.description or f"Delegate a task to the {child.name} agent."
        kw = "async def" if self.is_async else "def"
        aw = ("await ", "ainvoke") if self.is_async else ("", "invoke")
        self.body += [
            f"@tool({fn!r}, description={lit(desc)})",
            f"{kw} {fn}(request: str) -> str:",
            (f"    result = {aw[0]}{self.var[child.id]}.{aw[1]}("
             f'{{"messages": [{{"role": "user", "content": request}}]}})'),
            '    return result["messages"][-1].text',
            "",
            "",
        ]
        return fn

    def _tools(self, a: AgentSpec, path: str) -> list[str]:
        exprs: list[str] = []
        called: set[str] = set()
        mode = a.collaboration.mode
        if a.sub_agents and mode not in WORKFLOW:
            if mode == "handoff":
                self.rep.add(Severity.lossy, "handoff-as-tool", f"{path}.collaboration",
                             "control is not transferred to the sub-agent: the parent calls it "
                             "as a tool and keeps control (supervisor pattern)")
            for s in a.sub_agents:
                child = self.by_id[s]
                if s in self.var:
                    exprs.append(self._delegate_tool(a, child, a.collaboration.instructions.get(s),
                                                     path))
                    called.add(s)
        for t in a.tools:
            tp = f"{path}.tools[{t.id}]"
            if t.requires_confirmation:
                self.rep.add(Severity.lossy, "confirmation-dropped", tp,
                             "no per-call approval is generated; add langgraph.types.interrupt() "
                             "to the tool to ask a human")
            if t.kind is ToolKind.mcp:
                conn = self._mcp_conn(t)
                ref = t.server_ref or ""
                if conn is None:
                    self.rep.add(Severity.manual, "mcp-server-undefined", tp,
                                 f"no URL known for MCP server {ref!r}; tool dropped")
                    continue
                s = self.servers[ref]
                if s.get("headers"):
                    self.rep.add(Severity.manual, "mcp-headers-dropped", tp,
                                 "headers (possibly credentials) are not copied; add them to the "
                                 "connection's `headers`")
                self.mcp_used[ref] = conn
                var = self.mcp_var(ref)
                if t.tool_names:
                    names = ", ".join(repr(n) for n in t.tool_names)
                    exprs.append(f"*[t for t in {var} if t.name in ({names},)]")
                else:
                    exprs.append(f"*{var}")
            elif t.kind is ToolKind.function:
                exprs.append(self._function_stub(t, tp, f"original: {t.impl_ref or 'unknown'}"))
            elif t.kind is ToolKind.knowledge:
                kb = t.model_copy(update={
                    "parameters": {"properties": {"query": {"type": "string",
                                                            "description": "search query"}},
                                   "required": ["query"]},
                    "description": t.description or f"Search the knowledge base {t.kb_ref or t.id}."})
                exprs.append(self._function_stub(kb, tp, f"query knowledge base {t.kb_ref!r}"))
            elif t.kind is ToolKind.openapi:
                self.rep.add(Severity.lossy, "openapi-spec-dropped", tp,
                             "the OpenAPI spec is not translated; the stub must call the API")
                exprs.append(self._function_stub(t, tp, "an OpenAPI tool"))
            elif t.kind is ToolKind.agent:
                child = self.by_id.get(t.agent_ref or "")
                if child and child.id in self.var:
                    if child.id not in called:
                        exprs.append(self._delegate_tool(a, child, t.description, tp))
                        called.add(child.id)
                else:
                    self.rep.add(Severity.manual, "tool-unsupported", tp,
                                 "agent tool without a resolvable target agent; dropped")
            else:
                self.rep.add(Severity.manual, "tool-unsupported", tp,
                             f"{t.kind.value} tools have no LangChain equivalent; dropped")
        for r in a.remote_agents:
            rp = f"{path}.remote_agents[{r.id}]"
            exprs.append(self._function_stub(
                Tool(id=f"remote_{r.id}", kind=ToolKind.function, name=f"call_{r.name}",
                     description=r.instruction or r.description or f"Delegate to {r.name}.",
                     parameters={"properties": {"request": {"type": "string"}},
                                 "required": ["request"]}),
                rp, f"remote agent {r.ref or 'no locator'}"))
        return exprs

    def mcp_var(self, ref: str) -> str:
        return f"mcp_{py_name(ref)}"

    # ------------------------------------------------------------- agents
    def build(self, a: AgentSpec) -> str | None:
        """Emit the code of agent `a` (children first); returns its variable name."""
        if a.id in self.var:
            return self.var[a.id]
        if a.id in self.visiting:
            self.rep.add(Severity.lossy, "agent-cycle", f"agents[{a.id}]",
                         "circular sub-agent reference dropped")
            return None
        self.visiting.add(a.id)
        path = f"agents[{a.id}]"
        mode = a.collaboration.mode
        kids = []
        for s in a.sub_agents:
            if self.build(self.by_id[s]):
                kids.append(s)
        var = self.unique(a.id)
        if mode in WORKFLOW and kids:
            self._workflow(a, var, kids, path)
        else:
            if mode in WORKFLOW:
                self.rep.add(Severity.warning, "workflow-without-children", path,
                             f"{mode} agent has no sub-agents: emitted as a plain agent")
            self.var[a.id] = var     # visible to its delegate tools
            self._llm_agent(a, var, path)
        self.var[a.id] = var
        self.visiting.discard(a.id)
        return var

    def _llm_agent(self, a: AgentSpec, var: str, path: str) -> None:
        self.imports.add("from langchain.agents import create_agent")
        self.imports.add("from langchain.chat_models import init_chat_model")
        model, vendor = _model_str(a, self.rep, path)
        if vendor:
            self.packages.add(PROVIDERS[vendor][1])
            self.env.update(ENV_VARS[vendor])
        kwargs = []
        for k, v in a.model.params.items():
            key = PARAMS.get(k)
            if key is None or isinstance(v, (dict, list)) or (
                    key == "top_k" and vendor not in TOP_K_VENDORS):
                self.rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                             "no equivalent chat model argument")
                continue
            kwargs.append(f"{key}={v!r}")
        if model is None:
            self.imports.add("import os")
            self.env.add("AGENT_MODEL")
            first = 'os.environ["AGENT_MODEL"]'
        else:
            first = repr(model)
        mexpr = f"init_chat_model({', '.join([first, *kwargs])})"
        tools = self._tools(a, path)
        if a.description:
            self.body.append(f"# {a.name}: {a.description.strip().splitlines()[0]}")
        lines = [f"{var} = create_agent(", f"    model={mexpr},"]
        if tools:
            lines.append(f"    tools=[{', '.join(tools)}],")
        if a.instructions:
            lines.append(f"    system_prompt={lit(a.instructions)},")
        else:
            self.rep.add(Severity.warning, "instruction-missing", f"{path}.instructions",
                         "the agent has no system prompt")
        lines += [f"    name={py_name(a.name)!r},", ")", ""]
        self.body += lines
        if a.memory.enabled:
            self.rep.add(Severity.warning, "memory-checkpointer", f"{path}.memory",
                         "memory is enabled: LangGraph Platform persists threads by itself; for "
                         "local runs compile with a checkpointer (e.g. InMemorySaver). The TTL is "
                         "not translated")
        if a.guardrails:
            self.rep.add(Severity.lossy, "guardrails-dropped", f"{path}.guardrails",
                         "LangChain has no guardrail setting; use middleware if needed")

    def _workflow(self, a: AgentSpec, var: str, kids: list[str], path: str) -> None:
        mode = a.collaboration.mode
        self.imports.add("from langgraph.graph import END, START, MessagesState, StateGraph")
        if a.instructions or a.tools or a.model.id:
            self.rep.add(Severity.lossy, "workflow-agent-llm-dropped", path,
                         f"a {mode} workflow has no LLM: its instructions, model and tools are "
                         "dropped")
        if mode == "loop":
            self.rep.add(Severity.lossy, "loop-single-pass", f"{path}.collaboration",
                         "the loop is emitted as one sequential pass (no repeat or "
                         "max_iterations); add a conditional edge back to the first node")
        b = f"{var}_builder"
        names = [py_name(s) for s in kids]
        self.body.append(f"{b} = StateGraph(MessagesState)")
        for s, n in zip(kids, names, strict=True):
            self.body.append(f"{b}.add_node({n!r}, {self.var[s]})")
        if mode == "parallel":
            for n in names:
                self.body += [f"{b}.add_edge(START, {n!r})", f"{b}.add_edge({n!r}, END)"]
        else:
            chain = ["START", *(repr(n) for n in names), "END"]
            self.body += [f"{b}.add_edge({x}, {y})" for x, y in itertools.pairwise(chain)]
        self.body += [f"{var} = {b}.compile(name={py_name(a.name)!r})", ""]
        self.workflow = True

    # ------------------------------------------------------------- render
    def render(self, graph_var: str) -> str:
        head = ['"""Generated by agent-translator. Review before use.', "",
                f"Root agent: {self.root.name}. Install: pip install -r requirements.txt.",
                "Set the API key environment variable of each model provider used.",
                "Stubs below (if any) raise NotImplementedError until you implement them.", '"""',
                "from __future__ import annotations", ""]
        imps = set(self.imports)
        if any("Optional" in s for s in self.stubs):
            imps.add("from typing import Optional")
        if self.mcp_used:
            imps.add("from langchain_mcp_adapters.client import MultiServerMCPClient")
        if any(ln.startswith("@tool") for ln in self.body):
            imps.add("from langchain_core.tools import tool")
        std = sorted(i for i in imps if i.startswith(("import os", "from typing")))
        rest = sorted(i for i in imps if i not in std)
        out = head + std + ([""] if std else []) + rest + [""]
        if self.mcp_used:
            conns = json.dumps(self.mcp_used, indent=4)
            out += ["", f"mcp_client = MultiServerMCPClient({conns})", ""]
        for s in self.stubs:
            out += ["", "", s.rstrip()]
        out += ["", ""]
        if self.is_async:
            out += ["", "async def make_graph():",
                    '    """Async factory: MCP tools are fetched when the graph is built."""']
            body = [f"{self.mcp_var(r)} = await mcp_client.get_tools(server_name={r!r})"
                    for r in self.mcp_used] + self.body + [f"return {graph_var}"]
            out += [("    " + ln if ln else "") for ln in body]
        else:
            out += self.body + [f"graph = {graph_var}"]
        src = "\n".join(out).rstrip() + "\n"
        ast.parse(src)       # never write Python that does not compile
        return src


def env_example(env: set[str]) -> str:
    lines = ["# Copy to .env (langgraph.json loads it) and fill in the values.", ""]
    for v in sorted(env):
        lines.append(f"{v}=" if v != "AGENT_MODEL" else "AGENT_MODEL=provider:model-id")
    lines += ["", "# Optional: do not send traces to LangSmith.", "LANGSMITH_TRACING=false", ""]
    return "\n".join(lines)


def readme(graphs: dict[str, str], is_async: bool, stubs: list[str], mcp: bool) -> str:
    first = next(iter(graphs))
    out = ["# Generated LangGraph project", "",
           "Generated by agent-translator: review before use.", "",
           "## Run it locally", "",
           "```bash",
           "python -m venv .venv && . .venv/bin/activate",
           "pip install -r requirements.txt",
           'pip install -U "langgraph-cli[inmem]"',
           "cp .env.example .env     # then fill in the keys",
           "langgraph dev            # API on http://localhost:2024, no Docker",
           "```", "",
           "Graphs served (`langgraph.json`): " + ", ".join(f"`{g}`" for g in graphs) + ".", "",
           "Try it (replace the graph name if needed):", "",
           "```bash",
           "curl -s http://localhost:2024/runs/wait -H 'Content-Type: application/json' \\",
           (f"  -d '{{\"assistant_id\": \"{first}\", \"input\": {{\"messages\": "
            "[{\"role\": \"user\", \"content\": \"Hello\"}]}}'"),
           "```", "",
           "Without a server: `from graph import graph; graph.invoke({\"messages\": "
           "[{\"role\": \"user\", \"content\": \"Hello\"}]})`"
           + (" (modules with MCP tools expose `async def make_graph()` instead: "
              "`await make_graph()`)." if is_async else "."), ""]
    if stubs:
        out += ["## Before it can do real work", "",
                ("Some tools were generated as stubs that raise `NotImplementedError`. "
                 "Implement them in the generated module(s); the translation report lists them "
                 "(`function-stub-generated`)."), ""]
    if mcp:
        out += ["MCP servers are contacted when the graph is built: they must be reachable.", ""]
    return "\n".join(out)


def emit_project(ir: AgentIR, rep: Report) -> dict[str, str]:
    children = {s for a in ir.agents for s in a.sub_agents}
    children |= {t.agent_ref for a in ir.agents for t in a.tools
                 if t.kind is ToolKind.agent and t.agent_ref}
    roots = [a for a in ir.agents if a.id not in children] or ir.agents[:1]
    files: dict[str, str] = {}
    graphs: dict[str, str] = {}
    packages: set[str] = set()
    env: set[str] = set()
    stubs: list[str] = []
    is_async = mcp = False
    for r in roots:
        m = Module(r, ir, rep)
        var = m.build(r)
        fname = "graph.py" if len(roots) == 1 else f"{py_name(r.id)}.py"
        files[fname] = m.render(var or "")
        graphs[py_name(r.id)] = f"./{fname}:{'make_graph' if m.is_async else 'graph'}"
        env |= m.env
        stubs += m.stubs
        is_async |= m.is_async
        mcp |= bool(m.mcp_used)
        packages |= m.packages | ({"langchain-mcp-adapters"} if m.mcp_used else set())
    files["langgraph.json"] = json.dumps(
        {"dependencies": ["."], "graphs": graphs, "env": ".env"}, indent=2) + "\n"
    files[".env.example"] = env_example(env)
    files["README.md"] = readme(graphs, is_async, stubs, mcp)
    files["requirements.txt"] = "\n".join(sorted(packages)) + "\n"
    return files
