"""Azure adapter (Foundry azure.yaml and Agent Framework YAML), tested with official samples."""
import pytest

from agent_translator.report import Severity

from .adk_helpers import FIX as ADK_FIX
from .adk_helpers import ad as adk
from .azure_helpers import (
    FIX,
    FOUNDRY,
    FRAMEWORK,
    ad,
    codes,
    core,
    validate_foundry,
    validate_framework,
)
from .bedrock_helpers import FIX as BFIX
from .bedrock_helpers import ad as bedrock
from .kagent_helpers import FIX as KFIX
from .kagent_helpers import ad as kagent

FOUNDRY_OPT = {"format": "foundry"}
AF_OPT = {"format": "agent-framework"}


def ir_of(rel):
    ir, rep = ad.parse(FIX / rel)
    return ir, rep


@pytest.mark.parametrize("f", FOUNDRY + FRAMEWORK, ids=lambda f: f.name)
def test_fixtures_are_detected_only_by_azure(f):
    assert ad.detect(f)
    for other in (kagent, bedrock, adk):
        assert not other.detect(f)


@pytest.mark.parametrize("f", FOUNDRY, ids=lambda f: f.name)
def test_foundry_samples_validate_and_roundtrip(f, tmp_path):
    assert validate_foundry(f) >= 1
    ir, rep = ad.parse(f)
    assert ir.agents and rep.worst < Severity.error
    assert ad.emit(ir, tmp_path, FOUNDRY_OPT).worst < Severity.error
    assert validate_foundry(tmp_path / "azure.yaml") == len(ir.agents)
    back, _ = ad.parse(tmp_path / "azure.yaml")
    assert core(back) == core(ir)


@pytest.mark.parametrize("f", FRAMEWORK, ids=lambda f: f.name)
def test_framework_samples_validate_and_roundtrip(f, tmp_path):
    validate_framework(f)
    ir, rep = ad.parse(f)
    assert rep.worst < Severity.error or "model-expression" in codes(rep)
    assert ad.emit(ir, tmp_path, AF_OPT).worst < Severity.error
    out = next(tmp_path.glob("*.yaml"))
    validate_framework(out)
    back, _ = ad.parse(out)
    assert core(back) == core(ir)


# ------------------------------------------------------------------ official samples
def test_foundry_tool_kinds():
    ir, _ = ir_of("foundry/02-tools.yaml")
    t = ir.agents[0].tools
    assert [x.name for x in t] == ["get_weather", "convert_currency"]
    assert t[0].parameters["required"] == ["city", "unit"]
    ir, _ = ir_of("foundry/03-mcp-tools.yaml")
    t = ir.agents[0].tools[0]
    assert (t.kind.value, t.requires_confirmation) == ("mcp", False)
    assert ir.resources.mcp_servers[0]["url"] == "https://learn.microsoft.com/api/mcp"
    ir, _ = ir_of("foundry/06-azure-search-rag.yaml")
    t = ir.agents[0].tools[0]
    assert t.kind.value == "knowledge" and t.kb_ref == "${AZURE_SEARCH_INDEX_NAME}"


def test_foundry_toolbox_and_skills_are_reported():
    _ir, rep = ir_of("foundry/04-foundry-toolbox.yaml")
    assert {"mcp-server-unresolved", "toolbox-skills-azure-only"} <= codes(rep)
    _, rep = ir_of("foundry/05-skills.yaml")
    assert "toolbox-skills-azure-only" in codes(rep)


def test_framework_model_expression_and_options():
    ir, rep = ir_of("agent-framework/foundry-MicrosoftLearnAgent.yaml")
    a = ir.agents[0]
    assert a.model.id is None and a.extensions["azure"]["agent-framework"]["modelExpr"] == "=Env.FOUNDRY_MODEL"
    assert a.model.params == {"temperature": 0.9, "top_p": 0.95}
    assert "model-expression" in codes(rep)
    t = a.tools[0]
    assert (t.requires_confirmation, t.tool_names) == (False, ["microsoft_docs_search"])
    ir, _ = ir_of("agent-framework/chatclient-GetWeather.yaml")
    p = ir.agents[0].tools[0].parameters
    assert p["required"] == ["location"] and p["properties"]["unit"]["enum"] == ["celsius", "fahrenheit"]


# ------------------------------------------------------------------------ synthetic
def test_synthetic_foundry_maps_every_tool():
    ir, _ = ir_of("synthetic/multi-agent-azure.yaml")
    triage = next(a for a in ir.agents if a.id == "triage")
    billing = next(a for a in ir.agents if a.id == "billing")
    assert triage.remote_agents[0].ref == "https://billing.example.com/a2a"
    assert triage.collaboration.mode == "supervisor"
    mcp = triage.tools[0]
    assert (mcp.requires_confirmation, mcp.tool_names) == (True, ["search_tickets", "close_ticket"])
    assert [t.kind.value for t in billing.tools] == ["openapi", "knowledge", "builtin"]
    assert billing.tools[0].spec["servers"][0]["url"] == "https://invoices.example.com"
    assert triage.model.params == {"temperature": 0.2, "top_p": 0.9}


def test_synthetic_framework_options_and_schema():
    ir, rep = ir_of("synthetic/agent-framework-tools.yaml")
    a = ir.agents[0]
    assert a.model.params == {"temperature": 0.3, "top_p": 0.8, "max_tokens": 800}
    assert a.extensions["azure"]["agent-framework"]["modelOptions"] == {"seed": 7}
    assert "model-options-azure-only" in codes(rep)


# -------------------------------------------------------------------- cross platform
def test_bedrock_to_foundry_and_framework(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "synthetic-multi-agent.yaml")
    rep = ad.emit(ir, tmp_path / "f", FOUNDRY_OPT)
    assert {"collaboration-unsupported", "model-unmapped", "tool-dropped",
            "confirmation-dropped"} <= codes(rep)
    assert validate_foundry(tmp_path / "f" / "azure.yaml") == 3
    rep = ad.emit(ir, tmp_path / "a", AF_OPT)
    assert "collaboration-unsupported" in codes(rep)
    for f in (tmp_path / "a").glob("*.yaml"):
        validate_framework(f)


def test_kagent_to_foundry(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha3" / "assistant-inline.yaml")
    rep = ad.emit(ir, tmp_path, FOUNDRY_OPT)
    assert rep.worst < Severity.error
    validate_foundry(tmp_path / "azure.yaml")


def test_adk_to_foundry_and_framework(tmp_path):
    ir, _ = adk.parse(ADK_FIX / "synthetic" / "remote_tools" / "root_agent.yaml")
    rep = ad.emit(ir, tmp_path / "f", FOUNDRY_OPT)
    assert {"param-dropped", "tool-mapped", "model-deployment-unverified"} <= codes(rep)
    validate_foundry(tmp_path / "f" / "azure.yaml")
    ad.emit(ir, tmp_path / "a", AF_OPT)
    validate_framework(next((tmp_path / "a").glob("*.yaml")))


def test_foundry_to_other_platforms(tmp_path):
    ir, _ = ir_of("synthetic/multi-agent-azure.yaml")
    assert bedrock.emit(ir, tmp_path / "b").worst < Severity.error
    assert kagent.emit(ir, tmp_path / "k").worst < Severity.error
    assert adk.emit(ir, tmp_path / "d").worst < Severity.error


def test_bad_format_option(tmp_path):
    ir, _ = ir_of("foundry/01-basic.yaml")
    assert ad.emit(ir, tmp_path, {"format": "nope"}).worst is Severity.error
