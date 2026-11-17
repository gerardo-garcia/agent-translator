import pytest

from agent_translator.ir import AgentIR, AgentSpec, ModelSpec, Tool, ToolKind
from agent_translator.report import Severity

from .kagent_helpers import FIX, ad, agent_fixtures, by_id, codes, comparable, fixtures

V = "v1alpha3"
FILES = fixtures(V)
OPTS = {"api_version": V}


@pytest.mark.parametrize("f", FILES, ids=lambda p: p.name)
def test_fixture_conforms_to_crd(f, crd_validate):
    crd_validate(f)


@pytest.mark.parametrize("f", FILES, ids=lambda p: p.name)
def test_detect_and_parse(f):
    assert ad.detect(f)
    ir, rep = ad.parse(f)
    assert ir.agents and rep.worst < Severity.error


def test_inline_agent():
    ir, rep = ad.parse(FIX / V / "assistant-inline.yaml")
    a = ir.agents[0]
    assert (a.id, a.instructions) == ("assistant", "You are a helpful assistant.")
    assert "modelconfig-external" in codes(rep)


def test_ref_template_model_and_mcp_resolved():
    ir, _ = ad.parse(FIX / V / "team-a-shared-template.yaml")
    a = ir.agents[0]
    assert a.instructions == "Help the user using the available tools."
    assert (a.model.provider, a.model.id) == ("openai", "example-model")
    assert a.tools[0].kind is ToolKind.mcp and a.tools[0].server_ref == "shared-tools"
    srv = ir.resources.mcp_servers[0]
    assert srv["url"] == "https://tools.example.com/mcp" and srv["protocol"] == "streamable-http"


def test_unresolved_template_is_reported_not_silent():
    _, rep = ad.parse(FIX / V / "code-reviewer.yaml")
    assert {"template-unresolved", "egress-policy"} <= codes(rep)


def test_output_schema_preserved_in_version_extension():
    ir, rep = ad.parse(FIX / V / "data-extractor.yaml")
    ext = ir.agents[0].extensions["kagent"][V]
    assert ext["outputSchema"]["required"] == ["status", "payload"]
    assert "outputSchema-preserved" in codes(rep)


def test_subagent_becomes_ir_sub_agent():
    ir, _ = ad.parse(FIX / V / "reviewer-subagent.yaml")
    dev = by_id(ir, "developer")
    assert dev.sub_agents == ["reviewer"] and dev.collaboration.mode == "supervisor"
    assert {a.id for a in ir.agents} == {"developer", "reviewer"}


@pytest.mark.parametrize("f", agent_fixtures(V), ids=lambda p: p.name)
def test_roundtrip_is_stable(f, tmp_path, crd_validate):
    ir1, _ = ad.parse(f)
    assert ad.emit(ir1, tmp_path, OPTS).worst < Severity.error
    ir2, rep2 = ad.parse(tmp_path / "kagent.yaml")
    assert rep2.worst < Severity.error
    assert comparable(ir1) == comparable(ir2)
    crd_validate(tmp_path / "kagent.yaml")


def test_emit_from_foreign_ir_validates_and_reports(tmp_path, crd_validate):
    ir = AgentIR(
        agents=[AgentSpec(
            id="Support Bot", name="Support Bot", instructions="Line1\nLine2",
            model=ModelSpec(provider="anthropic", id="some-model",
                            params={"temperature": 0.2, "max_tokens": 100}),
            tools=[Tool(id="t1", kind=ToolKind.mcp, server_ref="kb", tool_names=["search"]),
                   Tool(id="t2", kind=ToolKind.function, name="lookup")],
            guardrails=[{"id": "g"}],
        )],
        resources={"mcp_servers": [{"id": "kb", "url": "https://kb.example/mcp"}]},
    )
    rep = ad.emit(ir, tmp_path, OPTS)
    assert {"name-sanitized", "tool-unsupported", "guardrail-dropped", "secret-required",
            "model-unverified"} <= codes(rep)
    crd_validate(tmp_path / "kagent.yaml")
    a = ad.parse(tmp_path / "kagent.yaml")[0].agents[0]
    assert a.id == "support-bot" and a.model.params["temperature"] == 0.2
    assert [t.kind for t in a.tools] == [ToolKind.mcp]


def test_unknown_api_version_is_rejected(tmp_path):
    p = tmp_path / "old.yaml"
    p.write_text("apiVersion: kagent.dev/v1alpha1\nkind: Agent\nmetadata: {name: a}\nspec: {}\n")
    assert ad.detect(p)
    ir, rep = ad.parse(p)
    assert rep.status == "failed" and "unsupported-api-version" in codes(rep) and not ir.agents


def test_bad_emit_option(tmp_path):
    ir = AgentIR(agents=[])
    assert ad.emit(ir, tmp_path, {"api_version": "v9"}).status == "failed"
