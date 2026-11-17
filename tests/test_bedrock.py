"""Bedrock Agents adapter, tested with official CloudFormation samples (see SOURCES.md)."""
import pytest

from agent_translator.adapters.bedrock import cfn
from agent_translator.ir import ToolKind
from agent_translator.report import Severity

from .bedrock_helpers import FIX, ad, codes, core, fixtures, validate_template
from .kagent_helpers import FIX as KFIX
from .kagent_helpers import ad as kagent

OFFICIAL = [f for f in fixtures() if not f.name.startswith("synthetic")]


def parse(name):
    return ad.parse(FIX / "cfn" / name)


def tool(agent, tid):
    return next(t for t in agent.tools if t.id == tid)


def roundtrip(ir, tmp_path):
    rep = ad.emit(ir, tmp_path)
    assert rep.worst < Severity.error, [i for i in rep.items if i.severity >= Severity.error]
    out = tmp_path / "template.yaml"
    validate_template(out)
    return ad.parse(out)[0], out


@pytest.mark.parametrize("f", fixtures(), ids=lambda f: f.name)
def test_fixture_is_valid_detected_and_parsed(f):
    validate_template(f)
    assert ad.detect(f)
    assert not kagent.detect(f)
    ir, rep = ad.parse(f)
    assert ir.agents and rep.worst < Severity.error


@pytest.mark.parametrize("f", fixtures(), ids=lambda f: f.name)
def test_roundtrip_keeps_the_core_and_reaches_a_fixpoint(f, tmp_path):
    ir, _ = ad.parse(f)
    ir2, _ = roundtrip(ir, tmp_path / "1")
    assert core(ir2) == core(ir)
    ir3, _ = roundtrip(ir2, tmp_path / "2")
    drop = {"tools": {"__all__": {"id"}}}
    assert [a.model_dump(exclude=drop) for a in ir3.agents] == \
           [a.model_dump(exclude=drop) for a in ir2.agents]


def test_kagent_files_are_not_bedrock():
    assert not any(ad.detect(f) for f in (KFIX / "v1alpha3").glob("*.yaml"))


# ------------------------------------------------------------ official samples
def test_model_ids_resolved_from_parameters_and_inference_profiles():
    ir, _ = parse("connected-house.yaml")          # !Sub arn:...inference-profile/${LLM}
    assert ir.agents[0].model.id == "us.amazon.nova-lite-v1:0"
    assert ir.agents[0].model.provider == "amazon"
    ir, _ = parse("competitive-intelligence.yaml")  # !Ref BedrockModelId
    assert ir.agents[0].model.provider == "anthropic"
    assert ir.agents[0].model.id.endswith("claude-sonnet-4-5-20250929-v1:0")


def test_inline_openapi_action_group_becomes_an_openapi_tool():
    ir, rep = parse("connected-house.yaml")
    t = tool(ir.agents[0], "HouseAPI")
    assert t.kind is ToolKind.openapi
    assert "/getWeather" in t.spec["paths"]
    assert t.impl_ref is None                      # Lambda is a !GetAtt on another resource
    assert "schema-external" not in codes(rep)


def test_s3_openapi_schema_is_manual_and_s3_location_survives_as_parameters(tmp_path):
    ir, rep = parse("hr-assistant.yaml")
    assert sum(i.code == "schema-external" for i in rep.items) == 4
    assert all(t.spec is None for t in ir.agents[0].tools)
    _, out = roundtrip(ir, tmp_path)
    tpl = cfn.loader().load(out.read_text())
    assert any("S3ObjectKey" in k for k in tpl["Parameters"]) or any(
        g["ApiSchema"]["S3"]["S3ObjectKey"] == "agent/api-schema/sql-action-group.json"
        for g in tpl["Resources"]["HRAgent"]["Properties"]["ActionGroups"])


def test_prompt_override_is_kept_and_flagged():
    ir, rep = parse("hr-assistant.yaml")
    assert "prompt-override-platform-specific" in codes(rep)
    assert "PromptOverrideConfiguration" in ir.agents[0].extensions["bedrock"]["properties"]


def test_function_schema_becomes_json_schema_tools():
    ir, _ = parse("medical-imaging.yaml")
    a = ir.agents[0]
    assert [t.id for t in a.tools] == ["user-input", "imagingBiomarkerProcessing.compute_imaging_biomarker",
                                       "imagingBiomarkerProcessing.analyze_imaging_biomarker"]
    f = a.tools[1]
    assert f.kind is ToolKind.function and f.name == "compute_imaging_biomarker"
    assert f.parameters == {"type": "object", "required": ["subject_id"], "properties": {
        "subject_id": {"type": "array", "description": "an array of patient subject ID"}}}
    assert a.tools[0].kind is ToolKind.builtin


def test_knowledge_base_reference_outside_the_file_is_flagged(tmp_path):
    ir, rep = parse("api-expert.yaml")
    t = ir.agents[0].tools[0]
    assert t.kind is ToolKind.knowledge and t.kb_ref is None
    assert "knowledge-base-external" in codes(rep)
    out = ad.emit(ir, tmp_path)
    assert "reference-externalised" in codes(out)  # !ImportValue -> parameter


def test_external_collaborators_and_guardrail(tmp_path):
    ir, rep = parse("competitive-intelligence.yaml")
    a = ir.agents[0]
    assert a.collaboration.mode == "supervisor" and a.sub_agents == []
    assert [r.name for r in a.remote_agents] == [
        "Web-Search-Agent", "SEC-10-K-Search-Agent", "USPTO-Search-Agent"]
    assert all(r.instruction for r in a.remote_agents)
    assert sum(i.code == "collaborator-remote" for i in rep.items) == 3
    assert a.guardrails and "guardrail-platform-specific" in codes(rep)
    _, out = roundtrip(ir, tmp_path)
    tpl = cfn.loader().load(out.read_text())
    assert "CompetitiveIntelGuardrail" in tpl["Resources"]       # carried along
    names = [c["CollaboratorName"]
             for c in tpl["Resources"]["CompetitiveIntelligenceAgent"]["Properties"]["AgentCollaborators"]]
    assert names == ["Web-Search-Agent", "SEC-10-K-Search-Agent", "USPTO-Search-Agent"]
    assert {"WebSearchAgentAliasArn", "SEC10kAgentAliasArn"} <= set(tpl["Parameters"])


def test_references_to_resources_not_emitted_become_parameters(tmp_path):
    ir, _ = parse("connected-house.yaml")
    rep = ad.emit(ir, tmp_path)
    tpl = cfn.loader().load((tmp_path / "template.yaml").read_text())
    props = tpl["Resources"]["ConnectedHouseAgent"]["Properties"]
    assert props["AgentResourceRoleArn"] == {"Ref": "ConnectedHouseAgentAgentResourceRoleArn"}
    assert "reference-externalised" in codes(rep)
    # no Ref/GetAtt may point to a logical id that the template lacks
    names = set(tpl["Resources"]) | set(tpl.get("Parameters", {}))
    assert {r for r in cfn.refs_in(tpl["Resources"]) if not cfn.is_pseudo(r)} <= names


# ------------------------------------------------------------------- synthetic
def test_synthetic_multi_agent_is_resolved_inside_the_file(tmp_path):
    ir, rep = ad.parse(FIX / "cfn" / "synthetic-multi-agent.yaml")
    sup = next(a for a in ir.agents if a.id == "Supervisor")
    assert sup.sub_agents == ["Billing", "Orders"]
    assert sup.collaboration.mode == "router"
    assert sup.memory.enabled and sup.memory.ttl_seconds == 14 * 86400
    assert "collaborator-remote" not in codes(rep)
    bill = next(a for a in ir.agents if a.id == "Billing")
    kb = next(t for t in bill.tools if t.kind is ToolKind.knowledge)
    assert kb.kb_ref == "ABCDE12345"
    ids = [t.id for t in bill.tools]
    assert "invoice-tools.refund_invoice" in ids
    assert tool(bill, "invoice-tools.refund_invoice").requires_confirmation is True
    assert tool(bill, "invoice-tools.get_invoice").requires_confirmation is None
    _, out = roundtrip(ir, tmp_path)
    tpl = cfn.loader().load(out.read_text())
    sp = tpl["Resources"]["Supervisor"]["Properties"]
    assert sp["AgentCollaboration"] == "SUPERVISOR_ROUTER"
    assert sp["MemoryConfiguration"]["StorageDays"] == 14
    assert sp["MemoryConfiguration"]["SessionSummaryConfiguration"] == {"MaxRecentSessions": 3}
    assert sp["AgentCollaborators"][0]["RelayConversationHistory"] == "TO_COLLABORATOR"
    assert sp["AgentCollaborators"][0]["AgentDescriptor"]["AliasArn"] == {
        "Fn::GetAtt": ["BillingAlias", "AgentAliasArn"]}
    ords = tpl["Resources"]["Orders"]["Properties"]["ActionGroups"][0]
    assert ords["ActionGroupExecutor"] == {"CustomControl": "RETURN_CONTROL"}
    fns = tpl["Resources"]["Billing"]["Properties"]["ActionGroups"][0]["FunctionSchema"]["Functions"]
    assert fns[1]["RequireConfirmation"] == "ENABLED"


def test_collaboration_cycles_are_rejected(tmp_path):
    ir, _ = ad.parse(FIX / "cfn" / "synthetic-multi-agent.yaml")
    next(a for a in ir.agents if a.id == "Billing").sub_agents.append("Supervisor")
    rep = ad.emit(ir, tmp_path)
    assert "collaboration-cycle" in codes(rep)


# ------------------------------------------------------------- cross platform
def test_kagent_multiagent_to_bedrock(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha2" / "observability-multiagent.yaml")
    sup = next(a for a in ir.agents if a.sub_agents)
    rep = ad.emit(ir, tmp_path)
    out = tmp_path / "template.yaml"
    validate_template(out)
    assert {"model-required", "role-required", "tool-dropped"} <= codes(rep)
    tpl = cfn.loader().load(out.read_text())
    props = tpl["Resources"][cfn.logical_id(sup.id)]["Properties"]
    assert len(props["AgentCollaborators"]) == len(sup.sub_agents)
    assert props["AgentCollaboration"] == "SUPERVISOR"
    for c in props["AgentCollaborators"]:
        alias = c["AgentDescriptor"]["AliasArn"]["Fn::GetAtt"][0]
        assert tpl["Resources"][alias]["Type"] == cfn.ALIAS
        assert len(c["CollaborationInstruction"]) > 10


def test_bedrock_to_kagent_validates_against_crds(tmp_path):
    from .conftest import validate_against_crds
    ir, _ = ad.parse(FIX / "cfn" / "synthetic-multi-agent.yaml")
    rep = kagent.emit(ir, tmp_path)
    assert rep.worst < Severity.error
    validate_against_crds(tmp_path / "kagent.yaml")
    back, _ = kagent.parse(tmp_path / "kagent.yaml")
    assert {a.id for a in back.agents} == {"supervisor", "billing", "orders"}


def test_non_bedrock_model_needs_a_parameter(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha3" / "assistant-inline.yaml")
    rep = ad.emit(ir, tmp_path)
    tpl = cfn.loader().load((tmp_path / "template.yaml").read_text())
    assert "model-required" in codes(rep)
    assert any(k.endswith("FoundationModel") for k in tpl["Parameters"])
