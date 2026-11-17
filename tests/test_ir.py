import pytest
from pydantic import ValidationError

from agent_translator.ir import AgentIR
from agent_translator.report import Report, Severity


def _agent(id, **kw):
    return {"id": id, "name": id, **kw}


def test_minimal_valid():
    ir = AgentIR.model_validate({"agents": [_agent("a", instructions="hi")]})
    assert ir.irVersion == "0.1"


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        AgentIR.model_validate({"agents": [_agent("a"), _agent("a")]})


def test_unknown_sub_agent_rejected():
    with pytest.raises(ValidationError, match="unknown sub_agent"):
        AgentIR.model_validate({"agents": [_agent("a", sub_agents=["zzz"])]})


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        AgentIR.model_validate({"agents": [_agent("a", bogus=1)]})


def test_report_status():
    r = Report()
    assert r.status == "ok"
    r.add(Severity.warning, "w")
    assert r.status == "ok"
    r.add(Severity.lossy, "l")
    assert r.status == "partial"
    r.add(Severity.error, "e")
    assert r.status == "failed"
