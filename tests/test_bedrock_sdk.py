import json

from agent_translator.adapters.bedrock import BedrockAdapter

from .bedrock_helpers import FIX, fixtures, validate_sdk_dir

ad = BedrockAdapter()
SDK = {"format": "sdk"}


def emit(src, tmp_path):
    ir, _ = ad.parse(src)
    return ad.emit(ir, tmp_path, SDK)


def test_getagent_bundle_becomes_valid_api_requests(tmp_path):
    rep = emit(FIX / "api" / "getagent-bundle.json", tmp_path)
    assert validate_sdk_dir(tmp_path) == [
        "CreateAgent", "CreateAgentActionGroup", "CreateAgentActionGroup",
        "CreateAgentActionGroup", "AssociateAgentKnowledgeBase", "AssociateAgentCollaborator",
        "PrepareAgent", "CreateAgentAlias"]
    create = json.loads(next(tmp_path.glob("01-*.json")).read_text())
    assert create["agentName"] == "travel-planner" and create["agentCollaboration"] == "SUPERVISOR"
    assert "AutoPrepare" not in json.dumps(create) and "autoPrepare" not in json.dumps(create)
    fn = json.loads(next(tmp_path.glob("03-*.json")).read_text())
    # map keys are user data and keep their case; fields are camelCase
    assert set(fn["functionSchema"]["functions"][0]["parameters"]) == {"flightId", "seats"}
    assert fn["agentVersion"] == "DRAFT" and fn["agentId"].startswith("REPLACE-WITH-")
    assert any(i.code == "sdk-placeholder" for i in rep.items)
    assert "REPLACE-WITH-TravelPlanner-AgentId" in (tmp_path / "README.md").read_text()


def test_every_cloudformation_fixture_has_valid_api_requests(tmp_path_factory):
    for i, f in enumerate(fixtures()):
        out = tmp_path_factory.mktemp(f"sdk{i}")
        rep = emit(f, out)
        ops = validate_sdk_dir(out)
        assert ops.count("CreateAgent") == ops.count("PrepareAgent") == ops.count("CreateAgentAlias")
        assert ops and rep.worst.name != "error", f.name


def test_collaborators_are_created_before_supervisors(tmp_path):
    f = FIX / "cfn" / "synthetic-multi-agent.yaml"
    ir, _ = ad.parse(f)
    sup = next(a for a in ir.agents if a.sub_agents)
    ad.emit(ir, tmp_path, SDK)
    files = sorted(p.name for p in tmp_path.glob("[0-9][0-9]-*-create-agent.json"))
    names = [json.loads((tmp_path / n).read_text())["agentName"] for n in files]
    subs = [next(a for a in ir.agents if a.id == s).name for s in sup.sub_agents]
    assert all(names.index(s) < names.index(sup.name) for s in subs if s in names)
    collab = json.loads(next(tmp_path.glob("*associate-agent-collaborator.json")).read_text())
    assert collab["agentDescriptor"]["aliasArn"].startswith("REPLACE-WITH-")
    assert "AgentAliasArn" in (tmp_path / "README.md").read_text()


def test_parameter_defaults_are_inlined(tmp_path):
    ir, _ = ad.parse(FIX / "cfn" / "api-expert.yaml")
    ad.emit(ir, tmp_path, SDK)
    text = "".join(p.read_text() for p in tmp_path.glob("*.json"))
    assert '"Ref"' not in text and "Fn::" not in text


def test_unknown_format_is_an_error(tmp_path):
    ir, _ = ad.parse(FIX / "api" / "getagent-minimal.json")
    rep = ad.emit(ir, tmp_path, {"format": "terraform"})
    assert rep.worst.name == "error" and not list(tmp_path.iterdir())
