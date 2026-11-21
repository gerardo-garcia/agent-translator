"""AgentCore harness target: emit-only, validated against the official CloudFormation schema."""
import pytest

from agent_translator.adapters.agentcore import AgentCoreAdapter
from agent_translator.adapters.bedrock import cfn
from agent_translator.ir import AgentIR, AgentSpec, Memory, ModelSpec, Resources, Tool, ToolKind
from agent_translator.report import Severity

from .agentcore_helpers import validate_template

ad = AgentCoreAdapter()


def emit(agents, tmp_path, servers=None, opts=None):
    ir = AgentIR(agents=agents, resources=Resources(mcp_servers=servers or []))
    rep = ad.emit(ir, tmp_path, opts or {})
    tpl = cfn.loader().load((tmp_path / "template.yaml").read_text())
    validate_template(tmp_path / "template.yaml")
    return tpl, rep, {i.code for i in rep.items}


def agent(**kw):
    kw.setdefault("id", "helper")
    kw.setdefault("name", "helper")
    kw.setdefault("instructions", "Be helpful and answer briefly and accurately.")
    kw.setdefault("model", ModelSpec(provider="amazon", id="eu.amazon.nova-lite-v1:0"))
    return AgentSpec(**kw)


def props(tpl):
    return next(iter(tpl["Resources"].values()))["Properties"]


def test_minimal_agent(tmp_path):
    tpl, _rep, codes = emit([agent()], tmp_path)
    p = props(tpl)
    assert p["Model"] == {"BedrockModelConfig": {"ModelId": "eu.amazon.nova-lite-v1:0"}}
    assert p["SystemPrompt"] == [{"Text": "Be helpful and answer briefly and accurately."}]
    assert p["ExecutionRoleArn"] == {"Ref": "HelperExecutionRoleArn"}
    assert "HelperExecutionRoleArn" in tpl["Parameters"]
    assert {"role-required", "memory-disabled", "written"} <= codes


def test_memory_is_disabled_unless_requested(tmp_path):
    tpl, _, _ = emit([agent()], tmp_path)
    assert props(tpl)["Memory"] == {"Disabled": {}}
    tpl, _, _ = emit([agent(memory=Memory(enabled=True, ttl_seconds=10 * 86400))], tmp_path)
    assert props(tpl)["Memory"] == {"ManagedMemoryConfiguration": {"EventExpiryDuration": 10}}


def test_memory_ttl_is_clamped(tmp_path):
    tpl, _, codes = emit([agent(memory=Memory(enabled=True, ttl_seconds=3600))], tmp_path)
    assert props(tpl)["Memory"]["ManagedMemoryConfiguration"]["EventExpiryDuration"] == 3
    assert "memory-ttl-clamped" in codes


def test_model_params(tmp_path):
    m = ModelSpec(provider="amazon", id="eu.amazon.nova-lite-v1:0",
                  params={"temperature": 0.2, "top_p": 0.9, "max_tokens": 512, "top_k": 40,
                          "seed": 1})
    tpl, rep, _codes = emit([agent(model=m)], tmp_path)
    assert props(tpl)["Model"]["BedrockModelConfig"] == {
        "ModelId": "eu.amazon.nova-lite-v1:0", "Temperature": 0.2, "TopP": 0.9, "MaxTokens": 512}
    dropped = {i.path for i in rep.items if i.code == "param-dropped"}
    assert dropped == {"agents[helper].model.params.top_k", "agents[helper].model.params.seed"}


def test_foreign_claude_id_is_rehosted(tmp_path):
    m = ModelSpec(provider="anthropic", id="claude-sonnet-4-5")
    tpl, _, codes = emit([agent(model=m)], tmp_path)
    mid = props(tpl)["Model"]["BedrockModelConfig"]["ModelId"]
    assert mid.startswith(("anthropic.", "eu.anthropic.", "us.anthropic.", "global.anthropic."))
    assert "model-rehosted" in codes


def test_openai_model_needs_a_credential_provider(tmp_path):
    m = ModelSpec(provider="openai", id="gpt-4o")
    tpl, _, codes = emit([agent(model=m)], tmp_path)
    cfg = props(tpl)["Model"]["OpenAiModelConfig"]
    assert cfg["ModelId"] == "gpt-4o" and cfg["ApiKeyArn"] == {"Ref": "HelperApiKeyArn"}
    assert "secret-required" in codes


def test_unknown_model_becomes_a_parameter(tmp_path):
    tpl, _, codes = emit([agent(model=ModelSpec())], tmp_path)
    assert props(tpl)["Model"]["BedrockModelConfig"]["ModelId"] == {"Ref": "HelperModelId"}
    assert "model-required" in codes


def test_mcp_tool_becomes_remote_mcp(tmp_path):
    servers = [{"id": "docs", "url": "https://mcp.example.com/mcp"}]
    tools = [Tool(id="a", kind=ToolKind.mcp, server_ref="docs"),
             Tool(id="b", kind=ToolKind.mcp, server_ref="docs", tool_names=["search"])]
    tpl, _, codes = emit([agent(tools=tools)], tmp_path, servers)
    assert props(tpl)["Tools"] == [{"Type": "remote_mcp", "Name": "docs",
                                    "Config": {"RemoteMcp": {"Url": "https://mcp.example.com/mcp"}}}]
    assert "tool-filter-dropped" in codes


def test_mcp_without_url_is_manual(tmp_path):
    tpl, _, codes = emit([agent(tools=[Tool(id="a", kind=ToolKind.mcp, server_ref="x")])], tmp_path)
    assert "Tools" not in props(tpl) and "mcp-server-undefined" in codes


def test_function_tool_becomes_inline_function(tmp_path):
    t = Tool(id="weather", kind=ToolKind.function, description="Get the weather",
             parameters={"type": "object", "properties": {"city": {"type": "string"}}},
             impl_ref="arn:aws:lambda:eu-west-1:123456789012:function:w")
    tpl, _, codes = emit([agent(tools=[t])], tmp_path)
    tool = props(tpl)["Tools"][0]
    assert tool["Type"] == "inline_function"
    assert tool["Config"]["InlineFunction"]["InputSchema"]["properties"] == {"city": {"type": "string"}}
    assert "function-impl-dropped" in codes


@pytest.mark.parametrize("kind", [ToolKind.openapi, ToolKind.knowledge, ToolKind.builtin, ToolKind.agent])
def test_unsupported_tools_are_reported(kind, tmp_path):
    tpl, rep, codes = emit([agent(tools=[Tool(id="t", kind=kind)])], tmp_path)
    assert "Tools" not in props(tpl) and "tool-unsupported" in codes
    assert rep.worst >= Severity.manual


def test_multi_agent_is_split_and_reported(tmp_path):
    parent = agent(id="boss", name="boss", sub_agents=["worker"])
    tpl, _rep, codes = emit([parent, agent(id="worker", name="worker")], tmp_path)
    assert set(tpl["Resources"]) == {"BossHarness", "WorkerHarness"}
    assert "collaboration-dropped" in codes


def test_name_is_sanitized(tmp_path):
    tpl, _, codes = emit([agent(name="my agent-v2.1")], tmp_path)
    assert props(tpl)["HarnessName"] == "my_agent_v2_1" and "name-sanitized" in codes
    tpl, _, _ = emit([agent(name="9lives")], tmp_path)
    assert props(tpl)["HarnessName"] == "a_9lives"


def test_description_and_guardrails_are_reported(tmp_path):
    a = agent(description="d", guardrails=[{"id": "g"}])
    _, _, codes = emit([a], tmp_path)
    assert {"description-dropped", "guardrails-dropped"} <= codes


def test_emit_only(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n")
    assert ad.detect(f) is False
    ir, rep = ad.parse(f)
    assert not ir.agents and rep.worst is Severity.error


def test_no_agents(tmp_path):
    rep = ad.emit(AgentIR(agents=[]), tmp_path, {})
    assert rep.worst is Severity.error
