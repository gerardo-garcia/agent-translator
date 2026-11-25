"""CrewAI adapter: JSON crew projects, standalone agents, classic YAML config."""
import ast
import json

import pytest

from agent_translator.adapters.crewai import CrewAIAdapter
from agent_translator.adapters.crewai.common import loads_jsonc
from agent_translator.ir import (
    AgentIR,
    AgentSpec,
    Collaboration,
    Memory,
    ModelSpec,
    Resources,
    Tool,
    ToolKind,
)
from agent_translator.report import Severity

from .crewai_helpers import FIX, SOURCES, validate_project

ad = CrewAIAdapter()


def codes(rep):
    return {i.code for i in rep.items}


# ------------------------------------------------------------------------ jsonc
def test_jsonc_comments_trailing_commas_and_urls():
    d = loads_jsonc('''{
      // line comment
      "url": "https://x.example/mcp", /* block */ "a": [1, 2,],
      "s": "not // a comment, and \\" quote",
    }''')
    assert d == {"url": "https://x.example/mcp", "a": [1, 2], "s": 'not // a comment, and " quote'}


# ----------------------------------------------------------------------- detect
@pytest.mark.parametrize("f", SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_detects_every_fixture(f):
    assert ad.detect(f)


def test_does_not_claim_other_formats(tmp_path):
    for name, text in {"flow.yaml": "schema: crewai.flow/v1\nname: x\nmethods: {}\n",
                       "adk.yaml": "name: a\nagent_class: LlmAgent\ninstruction: hi\n",
                       "x.yaml": "role: r\n", "bad.json": "{"}.items():
        f = tmp_path / name
        f.write_text(text)
        assert not ad.detect(f), name
    assert not ad.detect(tmp_path / "missing.jsonc")


# ------------------------------------------------------------------------ parse
def parse(rel):
    ir, rep = ad.parse(FIX / rel)
    return ir, rep, {a.id: a for a in ir.agents}


def test_sequential_crew_becomes_a_workflow_over_its_agents():
    _ir, rep, by = parse("json_sequential/crew.jsonc")
    root = by["support_triage"]
    assert root.collaboration.mode == "sequential" and root.sub_agents == ["classifier", "responder"]
    c = by["classifier"]
    assert c.name == "Support ticket classifier"
    assert c.model.provider == "openai" and c.model.id == "gpt-4o-mini"
    assert c.model.params == {"temperature": 0.1}
    assert c.instructions.startswith("You are Support ticket classifier.")
    assert "Task: Classify the support ticket: {ticket}" in c.instructions
    assert [(t.kind, t.impl_ref) for t in c.tools] == [(ToolKind.function, "custom:lookup_ticket")]
    r = by["responder"]
    assert (r.model.provider, r.model.id) == ("anthropic", "claude-sonnet-4-5")
    assert rep.worst < Severity.lossy


def test_hierarchical_crew_has_a_manager_and_mcp_tools():
    ir, rep, by = parse("json_hierarchical/crew.jsonc")
    m = by["market_brief"]
    assert m.collaboration.mode == "supervisor" and m.sub_agents == ["researcher", "analyst"]
    assert m.model.id == "gpt-4o" and "Task: Produce a market brief" in m.instructions
    assert m.memory.enabled and by["analyst"].memory.enabled
    res = by["researcher"]
    kinds = {t.kind for t in res.tools}
    assert kinds == {ToolKind.builtin, ToolKind.mcp}
    mcp = next(t for t in res.tools if t.kind is ToolKind.mcp)
    assert mcp.tool_names == ["search_news"] or mcp.tool_names == ["get_article"]
    assert ir.resources.mcp_servers == [{"id": "mcp_example_com_news",
                                         "url": "https://mcp.example.com/news",
                                         "protocol": "streamable-http"}]
    assert "delegation-not-translated" in codes(rep)


def test_standalone_agent_yaml_and_json_agree():
    a, ra, _ = parse("standalone_agent/agent.yaml")
    b, _, _ = parse("standalone_agent/agent.json")
    assert a == b and len(a.agents) == 1
    ag = a.agents[0]
    assert ag.id == "standalone_agent" and ag.model.id == "gpt-6-luna"
    assert [t.impl_ref for t in ag.tools] == [f"custom:{n}" for n in (
        "support_hours", "lookup_plan", "support_checklist")]
    assert "skills-dropped" in codes(ra)


def test_classic_config_reads_tasks_and_says_python_is_not_read():
    _ir, rep, by = parse("classic/config/agents.yaml")
    assert by["classic"].sub_agents == ["researcher", "reporting_analyst"]
    assert "Task: Conduct a thorough research about {topic}" in by["researcher"].instructions
    assert "python-config-not-read" in codes(rep)


def test_manager_agent_reference_becomes_the_root(tmp_path):
    (tmp_path / "agents").mkdir()
    for n in ("boss", "w1"):
        (tmp_path / "agents" / f"{n}.jsonc").write_text(json.dumps(
            {"role": n, "goal": "g", "backstory": "b", "llm": "openai/gpt-4o"}))
    (tmp_path / "crew.jsonc").write_text(json.dumps(
        {"name": "c", "agents": ["w1"], "process": "hierarchical", "manager_agent": "boss",
         "tasks": [{"description": "do {x}", "expected_output": "done"}]}))
    ir, _ = ad.parse(tmp_path / "crew.jsonc")
    boss = next(a for a in ir.agents if a.id == "boss")
    assert boss.sub_agents == ["w1"] and boss.collaboration.mode == "supervisor"
    assert "Task: do {x}" in boss.instructions
    out = tmp_path / "out"
    ad.emit(ir, out, {})
    crew = json.loads((out / "crew.jsonc").read_text())
    assert crew["manager_agent"] == "boss" and crew["agents"] == ["w1"]
    assert (out / "agents" / "boss.jsonc").exists()


def test_missing_agent_file_is_an_error(tmp_path):
    (tmp_path / "crew.jsonc").write_text(json.dumps(
        {"name": "c", "agents": ["ghost"], "tasks": [{"description": "d", "expected_output": "e"}]}))
    _, rep = ad.parse(tmp_path / "crew.jsonc")
    assert "agent-file-missing" in codes(rep) and rep.worst is Severity.error


def test_guardrail_and_unknown_task_agent(tmp_path):
    f = tmp_path / "crew.json"
    f.write_text(json.dumps({
        "name": "c", "process": "sequential",
        "agents": {"a": {"role": "r", "goal": "g", "backstory": "b",
                         "guardrail": "Be factual.", "apps": ["gmail"]}},
        "tasks": [{"description": "d", "expected_output": "e", "agent": "nobody"}]}))
    ir, rep = ad.parse(f)
    assert ir.agents[0].guardrails == [{"instruction": "Be factual."}]
    assert {"task-agent-unknown", "apps-dropped"} <= codes(rep)


# ---------------------------------------------------------------- round trips
@pytest.mark.parametrize("n", ["json_sequential", "json_hierarchical"])
def test_crewai_round_trip_is_lossless(n, tmp_path):
    ir, _ = ad.parse(FIX / n / "crew.jsonc")
    ad.emit(ir, tmp_path, {})
    src = FIX / n
    for f in ["crew.jsonc", *(f"agents/{p.name}" for p in (src / "agents").glob("*.jsonc"))]:
        assert loads_jsonc((src / f).read_text()) == json.loads((tmp_path / f).read_text()), f
    validate_project(tmp_path)


def test_edited_instructions_replace_the_original_persona(tmp_path):
    ir, _ = ad.parse(FIX / "json_sequential" / "crew.jsonc")
    ir.agents[1].instructions = "You only answer in haiku."
    ad.emit(ir, tmp_path, {})
    body = json.loads((tmp_path / "agents" / "classifier.jsonc").read_text())
    assert body["backstory"] == "You only answer in haiku." and body["role"] == ir.agents[1].name
    crew = json.loads((tmp_path / "crew.jsonc").read_text())
    assert crew["tasks"][0]["name"] == "classifier_task"          # tasks were rebuilt


# ------------------------------------------------------------------------- emit
def agent(i, **kw):
    kw.setdefault("name", i)
    kw.setdefault("instructions", f"You are {i}.")
    kw.setdefault("model", ModelSpec(provider="anthropic", id="claude-sonnet-4-5"))
    return AgentSpec(id=i, **kw)


def emit(agents, tmp_path, servers=None):
    ir = AgentIR(agents=agents, resources=Resources(mcp_servers=servers or []))
    rep = ad.emit(ir, tmp_path, {})
    validate_project(tmp_path)
    crew = loads_jsonc((tmp_path / "crew.jsonc").read_text())
    return crew, rep, codes(rep)


def test_single_agent_gets_one_synthesized_task(tmp_path):
    crew, rep, cs = emit([agent("solo", description="Helps.")], tmp_path)
    body = json.loads((tmp_path / "agents" / "solo.jsonc").read_text())
    assert body["role"] == "solo" and body["goal"] == "Helps." and body["backstory"] == "You are solo."
    assert body["llm"] == "anthropic/claude-sonnet-4-5"
    assert crew["process"] == "sequential" and crew["inputs"] == {"request": "Describe the task here."}
    assert "task-synthesized" in cs and rep.worst < Severity.lossy


def test_supervisor_with_instructions_is_a_manager_agent(tmp_path):
    boss = agent("boss", sub_agents=["w"], collaboration=Collaboration(mode="supervisor"))
    crew, _, _ = emit([boss, agent("w")], tmp_path)
    assert crew["process"] == "hierarchical" and crew["manager_agent"] == "boss"
    assert crew["agents"] == ["w"] and len(crew["tasks"]) == 1 and "agent" not in crew["tasks"][0]


def test_supervisor_with_tools_falls_back_to_manager_llm(tmp_path):
    t = Tool(id="f", kind=ToolKind.function, name="f")
    boss = agent("boss", sub_agents=["w"], tools=[t], collaboration=Collaboration(mode="supervisor"))
    crew, _rep, cs = emit([boss, agent("w")], tmp_path)
    assert crew["manager_llm"] == "anthropic/claude-sonnet-4-5" and "manager_agent" not in crew
    assert "manager-instructions-dropped" in cs


def test_manager_without_any_model_gets_a_flagged_placeholder(tmp_path):
    boss = agent("boss", sub_agents=["w"], model=ModelSpec(), instructions=None,
                 collaboration=Collaboration(mode="supervisor"))
    crew, _, cs = emit([boss, agent("w", model=ModelSpec())], tmp_path)
    assert crew["manager_llm"] == "openai/gpt-4o-mini" and "manager-model-required" in cs


def test_manager_borrows_a_workers_model(tmp_path):
    boss = agent("boss", sub_agents=["w"], model=ModelSpec(), instructions=None,
                 collaboration=Collaboration(mode="supervisor"))
    crew, _, cs = emit([boss, agent("w")], tmp_path)
    assert crew["manager_llm"] == "anthropic/claude-sonnet-4-5" and "manager-model-borrowed" in cs


def test_sequential_and_parallel_workflows(tmp_path):
    flow = agent("flow", sub_agents=["a", "b"], instructions=None, model=ModelSpec(),
                 collaboration=Collaboration(mode="sequential"))
    crew, _, _ = emit([flow, agent("a"), agent("b")], tmp_path)
    assert crew["process"] == "sequential" and [t["agent"] for t in crew["tasks"]] == ["a", "b"]
    par = flow.model_copy(update={"collaboration": Collaboration(mode="parallel")})
    crew, _, _ = emit([par, agent("a"), agent("b")], tmp_path)
    assert all(t["async_execution"] for t in crew["tasks"])
    loop = flow.model_copy(update={"collaboration": Collaboration(mode="loop")})
    _, _, cs = emit([loop, agent("a"), agent("b")], tmp_path)
    assert "loop-single-pass" in cs


def test_function_tool_stub_compiles_and_is_referenced(tmp_path):
    t = Tool(id="lookup", kind=ToolKind.function, name="Lookup Order", description="Find an order.",
             parameters={"type": "object", "properties": {"order_id": {"type": "string"},
                                                           "limit": {"type": "integer"}},
                         "required": ["order_id"]})
    emit([agent("a", tools=[t])], tmp_path)
    body = json.loads((tmp_path / "agents" / "a.jsonc").read_text())
    assert body["tools"] == ["custom:lookup_order"]
    src = (tmp_path / "tools" / "lookup_order.py").read_text()
    ast.parse(src)
    assert "order_id: str = Field(..., description=" in src
    assert "limit: Optional[int] = Field(None" in src
    assert "def _run(self, order_id: str, limit: Optional[int] = None) -> str:" in src


def test_tool_without_parameters_has_a_valid_stub(tmp_path):
    emit([agent("a", tools=[Tool(id="ping", kind=ToolKind.function)])], tmp_path)
    src = (tmp_path / "tools" / "ping.py").read_text()
    ast.parse(src)
    assert "def _run(self) -> str:" in src


def test_mcp_tools_and_filters_become_mcps(tmp_path):
    servers = [{"id": "docs", "url": "https://x.example/mcp"}]
    tools = [Tool(id="d", kind=ToolKind.mcp, server_ref="docs", tool_names=["a", "b"]),
             Tool(id="g", kind=ToolKind.mcp, server_ref="ghost")]
    _, _, cs = emit([agent("a", tools=tools)], tmp_path, servers)
    body = json.loads((tmp_path / "agents" / "a.jsonc").read_text())
    assert body["mcps"] == ["https://x.example/mcp#a", "https://x.example/mcp#b"]
    assert "mcp-server-undefined" in cs


def test_other_tool_kinds_and_builtin_names(tmp_path):
    tools = [Tool(id="b1", kind=ToolKind.builtin, name="SerperDevTool"),
             Tool(id="b2", kind=ToolKind.builtin, name="google_search"),
             Tool(id="k", kind=ToolKind.knowledge, kb_ref="kb1")]
    _, _, cs = emit([agent("a", tools=tools)], tmp_path)
    body = json.loads((tmp_path / "agents" / "a.jsonc").read_text())
    assert body["tools"] == ["SerperDevTool", "custom:k"]
    assert {"builtin-tool-name", "tool-unsupported", "function-stub-generated"} <= cs


def test_models_params_and_unmapped_ids(tmp_path):
    a = agent("a", model=ModelSpec(id="eu.amazon.nova-lite-v1:0",
                                   params={"temperature": 0.2, "top_k": 4, "max_tokens": 99}))
    b = agent("b", model=ModelSpec(id="my-private-model"), name="b")
    ir = AgentIR(agents=[a, b])
    rep = ad.emit(ir, tmp_path, {})
    one = json.loads((tmp_path / "a" / "agents" / "a.jsonc").read_text())
    assert one["llm"] == {"model": "bedrock/eu.amazon.nova-lite-v1:0", "temperature": 0.2,
                          "max_tokens": 99}
    assert {"param-dropped", "model-unmapped"} <= codes(rep)


def test_memory_guardrails_confirmation_and_remote_agents(tmp_path):
    from agent_translator.ir import RemoteAgent
    a = agent("a", memory=Memory(enabled=True), guardrails=[{"id": "g1"}, {"id": "g2"}],
              remote_agents=[RemoteAgent(id="r", name="r")],
              tools=[Tool(id="t", kind=ToolKind.function, requires_confirmation=True)])
    crew, _, cs = emit([a], tmp_path)
    assert crew["memory"] is True
    assert {"guardrails-dropped", "confirmation-dropped", "remote-agent-unsupported"} <= cs


def test_nested_agents_are_flattened_and_names_sanitized(tmp_path):
    inner = agent("inner agent", sub_agents=["leaf"], collaboration=Collaboration(mode="supervisor"))
    top = agent("top", sub_agents=["inner agent"], collaboration=Collaboration(mode="supervisor"))
    crew, _, cs = emit([top, inner, agent("leaf")], tmp_path)
    assert crew["agents"] == ["inner_agent", "leaf"]
    assert {"nesting-flattened", "name-sanitized"} <= cs


def test_two_roots_get_one_project_each(tmp_path):
    ir = AgentIR(agents=[agent("one"), agent("two")])
    rep = ad.emit(ir, tmp_path, {})
    validate_project(tmp_path)
    assert (tmp_path / "one" / "crew.jsonc").exists() and (tmp_path / "two" / "crew.jsonc").exists()
    assert rep.worst < Severity.error


def test_parse_of_a_non_crewai_file_is_an_error(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n")
    _, rep = ad.parse(f)
    assert rep.worst is Severity.error
