import pytest

from agent_translator.ir import ToolKind
from agent_translator.report import Severity

from .kagent_helpers import FIX, ad, agent_fixtures, by_id, codes, comparable, fixtures

V = "v1alpha2"
OPTS = {"api_version": V}


@pytest.mark.parametrize("f", fixtures(V), ids=lambda p: p.name)
def test_fixture_conforms_to_crd(f, crd_validate):
    crd_validate(f)


@pytest.mark.parametrize("f", agent_fixtures(V), ids=lambda p: p.name)
def test_detect_and_parse(f):
    ir, rep = ad.parse(f)
    assert ir.agents and rep.worst < Severity.error


def test_official_promql_agent():
    ir, rep = ad.parse(FIX / V / "promql-agent.yaml")
    a = ir.agents[0]
    assert a.id == "promql-agent" and "PromQL Query Generator" in a.instructions
    assert a.description.startswith("GeneratePromQLTool")
    assert not a.memory.enabled and not a.tools
    ext = a.extensions["kagent"][V]
    assert {"declarative", "spec"} >= set(ext) and "a2aConfig" in ext["declarative"]
    assert {"fields-preserved", "runtime-fields-preserved", "modelconfig-external"} <= codes(rep)


def test_official_memory_agent():
    ir, _ = ad.parse(FIX / V / "promql-agent-memory.yaml")
    a = ir.agents[0]
    assert a.memory.enabled and a.memory.ttl_seconds == 15 * 86400
    assert a.extensions["kagent"][V]["memoryModelConfig"] == "embedding-model-config"


def test_official_multiagent_resolves_agent_tool_and_mcp():
    ir, rep = ad.parse(FIX / V / "observability-multiagent.yaml")
    assert {a.id for a in ir.agents} == {"promql-agent", "k8s-agent", "observability-agent"}
    obs = by_id(ir, "observability-agent")
    assert obs.sub_agents == ["promql-agent"] and obs.collaboration.mode == "supervisor"
    mcp = {t.server_ref: t for t in obs.tools}
    assert set(mcp) == {"kagent-tool-server", "kagent-grafana-mcp"}
    assert mcp["kagent-tool-server"].tool_names == ["k8s_get_resources",
                                                    "k8s_get_available_api_resources"]
    assert all(t.kind is ToolKind.mcp for t in obs.tools)
    assert "subagent-unresolved" not in codes(rep)


def test_official_remote_mcp_servers():
    ir, rep = ad.parse(FIX / V / "server-everything-remote-mcpserver.yaml")
    s = ir.resources.mcp_servers[0]
    assert s["url"] == "https://servereverything.dev/mcp" and s["protocol"] == "streamable-http"
    assert s["extensions"]["kagent"][V]["timeout"] == "30s"
    assert not ir.agents and "mcp-server-fields-preserved" in codes(rep)


def test_synthetic_assistant():
    ir, rep = ad.parse(FIX / V / "synthetic-assistant.yaml")
    assert [a.id for a in ir.agents] == ["support-assistant"]     # BYO skipped
    assert "byo-agent" in codes(rep) and "subagent-unresolved" in codes(rep)
    a = ir.agents[0]
    assert (a.model.provider, a.model.id) == ("anthropic", "claude-sonnet-4-5")
    assert a.model.params == {"temperature": 0.3, "max_tokens": 2048}
    assert a.memory.ttl_seconds == 30 * 86400
    t = a.tools[0]
    assert t.tool_names == ["search_tickets", "close_ticket"]
    assert t.extensions["kagent"]["requireApproval"] == ["close_ticket"]
    assert t.requires_confirmation is True          # per-tool list collapsed to a bool in the IR
    assert [(r.id, r.ref) for r in a.remote_agents] == [("billing-agent", None)]
    assert a.collaboration.mode == "supervisor"


@pytest.mark.parametrize("f", agent_fixtures(V), ids=lambda p: p.name)
def test_roundtrip_is_stable(f, tmp_path, crd_validate):
    ir1, _ = ad.parse(f)
    assert ad.emit(ir1, tmp_path, OPTS).worst < Severity.error
    ir2, rep2 = ad.parse(tmp_path / "kagent.yaml")
    assert rep2.worst < Severity.error
    assert comparable(ir1) == comparable(ir2)
    crd_validate(tmp_path / "kagent.yaml")


def test_emit_keeps_unresolved_agent_tool_and_approval_list(tmp_path):
    ir, _ = ad.parse(FIX / V / "synthetic-assistant.yaml")
    ad.emit(ir, tmp_path, OPTS)
    text = (tmp_path / "kagent.yaml").read_text()
    assert "name: billing-agent" in text and "requireApproval" in text
    assert "kind: ModelConfig" in text and "claude-sonnet-4-5" in text
