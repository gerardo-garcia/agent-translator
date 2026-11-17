"""Bedrock API/CLI JSON (GetAgent + bundle) fixtures: shape-checked against botocore's own
service model, then parsed and translated."""
import json

import pytest

from agent_translator.adapters.bedrock import BedrockAdapter
from agent_translator.ir import ToolKind

from .bedrock_helpers import FIX

ad = BedrockAdapter()
API = sorted((FIX / "api").glob("*.json"))


def service_shapes():
    pytest.importorskip("botocore")
    from botocore.session import get_session
    return get_session().get_service_model("bedrock-agent")


def check_keys(model, shape, value, where):
    """Every key of `value` must be a member of the botocore `shape` (recursively)."""
    if shape.type_name == "structure" and isinstance(value, dict):
        for k, v in value.items():
            assert k in shape.members, f"{where}.{k} is not a member of {shape.name}"
            check_keys(model, shape.members[k], v, f"{where}.{k}")
    elif shape.type_name == "list" and isinstance(value, list):
        for i, v in enumerate(value):
            check_keys(model, shape.member, v, f"{where}[{i}]")
    elif shape.type_name == "map" and isinstance(value, dict):
        for k, v in value.items():
            check_keys(model, shape.value, v, f"{where}[{k}]")


SHAPES = {"agent": "GetAgentResponse", "agentActionGroups": "ListAgentActionGroupsResponse",
          "agentKnowledgeBases": "ListAgentKnowledgeBasesResponse",
          "agentCollaborators": "ListAgentCollaboratorsResponse"}
ELEMENTS = {"agentActionGroups": "AgentActionGroup", "agentKnowledgeBases": "AgentKnowledgeBase",
            "agentCollaborators": "AgentCollaborator",
            "actionGroupSummaries": "ActionGroupSummary",
            "agentKnowledgeBaseSummaries": "AgentKnowledgeBaseSummary"}


@pytest.mark.parametrize("f", API, ids=lambda f: f.name)
def test_fixtures_match_the_aws_service_model(f):
    model = service_shapes()
    d = json.loads(f.read_text())
    check_keys(model, model.shape_for("Agent"), d["agent"], "agent")
    for key, shape in ELEMENTS.items():
        for i, x in enumerate(d.get(key, [])):
            check_keys(model, model.shape_for(shape), x, f"{key}[{i}]")


@pytest.mark.parametrize("f", API, ids=lambda f: f.name)
def test_detected_and_parsed(f):
    assert ad.detect(f)
    ir, rep = ad.parse(f)
    assert len(ir.agents) == 1 and rep.worst.name != "error"
    assert any(i.code == "getagent-runtime-fields" for i in rep.items)


def test_minimal_getagent():
    ir, _ = ad.parse(FIX / "api" / "getagent-minimal.json")
    a = ir.agents[0]
    assert a.name == "support-bot" and a.description == "Answers customer questions"
    assert a.model.provider == "anthropic" and a.model.id == "anthropic.claude-3-haiku-20240307-v1:0"
    assert a.instructions.startswith("You are a customer support assistant")
    assert a.memory.enabled and a.memory.ttl_seconds == 30 * 86400
    assert a.guardrails and a.extensions["bedrock"]["properties"]["IdleSessionTTLInSeconds"] == 600


def test_bundle_has_everything_the_separate_calls_return():
    ir, rep = ad.parse(FIX / "api" / "getagent-bundle.json")
    a = ir.agents[0]
    kinds = [t.kind for t in a.tools]
    assert ToolKind.openapi in kinds and ToolKind.knowledge in kinds and ToolKind.builtin in kinds
    fn = next(t for t in a.tools if t.kind == ToolKind.function)
    assert fn.name == "bookFlight" and fn.requires_confirmation is True
    assert fn.parameters["properties"]["flightId"]["type"] == "string"
    assert fn.parameters["required"] == ["flightId"]
    assert a.collaboration.mode == "supervisor"
    assert a.remote_agents[0].ref.endswith("agent-alias/KLMNO11111/TSTALIASID")
    assert "hotel" in a.remote_agents[0].instruction
    assert any(i.code == "collaborator-remote" for i in rep.items)


def test_summaries_are_flagged():
    ir, rep = ad.parse(FIX / "api" / "getagent-summaries.json")
    codes = {i.code for i in rep.items}
    assert {"getagent-summaries", "getagent-failure-reasons", "action-group-incomplete"} <= codes
    assert [t.kind for t in ir.agents[0].tools] == [ToolKind.knowledge]


def test_cloudformation_templates_are_not_taken_for_getagent():
    for f in (FIX / "cfn").glob("*.yaml"):
        assert not __import__("agent_translator.adapters.bedrock.getagent",
                              fromlist=["x"]).is_getagent(
            __import__("agent_translator.adapters.bedrock.cfn", fromlist=["x"])
            .loader().load(f.read_text()))


def test_getagent_to_cloudformation_roundtrip(tmp_path):
    ir, _ = ad.parse(FIX / "api" / "getagent-bundle.json")
    ad.emit(ir, tmp_path)
    back, _ = ad.parse(tmp_path / "template.yaml")
    a, b = ir.agents[0], back.agents[0]
    assert {t.name for t in a.tools if t.name} <= {t.name for t in b.tools}
    assert [t.kind for t in a.tools] == [t.kind for t in b.tools]
    assert b.remote_agents[0].ref == a.remote_agents[0].ref


def test_builtin_signature_params_are_reported_as_dropped(tmp_path):
    d = json.loads((FIX / "api" / "getagent-bundle.json").read_text())
    d["agentActionGroups"][2]["parentActionGroupSignatureParams"] = {"x": "y"}
    f = tmp_path / "g.json"
    f.write_text(json.dumps(d))
    _, rep = ad.parse(f)
    assert any(i.code == "action-group-signature-params" for i in rep.items)
