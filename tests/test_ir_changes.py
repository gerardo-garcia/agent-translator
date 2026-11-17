"""IR 0.1 additions: Tool.requires_confirmation and AgentSpec.remote_agents."""
from agent_translator.adapters.bedrock import cfn
from agent_translator.ir import AgentIR, AgentSpec, RemoteAgent, Tool, ToolKind
from agent_translator.report import Severity

from .bedrock_helpers import FIX as BFIX
from .bedrock_helpers import ad as bedrock
from .bedrock_helpers import codes, validate_template
from .kagent_helpers import FIX as KFIX
from .kagent_helpers import ad as kagent


def test_confirmation_true_false_and_unspecified_survive_bedrock(tmp_path):
    fn = {"type": "object", "properties": {}}
    ir = AgentIR(agents=[AgentSpec(
        id="a", name="a", instructions="x" * 50,
        model={"provider": "amazon", "id": "amazon.nova-pro-v1:0"},
        tools=[Tool(id=f"t{i}", kind=ToolKind.function, name=f"f{i}", parameters=fn,
                    impl_ref="arn:aws:lambda:eu-west-1:123456789012:function:x",
                    requires_confirmation=v) for i, v in enumerate((True, False, None))])])
    assert bedrock.emit(ir, tmp_path).worst < Severity.error
    validate_template(tmp_path / "template.yaml")
    tpl = cfn.loader().load((tmp_path / "template.yaml").read_text())
    fns = tpl["Resources"]["A"]["Properties"]["ActionGroups"][0]["FunctionSchema"]["Functions"]
    assert [f.get("RequireConfirmation") for f in fns] == ["ENABLED", "DISABLED", None]
    back, _ = bedrock.parse(tmp_path / "template.yaml")
    assert [t.requires_confirmation for t in back.agents[0].tools] == [True, False, None]


def test_confirmation_on_unsupported_tool_kind_is_reported(tmp_path):
    ir = AgentIR(agents=[AgentSpec(
        id="a", name="a", instructions="x" * 50,
        tools=[Tool(id="kb", kind=ToolKind.knowledge, kb_ref="ABCDE12345",
                    requires_confirmation=True)])])
    assert "confirmation-dropped" in codes(bedrock.emit(ir, tmp_path))


def test_kagent_approval_is_first_class_and_per_tool_list_is_kept_on_same_version(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha2" / "synthetic-assistant.yaml")
    t = ir.agents[0].tools[0]
    assert t.requires_confirmation is True
    kagent.emit(ir, tmp_path, {"api_version": "v1alpha2"})
    assert "close_ticket" in (tmp_path / "kagent.yaml").read_text()
    t.requires_confirmation = False                       # IR value wins over the stored list
    kagent.emit(ir, tmp_path / "off", {"api_version": "v1alpha2"})
    assert "requireApproval" not in (tmp_path / "off" / "kagent.yaml").read_text()


def test_kagent_remote_agent_to_bedrock_asks_for_an_alias(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha2" / "synthetic-assistant.yaml")
    rep = bedrock.emit(ir, tmp_path)
    assert "remote-agent-ref-required" in codes(rep)
    validate_template(tmp_path / "template.yaml")
    tpl = cfn.loader().load((tmp_path / "template.yaml").read_text())
    c = next(iter(tpl["Resources"].values()))["Properties"]["AgentCollaborators"][0]
    assert c["CollaboratorName"] == "billing-agent"
    assert "Ref" in c["AgentDescriptor"]["AliasArn"]


def test_bedrock_remote_agents_to_kagent_are_reported_not_silent(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "competitive-intelligence.yaml")
    for v in ("v1alpha2", "v1alpha3"):
        rep = kagent.emit(ir, tmp_path / v, {"api_version": v})
        assert sum(i.code == "remote-agent-dropped" for i in rep.items) == 3


def test_remote_agent_survives_same_platform_roundtrips(tmp_path):
    for src, ad, opts in (("v1alpha2/synthetic-assistant.yaml", kagent, {"api_version": "v1alpha2"}),):
        ir, _ = ad.parse(KFIX / src)
        ad.emit(ir, tmp_path, opts)
        back, _ = ad.parse(tmp_path / "kagent.yaml")
        assert [r.name for r in back.agents[0].remote_agents] == ["billing-agent"]
    ir, _ = bedrock.parse(BFIX / "cfn" / "competitive-intelligence.yaml")
    bedrock.emit(ir, tmp_path / "b")
    back, _ = bedrock.parse(tmp_path / "b" / "template.yaml")
    assert [r.name for r in back.agents[0].remote_agents] == [r.name for r in ir.agents[0].remote_agents]


def test_remote_agent_ids_and_literal_ref():
    r = RemoteAgent(id="x", name="x", ref="https://a2a.example/agent")
    assert AgentIR(agents=[AgentSpec(id="a", name="a", remote_agents=[r])]).agents[0].remote_agents[0].ref
