"""Agent Spec adapter, tested with the official examples (see fixtures/agentspec/SOURCES.md)."""
import pytest

from agent_translator.report import Severity

from .adk_helpers import FIX as ADK_FIX
from .adk_helpers import ad as adk
from .agentspec_helpers import AGENTS, FIX, UNSUPPORTED, ad, codes, core, validate
from .azure_helpers import FIX as AZ_FIX
from .azure_helpers import ad as azure
from .bedrock_helpers import FIX as BFIX
from .bedrock_helpers import ad as bedrock
from .kagent_helpers import ad as kagent


def parse(name):
    return ad.parse(FIX / f"{name}.yaml")


@pytest.mark.parametrize("name", AGENTS + UNSUPPORTED)
def test_fixtures_are_detected_only_by_agentspec(name):
    f = FIX / f"{name}.yaml"
    assert ad.detect(f)
    for other in (kagent, bedrock, adk, azure):
        assert not other.detect(f)


@pytest.mark.parametrize("name", AGENTS)
def test_official_examples_roundtrip_and_validate(name, tmp_path):
    validate(FIX / f"{name}.yaml")
    ir, rep = parse(name)
    assert ir.agents and rep.worst < Severity.error
    assert ad.emit(ir, tmp_path).worst < Severity.error
    files = sorted(tmp_path.glob("*.yaml"))
    assert len(files) == 1
    validate(files[0])
    back, _ = ad.parse(files[0])
    assert core(back) == core(ir)


@pytest.mark.parametrize("name", UNSUPPORTED)
def test_unsupported_roots_are_reported(name):
    ir, rep = parse(name)
    assert "component-unsupported" in codes(rep) and not ir.agents


# ------------------------------------------------------------------- official samples
def test_managerworkers_and_swarm_structure():
    ir, _ = parse("howto_managerworkers")
    mgr = next(a for a in ir.agents if a.name == "CustomerServiceManager")
    assert mgr.collaboration.mode == "supervisor"
    assert mgr.sub_agents == ["RefundSpecialist", "SatisfactionSurveyor"]
    assert "refund" in mgr.collaboration.instructions["RefundSpecialist"]
    assert next(a for a in ir.agents if a.name == "RefundSpecialist").tools[0].parameters[
        "required"] == ["order_id", "customer_id"]
    ir, rep = parse("howto_swarm")
    gp = next(a for a in ir.agents if a.name == "GeneralPractitioner")
    assert gp.collaboration.mode == "handoff" and len(gp.sub_agents) == 2
    assert gp.extensions["agentspec"]["root"] == "Swarm"
    assert "swarm-relationships" in codes(rep)


def test_tools_and_toolboxes():
    ir, _ = parse("howto_mcp_agent")
    t = ir.agents[0].tools[0]
    assert t.kind.value == "mcp" and ir.resources.mcp_servers[0] == {
        "id": "Payslip-MCP-ToolBox", "protocol": "sse", "url": "http://localhost:8080/sse"}
    ir, rep = parse("howto_agent_with_remote_tools")
    t = ir.agents[0].tools[0]
    assert t.impl_ref.startswith("http://127.0.0.1:HOST_PORT/benefits/")
    assert "remote-tool-as-function" in codes(rep)
    ir, _ = parse("simple_agent_with_rag_tool")
    assert ir.agents[0].instructions.startswith("You are an expert in {{domain_of_expertise}}")


def test_model_mapping_and_params():
    ir, _ = parse("ext_ops_assistant_tutorial_agent")
    assert (ir.agents[0].model.provider, ir.agents[0].model.id) == ("openai", "model-id")
    ir, _ = parse("howto_agents")
    assert ir.agents[0].model.provider is None


# ---------------------------------------------------------------------- cross platform
def test_bedrock_to_agentspec(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "synthetic-multi-agent.yaml")
    rep = ad.emit(ir, tmp_path)
    assert {"model-via-openai-compatible", "knowledge-as-tool"} <= codes(rep)
    files = list(tmp_path.glob("*.yaml"))
    assert len(files) == 1                      # supervisor + two workers in one ManagerWorkers
    obj = validate(files[0])
    assert type(obj).__name__ == "ManagerWorkers" and len(obj.workers) == 2
    assert "$component_ref" in files[0].read_text()      # the shared LLM config is referenced once


def test_adk_workflow_and_handoff_to_agentspec(tmp_path):
    ir, _ = adk.parse(ADK_FIX / "samples/multi_agent/multi_agent_seq_config/root_agent.yaml")
    rep = ad.emit(ir, tmp_path / "s")
    assert "collaboration-mode" in codes(rep)
    for f in (tmp_path / "s").glob("*.yaml"):
        validate(f)
    ir, _ = adk.parse(ADK_FIX / "samples/multi_agent/multi_agent_basic_config/root_agent.yaml")
    ad.emit(ir, tmp_path / "h")
    objs = [validate(f) for f in (tmp_path / "h").glob("*.yaml")]
    assert [type(o).__name__ for o in objs] == ["Swarm"]


def test_adk_mcp_and_function_tools_to_agentspec(tmp_path):
    ir, _ = adk.parse(ADK_FIX / "synthetic/remote_tools/root_agent.yaml")
    rep = ad.emit(ir, tmp_path)
    assert "tool-dropped" in codes(rep)
    obj = validate(next(tmp_path.glob("*.yaml")))
    assert len(obj.toolboxes) == 2


def test_foundry_openapi_and_a2a_to_agentspec(tmp_path):
    ir, _ = azure.parse(AZ_FIX / "synthetic/multi-agent-azure.yaml")
    ad.emit(ir, tmp_path)
    from .agentspec_helpers import YAML
    mw = next(d for d in (YAML(typ="safe", pure=True).load(f.read_text())
                          for f in tmp_path.glob("*.yaml")) if d["component_type"] == "ManagerWorkers")
    assert [w["component_type"] for w in mw["workers"]] == ["A2AAgent"]
    assert mw["workers"][0]["agent_url"] == "https://billing.example.com/a2a"
    for f in tmp_path.glob("*.yaml"):
        validate(f, drop_a2a_workers="ManagerWorkers" in f.read_text())
    billing = validate(next(f for f in tmp_path.glob("*.yaml") if "component_type: Agent" in f.read_text()[:30]))
    assert billing.tools[0].name == "getInvoice"
    assert billing.tools[0].url == "https://invoices.example.com/invoices/{{id}}"


def test_confirmation_is_kept_as_tool_field(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "synthetic-multi-agent.yaml")
    ad.emit(ir, tmp_path)
    text = next(tmp_path.glob("*.yaml")).read_text()
    assert "requires_confirmation: true" in text and "agentspec_version: 26.1.0" in text


def test_agentspec_to_other_platforms(tmp_path):
    for name in ("howto_managerworkers", "howto_swarm", "howto_mcp_agent"):
        ir, _ = parse(name)
        assert bedrock.emit(ir, tmp_path / name / "b").worst < Severity.error
        assert kagent.emit(ir, tmp_path / name / "k").worst < Severity.error
        assert adk.emit(ir, tmp_path / name / "d").worst < Severity.error
        assert azure.emit(ir, tmp_path / name / "z", {"format": "foundry"}).worst < Severity.error
