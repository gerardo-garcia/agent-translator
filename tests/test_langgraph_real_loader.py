"""Load emitted projects with the real langgraph/langchain (skipped when they are not installed).

Run: uv run --all-extras --with langgraph --with langchain --with langchain-mcp-adapters \
       pytest tests/test_langgraph_real_loader.py
The chat model is faked (no provider package, key or network): what is checked is that the
generated code builds a compiled graph with the intended nodes and tools.
"""
import asyncio

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain")
pytest.importorskip("langchain_mcp_adapters")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agent_translator.adapters.langgraph import LangGraphAdapter
from agent_translator.ir import (
    AgentIR,
    AgentSpec,
    Collaboration,
    ModelSpec,
    Resources,
    Tool,
    ToolKind,
)

from .langgraph_helpers import load_module


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def fake(monkeypatch):
    calls = []

    def init(model, **kw):
        calls.append((model, kw))
        return FakeModel(messages=iter([AIMessage(content="done")] * 20))

    monkeypatch.setattr("langchain.chat_models.init_chat_model", init)
    return calls


def a(id, **kw):
    kw.setdefault("instructions", f"You are {id}.")
    kw.setdefault("model", ModelSpec(provider="anthropic", id="claude-sonnet-4-5",
                                     params={"temperature": 0.1}))
    return AgentSpec(id=id, name=id, **kw)


def build(agents, tmp_path, servers=None):
    ir = AgentIR(agents=agents, resources=Resources(mcp_servers=servers or []))
    LangGraphAdapter().emit(ir, tmp_path, {})
    return load_module(tmp_path / "graph.py")


def test_single_agent_runs(tmp_path, fake):
    mod = build([a("solo", tools=[Tool(id="t", kind=ToolKind.function, name="t")])], tmp_path)
    assert fake == [("anthropic:claude-sonnet-4-5", {"temperature": 0.1})]
    out = mod.graph.invoke({"messages": [{"role": "user", "content": "hi"}]})
    assert out["messages"][-1].content == "done"


def test_supervisor_and_sub_agent_tool_run(tmp_path, fake):
    boss = a("boss", sub_agents=["w"], collaboration=Collaboration(mode="supervisor"))
    mod = build([boss, a("w")], tmp_path)
    tool = mod.call_w
    assert tool.name == "call_w"
    assert tool.invoke({"request": "do it"}) == "done"


@pytest.mark.parametrize("mode,edges", [("sequential", {("a", "b")}),
                                        ("parallel", {("__start__", "b")})])
def test_workflows_compile_with_expected_edges(tmp_path, fake, mode, edges):
    flow = a("flow", sub_agents=["a", "b"], collaboration=Collaboration(mode=mode))
    mod = build([flow, a("a"), a("b")], tmp_path)
    g = mod.graph.get_graph()
    assert {"a", "b"} <= set(g.nodes)
    assert edges <= {(e.source, e.target) for e in g.edges}
    out = mod.graph.invoke({"messages": [{"role": "user", "content": "go"}]})
    assert out["messages"][-1].content == "done"


def test_mcp_factory_builds_the_graph(tmp_path, fake):
    servers = [{"id": "docs", "url": "http://localhost:9/mcp", "protocol": "streamable-http"}]
    tool = Tool(id="d", kind=ToolKind.mcp, server_ref="docs")
    mod = build([a("solo", tools=[tool])], tmp_path, servers)

    async def no_tools(server_name=None):
        assert server_name == "docs"
        return []

    mod.mcp_client.get_tools = no_tools
    graph = asyncio.run(mod.make_graph())
    assert "model" in graph.get_graph().nodes
