"""v1alpha2 <-> v1alpha3 migration through the IR."""
import pytest

from agent_translator.report import Severity

from .kagent_helpers import FIX, ad, agent_fixtures, by_id, codes

CASES = [(src, dst) for src, dst in (("v1alpha2", "v1alpha3"), ("v1alpha3", "v1alpha2"))]


@pytest.mark.parametrize(("src", "dst"), CASES)
def test_all_fixtures_translate_to_valid_target(src, dst, tmp_path, crd_validate):
    for f in agent_fixtures(src):
        out = tmp_path / f.stem
        out.mkdir()
        ir, _ = ad.parse(f)
        rep = ad.emit(ir, out, {"api_version": dst})
        assert rep.worst < Severity.error, f.name
        crd_validate(out / "kagent.yaml")
        ir2, rep2 = ad.parse(out / "kagent.yaml")
        assert rep2.worst < Severity.error, f.name
        # the core survives the migration
        for a in ir.agents:
            if a.id in {x.id for x in ir2.agents}:
                assert by_id(ir2, a.id).instructions == a.instructions, (f.name, a.id)


def test_v2_to_v3_multiagent(tmp_path):
    ir, _ = ad.parse(FIX / "v1alpha2" / "observability-multiagent.yaml")
    rep = ad.emit(ir, tmp_path, {"api_version": "v1alpha3"})
    assert "subagent-semantics" in codes(rep)          # promql-agent becomes an AgentTemplate
    text = (tmp_path / "kagent.yaml").read_text()
    assert "kind: AgentTemplate" in text and "subAgent:" in text
    assert "kagent.dev/v1alpha2" not in text


def test_v2_to_v3_reports_semantic_losses(tmp_path):
    ir, _ = ad.parse(FIX / "v1alpha2" / "synthetic-assistant.yaml")
    rep = ad.emit(ir, tmp_path, {"api_version": "v1alpha3"})
    assert {"require-approval-coarsened", "modelconfig-regenerated", "kagent-fields-dropped",
            "memory-harness"} <= codes(rep)
    # the user's existing Secret is reused, not replaced by an invented one
    assert "secret-reused" in codes(rep) and "secret-required" not in codes(rep)
    assert "apiKeySecret: anthropic-key" in (tmp_path / "kagent.yaml").read_text()


def test_v3_to_v2_subagent_and_dropped_fields(tmp_path):
    ir, _ = ad.parse(FIX / "v1alpha3" / "reviewer-subagent.yaml")
    rep = ad.emit(ir, tmp_path, {"api_version": "v1alpha2"})
    assert "subagent-standalone" in codes(rep)
    text = (tmp_path / "kagent.yaml").read_text()
    assert "type: Agent" in text and text.count("kind: Agent\n") == 2
    ir2, _ = ad.parse(tmp_path / "kagent.yaml")
    assert by_id(ir2, "developer").sub_agents == ["reviewer"]

    ir3, _ = ad.parse(FIX / "v1alpha3" / "data-extractor.yaml")
    rep3 = ad.emit(ir3, tmp_path, {"api_version": "v1alpha2"})
    assert "kagent-fields-dropped" in codes(rep3)       # outputSchema/harness have no v1alpha2 form


def test_group_mapping_for_mcp_servers(tmp_path):
    ir, _ = ad.parse(FIX / "v1alpha2" / "observability-multiagent.yaml")
    ad.emit(ir, tmp_path, {"api_version": "v1alpha3"})
    text = (tmp_path / "kagent.yaml").read_text()
    assert "apiGroup: api.kagent.dev" in text and "apiGroup: kagent.dev\n" not in text
