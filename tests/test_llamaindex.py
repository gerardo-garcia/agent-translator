"""LlamaIndex target (emit-only)."""
import ast

from agent_translator.adapters.llamaindex import LlamaIndexAdapter
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

from .llamaindex_helpers import validate_project

ad = LlamaIndexAdapter()


def agent(i, **kw):
    kw.setdefault("name", i)
    kw.setdefault("instructions", f"You are {i}.")
    kw.setdefault("model", ModelSpec(provider="anthropic", id="claude-sonnet-4-5"))
    return AgentSpec(id=i, **kw)


def emit(agents, tmp_path, servers=None):
    ir = AgentIR(agents=agents, resources=Resources(mcp_servers=servers or []))
    rep = ad.emit(ir, tmp_path, {})
    validate_project(tmp_path)
    return (tmp_path / "agent.py").read_text(), rep, {i.code for i in rep.items}


def test_not_a_source_format(tmp_path):
    assert not ad.detect(tmp_path / "agent.py")
    _, rep = ad.parse(tmp_path / "agent.py")
    assert rep.worst is Severity.error


def test_single_agent(tmp_path):
    src, rep, _ = emit([agent("solo", description="Helps.",
                              model=ModelSpec(provider="anthropic", id="claude-sonnet-4-5",
                                              params={"temperature": 0.2, "top_p": 0.9}))],
                       tmp_path)
    assert "FunctionAgent(" in src and "AgentWorkflow" not in src
    assert "llm=Anthropic(model='claude-sonnet-4-5', temperature=0.2)" in src
    assert "param-dropped" in {i.code for i in rep.items}
    assert "llama-index-llms-anthropic" in (tmp_path / "requirements.txt").read_text()
    assert "ANTHROPIC_API_KEY" in (tmp_path / ".env.example").read_text()


def test_supervisor_is_an_agent_workflow_with_handoff(tmp_path):
    boss = agent("boss", sub_agents=["w"], collaboration=Collaboration(mode="supervisor"))
    src, _, cs = emit([boss, agent("w")], tmp_path)
    assert "can_handoff_to=['boss']" in src and "can_handoff_to=['w']" in src
    assert "AgentWorkflow(agents=[boss, w]," in src and "root_agent=boss.name)" in src
    assert "supervisor-as-handoff" in cs


def test_sequential_and_parallel_pipelines(tmp_path):
    flow = agent("flow", sub_agents=["a", "b"], model=ModelSpec(),
                 collaboration=Collaboration(mode="sequential"))
    src, _, cs = emit([flow, agent("a"), agent("b")], tmp_path)
    assert "Pipeline([a, b], parallel=False)" in src and "workflow-agent-llm-dropped" in cs
    par = flow.model_copy(update={"collaboration": Collaboration(mode="parallel")})
    src, _, cs = emit([par, agent("a"), agent("b")], tmp_path)
    assert "parallel=True" in src and "parallel-outputs-joined" in cs
    loop = flow.model_copy(update={"collaboration": Collaboration(mode="loop")})
    _, _, cs = emit([loop, agent("a"), agent("b")], tmp_path)
    assert "loop-single-pass" in cs


def test_nested_workflow_is_flattened_into_its_parent(tmp_path):
    inner = agent("inner", sub_agents=["x", "y"], collaboration=Collaboration(mode="sequential"))
    boss = agent("boss", sub_agents=["inner"], collaboration=Collaboration(mode="supervisor"))
    src, _, cs = emit([boss, inner, agent("x"), agent("y")], tmp_path)
    assert "can_handoff_to=['x', 'y']" in src and "workflow-flattened" in cs


def test_function_tool_stub_and_mcp_filter(tmp_path):
    servers = [{"id": "docs", "url": "https://x.example/mcp", "headers": {"A": "b"}}]
    tools = [Tool(id="f", kind=ToolKind.function, name="lookup", description="Find."),
             Tool(id="m", kind=ToolKind.mcp, server_ref="docs", tool_names=["a"]),
             Tool(id="g", kind=ToolKind.mcp, server_ref="ghost"),
             Tool(id="k", kind=ToolKind.knowledge, kb_ref="kb"),
             Tool(id="b", kind=ToolKind.builtin, name="google_search", requires_confirmation=True)]
    src, _, cs = emit([agent("a", tools=tools)], tmp_path, servers)
    assert "def lookup(" in src and "raise NotImplementedError" in src
    assert "await McpToolSpec(BasicMCPClient('https://x.example/mcp')).to_tool_list_async()" in src
    assert "t.metadata.name in ('a',)" in src
    assert {"function-stub-generated", "mcp-headers-dropped", "mcp-server-undefined",
            "tool-unsupported", "confirmation-dropped"} <= cs
    assert "llama-index-tools-mcp" in (tmp_path / "requirements.txt").read_text()


def test_agent_tool_delegates_to_the_other_agent(tmp_path):
    boss = agent("boss", tools=[Tool(id="t", kind=ToolKind.agent, agent_ref="w")])
    src, _, _ = emit([boss, agent("w")], tmp_path)
    assert "async def call_w(request: str)" in src and "FunctionTool.from_defaults" in src


def test_models_providers_and_missing_model(tmp_path):
    a = agent("a", model=ModelSpec(id="eu.amazon.nova-lite-v1:0"))
    b = agent("b", model=ModelSpec(id="my-private-model"))
    c = agent("c", model=ModelSpec())
    ir = AgentIR(agents=[a, b, c])
    rep = ad.emit(ir, tmp_path, {})
    cs = {i.code for i in rep.items}
    assert {"model-unmapped", "model-required"} <= cs
    src = (tmp_path / "a.py").read_text()
    assert 'BedrockConverse(model=\'eu.amazon.nova-lite-v1:0\', region_name=os.environ.get("AWS_REGION"))' in src
    assert "llm=" not in (tmp_path / "c.py").read_text()
    assert "Settings.llm" in (tmp_path / "README.md").read_text()


def test_memory_guardrails_and_remote_agents_are_reported(tmp_path):
    from agent_translator.ir import RemoteAgent
    a = agent("a", memory=Memory(enabled=True), guardrails=[{"id": "g"}],
              remote_agents=[RemoteAgent(id="r", name="r")])
    src, _, cs = emit([a], tmp_path)
    ast.parse(src)
    assert {"memory-context", "guardrails-dropped", "function-stub-generated"} <= cs


def test_two_roots_two_modules(tmp_path):
    ad.emit(AgentIR(agents=[agent("one"), agent("two")]), tmp_path, {})
    validate_project(tmp_path)
    assert (tmp_path / "one.py").exists() and (tmp_path / "two.py").exists()
