"""LangGraph target: emit-only; structure checked on the generated source."""
import ast
import json

from agent_translator.adapters.langgraph import LangGraphAdapter
from agent_translator.ir import (
    AgentIR,
    AgentSpec,
    Collaboration,
    Memory,
    ModelSpec,
    RemoteAgent,
    Resources,
    Tool,
    ToolKind,
)
from agent_translator.report import Severity

from .langgraph_helpers import validate_project

ad = LangGraphAdapter()


def agent(**kw):
    kw.setdefault("id", "helper")
    kw.setdefault("name", "helper")
    kw.setdefault("instructions", "Be helpful.")
    kw.setdefault("model", ModelSpec(provider="anthropic", id="claude-sonnet-4-5"))
    return AgentSpec(**kw)


def emit(agents, tmp_path, servers=None, opts=None):
    ir = AgentIR(agents=agents, resources=Resources(mcp_servers=servers or []))
    rep = ad.emit(ir, tmp_path, opts or {})
    validate_project(tmp_path)
    return rep, {i.code for i in rep.items}, (tmp_path / "graph.py").read_text()


def test_minimal_agent(tmp_path):
    rep, _codes, src = emit([agent()], tmp_path)
    assert "init_chat_model('anthropic:claude-sonnet-4-5')" in src
    assert "system_prompt='Be helpful.'" in src
    assert "graph = helper" in src
    assert rep.worst < Severity.lossy
    assert (tmp_path / "requirements.txt").read_text().split() == [
        "langchain", "langchain-anthropic", "langgraph"]
    assert json.loads((tmp_path / "langgraph.json").read_text())["graphs"] == {
        "helper": "./graph.py:graph"}


def test_parse_is_unsupported(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("")
    assert ad.detect(f) is False
    _, rep = ad.parse(f)
    assert {i.code for i in rep.items} == {"parse-unsupported"}


def test_model_providers_and_params(tmp_path):
    a = agent(model=ModelSpec(id="eu.amazon.nova-lite-v1:0",
                              params={"temperature": 0.2, "top_k": 5, "stop": ["x"]}))
    _rep, codes, src = emit([a], tmp_path)
    assert "init_chat_model('bedrock_converse:eu.amazon.nova-lite-v1:0', temperature=0.2)" in src
    assert "param-dropped" in codes          # top_k is not a Bedrock argument, stop is unknown
    assert "langchain-aws" in (tmp_path / "requirements.txt").read_text()


def test_missing_or_unknown_model_needs_manual_work(tmp_path):
    _rep, codes, src = emit([agent(model=ModelSpec())], tmp_path)
    assert 'os.environ["AGENT_MODEL"]' in src and "model-required" in codes
    _rep, codes, _ = emit([agent(model=ModelSpec(id="my-private-model"))], tmp_path)
    assert "model-unmapped" in codes


def test_function_tool_becomes_a_typed_stub(tmp_path):
    t = Tool(id="lookup", kind=ToolKind.function, description="Look up an order.",
             parameters={"type": "object", "properties": {"order_id": {"type": "string"},
                                                           "limit": {"type": "integer"}},
                         "required": ["order_id"]}, impl_ref="arn:aws:lambda:x")
    rep, codes, src = emit([agent(tools=[t])], tmp_path)
    assert "def lookup(order_id: str, limit: Optional[int] = None) -> dict:" in src
    assert "tools=[lookup]" in src and "function-stub-generated" in codes
    assert rep.worst is Severity.manual


def test_mcp_tools_use_an_async_factory(tmp_path):
    servers = [{"id": "docs", "url": "https://x/mcp", "protocol": "streamable-http"},
               {"id": "feed", "url": "https://x/sse", "protocol": "sse"}]
    tools = [Tool(id="d", kind=ToolKind.mcp, server_ref="docs", tool_names=["search"]),
             Tool(id="f", kind=ToolKind.mcp, server_ref="feed")]
    _, _, src = emit([agent(tools=tools)], tmp_path, servers)
    assert "async def make_graph():" in src
    assert "if t.name in ('search',)" in src
    assert '"transport": "sse"' in src and '"transport": "streamable_http"' in src
    assert json.loads((tmp_path / "langgraph.json").read_text())["graphs"] == {
        "helper": "./graph.py:make_graph"}
    assert "langchain-mcp-adapters" in (tmp_path / "requirements.txt").read_text()


def test_undescribed_mcp_server_is_reported(tmp_path):
    t = Tool(id="d", kind=ToolKind.mcp, server_ref="ghost")
    _, codes, src = emit([agent(tools=[t])], tmp_path)
    assert "mcp-server-undefined" in codes and "tools=" not in src


def test_supervisor_calls_sub_agents_as_tools(tmp_path):
    root = agent(id="boss", name="boss", sub_agents=["w"],
                 collaboration=Collaboration(mode="supervisor", instructions={"w": "Use for X."}))
    worker = agent(id="w", name="w", description="Does X.")
    _, codes, src = emit([root, worker], tmp_path)
    assert "@tool('call_w', description='Use for X.')" in src
    assert src.index("w = create_agent(") < src.index("boss = create_agent(")
    assert "tools=[call_w]" in src and "graph = boss" in src
    assert "handoff-as-tool" not in codes


def test_handoff_is_lossy(tmp_path):
    root = agent(id="boss", name="boss", sub_agents=["w"],
                 collaboration=Collaboration(mode="handoff"))
    rep, codes, _ = emit([root, agent(id="w", name="w")], tmp_path)
    assert "handoff-as-tool" in codes and rep.worst is Severity.lossy


def test_sequential_and_parallel_workflows(tmp_path):
    kids = [agent(id="a", name="a"), agent(id="b", name="b")]
    seq = agent(id="flow", name="flow", sub_agents=["a", "b"], instructions=None,
                collaboration=Collaboration(mode="sequential"))
    _, _, src = emit([seq, *kids], tmp_path)
    for edge in ("add_edge(START, 'a')", "add_edge('a', 'b')", "add_edge('b', END)"):
        assert edge in src
    par = seq.model_copy(update={"collaboration": Collaboration(mode="parallel")})
    _, _, src = emit([par, *kids], tmp_path)
    assert "add_edge(START, 'a')" in src and "add_edge(START, 'b')" in src
    assert "add_edge('a', 'b')" not in src


def test_loop_is_single_pass_and_reported(tmp_path):
    loop = agent(id="l", name="l", sub_agents=["a"], collaboration=Collaboration(mode="loop"))
    _rep, codes, _ = emit([loop, agent(id="a", name="a")], tmp_path)
    assert "loop-single-pass" in codes and "workflow-agent-llm-dropped" in codes


def test_remote_agents_memory_guardrails_and_confirmation(tmp_path):
    a = agent(remote_agents=[RemoteAgent(id="r", name="billing", ref="https://x/a2a")],
              memory=Memory(enabled=True, ttl_seconds=60), guardrails=[{"id": "g"}],
              tools=[Tool(id="t", kind=ToolKind.function, requires_confirmation=True)])
    _rep, codes, src = emit([a], tmp_path)
    assert {"memory-checkpointer", "guardrails-dropped", "confirmation-dropped",
            "function-stub-generated"} <= codes
    assert "def call_billing(request: str)" in src


def test_unsupported_tool_kinds_are_dropped_with_a_report(tmp_path):
    t = Tool(id="b", kind=ToolKind.builtin)
    _, codes, src = emit([agent(tools=[t])], tmp_path)
    assert "tool-unsupported" in codes and "tools=" not in src


def test_two_roots_get_one_module_each(tmp_path):
    ir = AgentIR(agents=[agent(id="one", name="one"), agent(id="two", name="two")])
    rep = ad.emit(ir, tmp_path, {})
    validate_project(tmp_path)
    assert (tmp_path / "one.py").exists() and (tmp_path / "two.py").exists()
    assert not (tmp_path / "graph.py").exists() and rep.worst < Severity.error


def test_generated_python_is_valid_for_odd_text(tmp_path):
    a = agent(instructions='Say """hi""" and use a \\ backslash.\nSecond line.', id="x-y z",
              name="x y")
    _, _, src = emit([a], tmp_path)
    ast.parse(src)


def test_env_example_and_readme_follow_the_models_used(tmp_path):
    t = Tool(id="t", kind=ToolKind.function)
    other = agent(id="o", name="o", model=ModelSpec(id="gpt-4o", provider="openai"))
    ad.emit(AgentIR(agents=[agent(tools=[t]), other]), tmp_path, {})   # two roots, two modules
    env = (tmp_path / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=" in env and "OPENAI_API_KEY=" in env and "GOOGLE_API_KEY" not in env
    readme = (tmp_path / "README.md").read_text()
    assert "`helper`, `o`" in readme and "NotImplementedError" in readme


def test_env_example_asks_for_the_model_when_missing(tmp_path):
    emit([agent(model=ModelSpec())], tmp_path)
    assert "AGENT_MODEL=provider:model-id" in (tmp_path / ".env.example").read_text()
