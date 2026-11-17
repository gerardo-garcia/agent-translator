"""IR -> ADK Agent Config YAML (+ generated Python for what YAML cannot express)."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ...ir import AgentIR, AgentSpec, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

PY_TYPE = {"string": "str", "number": "float", "integer": "int", "boolean": "bool",
           "array": "list", "object": "dict"}
BEDROCK_ID = re.compile(r"^(?:(?:us|eu|apac|global|us-gov|jp|au|ca)\.)?"
                        r"(?:anthropic|amazon|meta|mistral|cohere|ai21|deepseek|openai|writer|qwen)\.")
GEN_KEYS = {v: k for k, v in c.PARAMS.items()}


class Pkg:
    """One ADK agent folder: YAML files plus the Python modules generated for it."""

    def __init__(self, name: str, ir: AgentIR, rep: Report):
        self.name, self.ir, self.rep = name, ir, rep
        self.by_id = {a.id: a for a in ir.agents}
        self.files: dict[str, str] = {}
        self.tool_code: list[str] = []
        self.model_code: list[str] = []
        self.tool_names: set[str] = set()
        self.servers = {s["id"]: s for s in ir.resources.mcp_servers}
        self.done: dict[str, str] = {}
        self.delegation: dict[str, str] = {}     # child id -> parent's "when to delegate" text
        for a in ir.agents:
            for sid, text in a.collaboration.instructions.items():
                self.delegation.setdefault(sid, text)

    def file_of(self, aid: str) -> str:
        return f"{c.py_name(aid)}.yaml"

    def agent(self, a: AgentSpec, root: bool = False) -> str:
        fname = "root_agent.yaml" if root else self.file_of(a.id)
        if a.id in self.done and not root:
            self.rep.add(Severity.lossy, "agent-shared-child", f"agents[{a.id}]",
                         "ADK allows one parent per agent; the file is referenced twice")
            return self.done[a.id]
        self.done[a.id] = fname
        self.files[fname] = ""        # reserve (cycles)
        self.files[fname] = self._dump(self._config(a))
        return fname

    # ----------------------------------------------------------- one agent
    def _config(self, a: AgentSpec) -> dict[str, Any]:
        path = f"agents[{a.id}]"
        ax = c.adk(a)
        mode = a.collaboration.mode
        cls = c.MODE_CLASS.get(mode)
        if not cls and ax.get("agentClass") and ax["agentClass"] not in c.KNOWN_CLASSES:
            cls = ax["agentClass"]
        cfg: dict[str, Any] = {"agent_class": cls or c.LLM_CLASS, "name": c.py_name(a.name)}
        if cfg["name"] != a.name:
            self.rep.add(Severity.warning, "name-sanitized", f"{path}.name",
                         f"{a.name!r} -> {cfg['name']!r} (ADK names are Python identifiers)")
        desc = a.description
        deleg = self.delegation.get(a.id)
        if deleg and (not desc or deleg not in desc):
            desc = f"{desc} {deleg}" if desc else deleg     # ADK shows this to the parent LLM
        if desc:
            cfg["description"] = desc
        custom = cls not in (None, *c.KNOWN_CLASSES)
        workflow = cls in c.WORKFLOW_CLASSES
        if cls is None or custom:
            self._llm_fields(a, cfg, path, custom)
        elif a.instructions or a.tools:
            self.rep.add(Severity.lossy, "workflow-agent-llm-dropped", path,
                         f"{cls} has no LLM: instructions, model and tools of the IR agent are dropped")
        if workflow and mode == "loop" and a.collaboration.max_iterations:
            cfg["max_iterations"] = a.collaboration.max_iterations

        called = self._called(a)
        subs = []
        for sid in a.sub_agents:
            if sid in called and not workflow and not custom:
                continue
            subs.append({"config_path": self.agent(self.by_id[sid])})
        for r in a.remote_agents:
            rx = r.extensions.get("adk", {})
            if rx.get("code"):
                if "agentTool" not in rx:          # AgentTool ones are added with the tools
                    subs.append({"code": rx["code"]})
                continue
            self.rep.add(Severity.lossy, "remote-agent-dropped", f"{path}.remote_agents[{r.id}]",
                         f"remote agent {r.name!r} ({r.ref or 'no locator'}) cannot be declared "
                         "in YAML; wrap it in Python (e.g. RemoteA2aAgent) and reference it with "
                         "`code:`")
        if subs:
            cfg["sub_agents"] = subs
        for k, v in (ax.get("fields") or {}).items():
            cfg.setdefault(k, v)
        return cfg

    def _called(self, a: AgentSpec) -> set[str]:
        """Sub-agents wired as AgentTool (called) rather than transferred to."""
        ax = c.adk(a)
        if "agentClass" in ax:
            return set(ax.get("agentTools", {}))
        return set(a.sub_agents) if a.collaboration.mode in ("supervisor", "router") else set()

    def _llm_fields(self, a: AgentSpec, cfg: dict, path: str, custom: bool) -> None:
        ax = c.adk(a)
        if not custom:
            model = self._model(a, path)
            if isinstance(model, dict):
                cfg["model_code"] = model
            elif model:
                cfg["model"] = model
            if a.instructions:
                cfg["instruction"] = a.instructions
                if not ax:
                    ph = sorted(set(c.PLACEHOLDER.findall(a.instructions)))
                    if ph:
                        self.rep.add(Severity.warning, "instruction-placeholders", f"{path}.instructions",
                                     f"{ph} will be read as ADK session-state variables")
            else:
                cfg["instruction"] = ""
                self.rep.add(Severity.warning, "instruction-missing", f"{path}.instructions",
                             "LlmAgent requires an instruction; left empty")
            gen = {GEN_KEYS[k]: v for k, v in a.model.params.items() if k in GEN_KEYS}
            gen.update(ax.get("generateContentConfig", {}))
            if gen:
                cfg["generate_content_config"] = gen
            tools = self._tools(a, path)
            called = self._called(a)
            for sid in a.sub_agents:
                if sid in called:
                    tools.append({"name": "AgentTool", "args": {
                        "agent": {"config_path": self.agent(self.by_id[sid])},
                        **ax.get("agentTools", {}).get(sid, {})}})
            for r in a.remote_agents:
                rx = r.extensions.get("adk", {})
                if rx.get("code") and "agentTool" in rx:
                    tools.append({"name": "AgentTool", "args": {"agent": {"code": rx["code"]},
                                                               **rx["agentTool"]}})
            if tools:
                cfg["tools"] = tools

    # --------------------------------------------------------------- model
    def _model(self, a: AgentSpec, path: str) -> Any:
        m, ax = a.model, c.adk(a)
        if not m.id:
            return ax.get("modelCode")
        raw = ax.get("modelString")
        if raw and c.vendor_of(raw)[1] == m.id:
            return raw
        if m.provider == "google" or m.id.lower().startswith("gemini"):
            return m.id
        if m.provider in ("openai", "anthropic") or BEDROCK_ID.match(m.id) or m.provider in (
                "amazon", "meta", "mistral", "cohere"):
            if m.provider == "openai":
                lite = f"openai/{m.id}"
            elif m.provider == "anthropic" and not BEDROCK_ID.match(m.id):
                lite = f"anthropic/{m.id}"
            else:
                lite = f"bedrock/{m.id}"
            var = f"{c.py_name(a.id)}_llm"
            self.model_code.append(f'{var} = LiteLlm(model="{lite}")')
            self.rep.add(Severity.warning, "model-via-litellm", f"{path}.model",
                         f"{m.id!r} is not a Gemini model: emitted as LiteLlm({lite!r}) in "
                         "models.py (needs `pip install litellm` and that provider's credentials; "
                         "the id is not verified)")
            return {"name": f"{self.name}.models.{var}"}
        self.rep.add(Severity.manual, "model-unmapped", f"{path}.model",
                     f"model {m.id!r} ({m.provider}) copied as-is; ADK expects a Gemini id or a "
                     "LiteLlm model")
        return m.id

    # --------------------------------------------------------------- tools
    def _tools(self, a: AgentSpec, path: str) -> list[dict]:
        out: list[dict] = []
        for t in a.tools:
            tp = f"{path}.tools[{t.id}]"
            tx = c.adk(t)
            if t.requires_confirmation and t.kind is not ToolKind.agent:
                self.rep.add(Severity.lossy, "confirmation-dropped", tp,
                             "ADK declares tool confirmation in Python (require_confirmation), "
                             "not in the YAML config")
            if t.kind is ToolKind.function:
                out.append(self._function(t, tp))
            elif t.kind is ToolKind.mcp:
                e = self._mcp(t, tp)
                if e:
                    out.append(e)
            elif t.kind is ToolKind.openapi:
                out.append(self._openapi(t, tp))
            elif t.kind is ToolKind.builtin:
                if t.name in c.BUILTIN_TOOLS or "args" in tx or tx:
                    out.append({"name": t.name or t.id,
                                **({"args": tx["args"]} if tx.get("args") else {})})
                elif t.name == "AMAZON.UserInput":
                    self.rep.add(Severity.info, "tool-implicit", tp,
                                 "ADK agents can ask the user without a dedicated tool")
                else:
                    self.rep.add(Severity.lossy, "tool-dropped", tp,
                                 f"built-in tool {t.name or t.id!r} has no ADK equivalent")
            elif t.kind is ToolKind.knowledge:
                if tx.get("tool"):
                    out.append({"name": tx["tool"], "args": tx.get("args", {})})
                else:
                    self.rep.add(Severity.lossy, "tool-dropped", tp,
                                 f"knowledge base {t.kb_ref or t.id!r}: use VertexAiSearchTool or "
                                 "VertexAiRagRetrieval with a Google Cloud data store (manual)")
        return out

    def _function(self, t: Tool, path: str) -> dict:
        tx = c.adk(t)
        ref = t.impl_ref if c.is_py_path(t.impl_ref) else None
        if ref is None:
            fn = c.py_name(t.name or t.id)
            while fn in self.tool_names:
                fn += "_"
            self.tool_names.add(fn)
            self.tool_code.append(_stub(fn, t))
            ref = f"{self.name}.tools.{fn}"
            self.rep.add(Severity.manual, "function-stub-generated", path,
                         f"{ref}: implement it (the original runs as {t.impl_ref or 'unknown'})")
        if tx.get("longRunning"):
            return {"name": "LongRunningFunctionTool", "args": {"func": ref, **tx.get("args", {})}}
        e: dict[str, Any] = {"name": ref}
        if tx.get("args"):
            e["args"] = tx["args"]
        return e

    def _mcp(self, t: Tool, path: str) -> dict | None:
        s = self.servers.get(t.server_ref or "")
        args = dict(c.adk(t).get("args", {}))
        if s is None and not args:
            self.rep.add(Severity.manual, "mcp-server-unknown", path,
                         f"MCP server {t.server_ref!r} is not described; tool dropped")
            return None
        if s and s.get("url"):
            sse = "sse" in str(s.get("protocol", "")).lower()
            key = "sse_connection_params" if sse else "streamable_http_connection_params"
            other = ("streamable_http_connection_params" if sse else "sse_connection_params")
            args.pop(other, None)
            args.pop("stdio_connection_params", None)
            args.pop("stdio_server_params", None)
            args[key] = {**args.get(key, {}), "url": s["url"]}
        elif not args:
            self.rep.add(Severity.manual, "mcp-server-unknown", path,
                         f"MCP server {t.server_ref!r} has no URL; tool dropped")
            return None
        if t.tool_names:
            args["tool_filter"] = t.tool_names
        return {"name": "McpToolset", "args": args}

    def _openapi(self, t: Tool, path: str) -> dict:
        var = f"openapi_{c.py_name(t.id)}"
        if isinstance(t.spec, dict):
            fname = f"{var}.json"
            self.files[fname] = json.dumps(t.spec, indent=2) + "\n"
            self.tool_code.append(
                f'{var} = OpenAPIToolset(spec_dict=json.loads((_HERE / "{fname}").read_text()))')
            if not t.spec.get("servers"):
                self.rep.add(Severity.manual, "openapi-no-server", path,
                             "the spec has no `servers`: ADK calls the API directly, so add its URL")
            self.rep.add(Severity.manual, "openapi-executor", path,
                         f"calls go straight to the HTTP API; the original executor "
                         f"({t.impl_ref or 'unknown'}) is not used")
        else:
            self.tool_code.append(f"# {var}: OpenAPI spec of {t.id!r} unknown; build the toolset here")
            self.rep.add(Severity.manual, "schema-required", path, f"tool {t.id!r} has no OpenAPI spec")
        return {"name": f"{self.name}.tools.{var}"}

    # ----------------------------------------------------------- rendering
    def _dump(self, cfg: dict) -> str:
        import io

        from ruamel.yaml import YAML
        from ruamel.yaml.scalarstring import LiteralScalarString

        def lit(v: Any) -> Any:
            if isinstance(v, str) and "\n" in v:
                return LiteralScalarString(v)
            if isinstance(v, dict):
                return {k: lit(x) for k, x in v.items()}
            if isinstance(v, list):
                return [lit(x) for x in v]
            return v
        y = YAML()
        y.default_flow_style = False
        y.indent(mapping=2, sequence=4, offset=2)
        y.width = 4096
        buf = io.StringIO()
        y.dump(lit(cfg), buf)
        return buf.getvalue()

    def python_files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.tool_code:
            head = ['"""Generated by agent-translator. Implement the stubs before use."""',
                    "from __future__ import annotations", ""]
            if any("OpenAPIToolset" in x for x in self.tool_code):
                head += ["import json", "from pathlib import Path", ""]
            head += ["from typing import Optional", ""]
            if any("OpenAPIToolset" in x for x in self.tool_code):
                head += [("from google.adk.tools.openapi_tool.openapi_spec_parser.openapi_toolset"
                          " import OpenAPIToolset"), "", "_HERE = Path(__file__).parent", ""]
            out["tools.py"] = "\n".join(head) + "\n\n" + "\n\n".join(self.tool_code) + "\n"
        if self.model_code:
            out["models.py"] = ('"""Generated by agent-translator."""\n'
                                "from google.adk.models.lite_llm import LiteLlm\n\n"
                                + "\n".join(self.model_code) + "\n")
        for name, src in out.items():
            ast.parse(src, filename=name)      # never write Python that does not compile
        if out:
            out["__init__.py"] = ""
        return out


def _stub(fn: str, t: Tool) -> str:
    sch = t.parameters or {}
    props, req = sch.get("properties") or {}, set(sch.get("required") or [])
    names = {p: c.py_name(p) for p in props}
    order = [p for p in props if p in req] + [p for p in props if p not in req]
    sig = []
    for p in order:
        typ = PY_TYPE.get(props[p].get("type"), "str")
        sig.append(f"{names[p]}: {typ}" if p in req else f"{names[p]}: Optional[{typ}] = None")
    doc = [(t.description or f"{t.name or t.id}.").strip().replace('"""', "'''")]
    if props:
        doc += ["", "Args:"] + [
            f"    {names[p]}: {(props[p].get('description') or p).replace(chr(10), ' ')}"
            for p in order]
    body = "\n".join("    " + ln if ln else "" for ln in doc)
    return (f"def {fn}({', '.join(sig)}) -> dict:\n    \"\"\"{body.lstrip()}\n    \"\"\"\n"
            f"    raise NotImplementedError({fn!r})\n")


def emit(ir: AgentIR, out: Path, rep: Report) -> list[Path]:
    children = {s for a in ir.agents for s in a.sub_agents}
    roots = [a for a in ir.agents if a.id not in children] or ir.agents[:1]
    written: list[Path] = []
    for r in roots:
        pkg = Pkg(c.py_name(r.id), ir, rep)
        pkg.agent(r, root=True)
        folder = out / pkg.name
        folder.mkdir(parents=True, exist_ok=True)
        files = {**pkg.files, **pkg.python_files()}
        for name, text in files.items():
            (folder / name).write_text(text)
            written.append(folder / name)
    return written
