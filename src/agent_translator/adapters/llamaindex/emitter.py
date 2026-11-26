"""IR -> LlamaIndex project: one self-contained module per root agent, ``requirements.txt``.

Mapping: an LLM agent is ``FunctionAgent``; a supervisor and its sub-agents become an
``AgentWorkflow`` (LlamaIndex's native handoff); sequential/parallel workflows are a tiny
``Pipeline`` helper that runs the sub-agents in order or concurrently. Anything LlamaIndex cannot
express from the IR is reported, never dropped quietly.
"""
from __future__ import annotations

import ast

from ...ir import AgentIR, AgentSpec, Tool, ToolKind
from ...models import lookup
from ...report import Report, Severity
from ..adk.common import py_name
from ..adk.emitter import _stub
from ..bedrock.emitter import BEDROCK_MODEL
from ..langgraph.emitter import lit

PROVIDERS = {  # vendor -> (import line, class, pip package, supported params, env vars)
    "anthropic": ("from llama_index.llms.anthropic import Anthropic", "Anthropic",
                  "llama-index-llms-anthropic", {"temperature", "max_tokens"},
                  ["ANTHROPIC_API_KEY"]),
    "openai": ("from llama_index.llms.openai import OpenAI", "OpenAI",
               "llama-index-llms-openai", {"temperature", "max_tokens"}, ["OPENAI_API_KEY"]),
    "google": ("from llama_index.llms.google_genai import GoogleGenAI", "GoogleGenAI",
               "llama-index-llms-google-genai", {"temperature"}, ["GOOGLE_API_KEY"]),
    "bedrock": ("from llama_index.llms.bedrock_converse import BedrockConverse",
                "BedrockConverse", "llama-index-llms-bedrock-converse",
                {"temperature", "max_tokens"},
                ["AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]),
}
RESERVED = {"build", "run", "Pipeline", "FunctionAgent", "AgentWorkflow", "FunctionTool",
            "McpToolSpec", "BasicMCPClient", "asyncio", "sys", "Optional", "os"}
WORKFLOW = {"sequential", "parallel", "loop"}
PIPELINE = '''class Pipeline:
    """Runs agents one after another (each gets the previous output) or concurrently."""

    def __init__(self, steps, parallel=False):
        self.steps, self.parallel = steps, parallel

    async def run(self, user_msg: str) -> str:
        if self.parallel:
            results = await asyncio.gather(*(s.run(user_msg=user_msg) for s in self.steps))
            return "\\n\\n".join(str(r) for r in results)
        text = user_msg
        for step in self.steps:
            text = str(await step.run(user_msg=text))
        return text
'''


def _vendor(a: AgentSpec, rep: Report, path: str) -> str | None:
    m = a.model
    if not m.id:
        rep.add(Severity.manual, "model-required", f"{path}.model",
                "the source has no model: set llama_index.core.Settings.llm (or pass llm=...)")
        return None
    if BEDROCK_MODEL.match(m.id):
        key = "bedrock"
    else:
        match = lookup(m.id)
        key = (match.vendor if match else m.provider) or ""
    if key not in PROVIDERS:
        rep.add(Severity.manual, "model-unmapped", f"{path}.model",
                f"model {m.id!r} ({m.provider}) has no known LlamaIndex LLM class: set "
                "llama_index.core.Settings.llm yourself")
        return None
    return key


class Module:
    """One root agent and everything it needs, rendered as one Python module."""

    def __init__(self, root: AgentSpec, ir: AgentIR, rep: Report):
        self.root, self.rep = root, rep
        self.by_id = {a.id: a for a in ir.agents}
        self.servers = {s["id"]: s for s in ir.resources.mcp_servers}
        self.names: set[str] = set(RESERVED)
        self.agent_var: dict[str, str] = {}
        self.unit_var: dict[str, str] = {}
        self.defined: set[str] = set()
        self.visiting: set[str] = set()
        self.stubs: list[str] = []
        self.stub_of: dict[str, str] = {}
        self.pre: list[str] = []            # MCP tool fetches (first lines of build())
        self.body: list[str] = []
        self.mcp_vars: dict[str, str] = {}
        self.imports: set[str] = {"import asyncio"}
        self.packages = {"llama-index-core"}
        self.env: set[str] = set()
        self.pipeline = False

    # ------------------------------------------------------------ helpers
    def unique(self, base: str) -> str:
        base = py_name(base)
        n, i = base, 2
        while n in self.names:
            n, i = f"{base}_{i}", i + 1
        self.names.add(n)
        return n

    def kids(self, a: AgentSpec, seen: frozenset = frozenset()) -> list[str]:
        """Sub-agent ids; nested workflows are flattened into their parent."""
        out: list[str] = []
        for s in a.sub_agents:
            c = self.by_id[s]
            if s in seen:
                continue
            if c.collaboration.mode in WORKFLOW and c.sub_agents:
                self.rep.add(Severity.lossy, "workflow-flattened", f"agents[{c.id}]",
                             f"a nested {c.collaboration.mode} workflow cannot be a handoff "
                             "target: its sub-agents are attached to the parent directly")
                out += self.kids(c, seen | {a.id})
            else:
                out.append(s)
        return list(dict.fromkeys(out))

    def _function_stub(self, t: Tool, path: str, why: str) -> str:
        if t.id in self.stub_of:
            return self.stub_of[t.id]
        fn = self.unique(t.name or t.id)
        self.stub_of[t.id] = fn
        self.stubs.append(_stub(fn, t))
        self.rep.add(Severity.manual, "function-stub-generated", path,
                     f"{fn}() in the generated module is a stub: implement it ({why})")
        return fn

    def _mcp(self, t: Tool, path: str) -> str | None:
        ref = t.server_ref or ""
        s = self.servers.get(ref)
        if not s or not s.get("url"):
            self.rep.add(Severity.manual, "mcp-server-undefined", path,
                         f"no URL known for MCP server {ref!r}; tool dropped")
            return None
        if s.get("headers"):
            self.rep.add(Severity.manual, "mcp-headers-dropped", path,
                         "headers (possibly credentials) are not copied; pass `headers=` to "
                         "BasicMCPClient")
        if ref not in self.mcp_vars:
            var = self.unique(f"mcp_{ref}")
            self.mcp_vars[ref] = var
            self.pre.append(f"{var} = await McpToolSpec(BasicMCPClient({s['url']!r}))"
                            ".to_tool_list_async()")
            self.imports.add("from llama_index.tools.mcp import BasicMCPClient, McpToolSpec")
            self.packages.add("llama-index-tools-mcp")
        var = self.mcp_vars[ref]
        if t.tool_names:
            names = ", ".join(repr(n) for n in t.tool_names)
            return f"*[t for t in {var} if t.metadata.name in ({names},)]"
        return f"*{var}"

    def _delegate(self, child: AgentSpec, how: str | None) -> str:
        runner = self.unit(child)
        fn = self.unique(f"call_{child.id}")
        desc = how or child.description or f"Delegate a task to the {child.name} agent."
        self.body += [f"async def {fn}(request: str) -> str:",
                      f"    return str(await {runner}.run(user_msg=request))", "",
                      f"{fn}_tool = FunctionTool.from_defaults(async_fn={fn}, name={fn!r},",
                      f"    description={lit(desc)})", ""]
        self.imports.add("from llama_index.core.tools import FunctionTool")
        return f"{fn}_tool"

    def _tools(self, a: AgentSpec, path: str) -> list[str]:
        exprs: list[str] = []
        for t in a.tools:
            tp = f"{path}.tools[{t.id}]"
            if t.requires_confirmation:
                self.rep.add(Severity.lossy, "confirmation-dropped", tp,
                             "no per-call approval is generated; LlamaIndex agents ask a human "
                             "with InputRequiredEvent/HumanResponseEvent inside the tool")
            if t.kind is ToolKind.mcp:
                v = self._mcp(t, tp)
                if v:
                    exprs.append(v)
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
                if child and child.id not in self.visiting:
                    exprs.append(self._delegate(child, t.description))
                else:
                    self.rep.add(Severity.manual, "tool-unsupported", tp,
                                 "agent tool without a resolvable target agent; dropped")
            else:
                self.rep.add(Severity.manual, "tool-unsupported", tp,
                             f"{t.kind.value} tools have no LlamaIndex equivalent; dropped")
        for r in a.remote_agents:
            exprs.append(self._function_stub(
                Tool(id=f"remote_{r.id}", kind=ToolKind.function, name=f"call_{r.name}",
                     description=r.instruction or r.description or f"Delegate to {r.name}.",
                     parameters={"properties": {"request": {"type": "string"}},
                                 "required": ["request"]}),
                f"{path}.remote_agents[{r.id}]", f"remote agent {r.ref or 'no locator'}"))
        return exprs

    # ------------------------------------------------------------- agents
    def _llm(self, a: AgentSpec, path: str) -> str | None:
        vendor = _vendor(a, self.rep, path)
        if vendor is None:
            return None
        imp, cls, pkg, supported, env = PROVIDERS[vendor]
        self.imports.add(imp)
        self.packages.add(pkg)
        self.env.update(env)
        args = [f"model={a.model.id!r}"]
        if vendor == "bedrock":
            self.imports.add("import os")
            args.append('region_name=os.environ.get("AWS_REGION")')
        for k, v in a.model.params.items():
            if k not in supported or isinstance(v, (dict, list)):
                self.rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                             f"{cls} has no such argument")
            else:
                args.append(f"{k}={v!r}")
        return f"{cls}({', '.join(args)})"

    def define(self, a: AgentSpec, handoff: list[str]) -> str:
        var = self.agent_var.setdefault(a.id, self.unique(a.id))
        if a.id in self.defined:
            return var
        self.defined.add(a.id)
        path = f"agents[{a.id}]"
        self.visiting.add(a.id)
        tools = self._tools(a, path)
        self.visiting.discard(a.id)
        llm = self._llm(a, path)
        self.imports.add("from llama_index.core.agent.workflow import FunctionAgent")
        desc = (a.description or a.name).strip().splitlines()[0]
        lines = [f"{var} = FunctionAgent(", f"    name={var!r},", f"    description={lit(desc)},"]
        if a.instructions:
            lines.append(f"    system_prompt={lit(a.instructions)},")
        else:
            self.rep.add(Severity.warning, "instruction-missing", f"{path}.instructions",
                         "the agent has no system prompt")
        if llm:
            lines.append(f"    llm={llm},")
        if tools:
            lines.append(f"    tools=[{', '.join(tools)}],")
        if handoff:
            lines.append(f"    can_handoff_to={[self.agent_var[h] for h in handoff]!r},")
        lines += [")", ""]
        self.body += lines
        if a.memory.enabled:
            self.rep.add(Severity.warning, "memory-context", f"{path}.memory",
                         "memory is enabled: pass the same llama_index.core.workflow.Context to "
                         "successive run() calls to keep the conversation; the TTL is not "
                         "translated")
        if a.guardrails:
            self.rep.add(Severity.lossy, "guardrails-dropped", f"{path}.guardrails",
                         "LlamaIndex has no guardrail setting; add checks in a tool or workflow")
        return var

    def unit(self, a: AgentSpec) -> str:
        """Variable of an object with ``run(user_msg=...)`` for `a` and everything below it."""
        if a.id in self.unit_var:
            return self.unit_var[a.id]
        path = f"agents[{a.id}]"
        ks = self.kids(a)
        mode = a.collaboration.mode
        if mode in WORKFLOW and ks:
            steps = [self.unit(self.by_id[k]) for k in ks]
            if a.instructions or a.tools or a.model.id:
                self.rep.add(Severity.lossy, "workflow-agent-llm-dropped", path,
                             f"a {mode} workflow has no LLM: its instructions, model and tools "
                             "are dropped")
            if mode == "loop":
                self.rep.add(Severity.lossy, "loop-single-pass", f"{path}.collaboration",
                             "the loop is emitted as one sequential pass (no repeat or "
                             "max_iterations)")
            if mode == "parallel":
                self.rep.add(Severity.lossy, "parallel-outputs-joined", f"{path}.collaboration",
                             "parallel outputs are concatenated with blank lines, not merged")
            var = self.unique(a.id)
            self.pipeline = True
            self.body += [f"{var} = Pipeline([{', '.join(steps)}], parallel={mode == 'parallel'})",
                          ""]
        elif ks:
            if mode in WORKFLOW:
                self.rep.add(Severity.warning, "workflow-without-children", path,
                             f"{mode} agent has no sub-agents: emitted as a plain agent")
            var = self._team(a, ks, path)
        else:
            if mode in WORKFLOW:
                self.rep.add(Severity.warning, "workflow-without-children", path,
                             f"{mode} agent has no sub-agents: emitted as a plain agent")
            var = self.define(a, [])
        self.unit_var[a.id] = var
        return var

    def _team(self, a: AgentSpec, ks: list[str], path: str) -> str:
        """AgentWorkflow: `a` hands off to its sub-agents (and, for a supervisor, they back)."""
        mode = a.collaboration.mode
        self.rep.add(Severity.warning, "supervisor-as-handoff", f"{path}.collaboration",
                     "LlamaIndex has no agent-as-tool supervisor in AgentWorkflow: control is "
                     "handed off to the sub-agent"
                     + (" and handed back when it finishes" if mode != "handoff" else ""))
        team: dict[str, list[str]] = {}          # agent id -> ids it may hand off to

        def walk(x: AgentSpec, parent: AgentSpec | None) -> None:
            if x.id in team:
                return
            team[x.id] = []
            for k in self.kids(x):
                team[x.id].append(k)
                walk(self.by_id[k], x)
            if parent is not None and x.collaboration.mode != "handoff" \
                    and parent.collaboration.mode != "handoff":
                team[x.id].append(parent.id)
        walk(a, None)
        for aid in reversed(list(team)):                 # names first, so handoff lists resolve
            self.agent_var.setdefault(aid, self.unique(aid))
        for aid in reversed(list(team)):
            x = self.by_id[aid]
            self.define(x, list(dict.fromkeys(team[aid])))
        var = self.unique(f"{a.id}_team")
        members = ", ".join(self.agent_var[i] for i in team)
        self.imports.add("from llama_index.core.agent.workflow import AgentWorkflow")
        self.body += [f"{var} = AgentWorkflow(agents=[{members}],",
                      f"    root_agent={self.agent_var[a.id]}.name)", ""]
        return var

    # ------------------------------------------------------------- render
    def render(self, runner: str) -> str:
        head = ['"""Generated by agent-translator. Review before use.', "",
                f"Root agent: {self.root.name}. Install: pip install -r requirements.txt.",
                "Set the API key environment variable of each model provider used.",
                "Stubs below (if any) raise NotImplementedError until you implement them.",
                'Run: python agent.py "your question"', '"""',
                "from __future__ import annotations", ""]
        imps = set(self.imports)
        if any("Optional" in s for s in self.stubs):
            imps.add("from typing import Optional")
        std = sorted(i for i in imps if i.startswith(("import ", "from typing")))
        rest = sorted(i for i in imps if i not in std)
        out = head + std + [""] + rest + [""]
        for s in self.stubs:
            out += ["", "", s.rstrip()]
        if self.pipeline:
            out += ["", "", PIPELINE.rstrip()]
        body = self.pre + self.body + [f"return {runner}"]
        out += ["", "", "async def build():",
                '    """Create the agents (MCP tools are fetched here: servers must be up)."""']
        out += [("    " + ln if ln else "") for ln in body]
        out += ["", "", "async def run(query: str) -> str:",
                "    runner = await build()",
                "    return str(await runner.run(user_msg=query))", "", "",
                'if __name__ == "__main__":', "    import sys", "",
                '    print(asyncio.run(run(" ".join(sys.argv[1:]) or "Hello")))']
        src = "\n".join(out).rstrip() + "\n"
        ast.parse(src)       # never write Python that does not compile
        return src


def env_example(env: set[str]) -> str:
    lines = ["# Export these (or load them with your own tooling) before running.", ""]
    lines += [f"{v}=" for v in sorted(env)]
    return "\n".join(lines) + "\n"


def readme(files: list[str], stubs: bool, mcp: bool, nomodel: bool) -> str:
    out = ["# Generated LlamaIndex project", "",
           "Generated by agent-translator: review before use.", "",
           "```bash",
           "python -m venv .venv && . .venv/bin/activate",
           "pip install -r requirements.txt",
           "set -a; . ./.env.example; set +a     # after filling in the keys",
           f'python {files[0]} "Hello"',
           "```", "",
           "Modules: " + ", ".join(f"`{f}`" for f in files)
           + ". Each exposes `async build()` (the agent or workflow) and `async run(query)`.", ""]
    if stubs:
        out += [("Some tools were generated as stubs that raise `NotImplementedError`: implement "
                 "them (the translation report lists them as `function-stub-generated`)."), ""]
    if mcp:
        out += ["MCP servers are contacted when `build()` runs: they must be reachable.", ""]
    if nomodel:
        out += ["Some agents have no model: set `llama_index.core.Settings.llm` before `build()`.", ""]
    return "\n".join(out)


def emit_project(ir: AgentIR, rep: Report) -> dict[str, str]:
    children = {s for a in ir.agents for s in a.sub_agents}
    children |= {t.agent_ref for a in ir.agents for t in a.tools
                 if t.kind is ToolKind.agent and t.agent_ref}
    roots = [a for a in ir.agents if a.id not in children] or ir.agents[:1]
    files: dict[str, str] = {}
    packages: set[str] = set()
    env: set[str] = set()
    stubs = mcp = False
    for r in roots:
        m = Module(r, ir, rep)
        var = m.unit(r)
        files["agent.py" if len(roots) == 1 else f"{py_name(r.id)}.py"] = m.render(var)
        env |= m.env
        packages |= m.packages
        stubs |= bool(m.stubs)
        mcp |= bool(m.pre)
    files[".env.example"] = env_example(env)
    files["requirements.txt"] = "\n".join(sorted(packages)) + "\n"
    nomodel = any(i.code in ("model-required", "model-unmapped") for i in rep.items)
    files["README.md"] = readme([f for f in files if f.endswith(".py")], stubs, mcp, nomodel)
    return files
