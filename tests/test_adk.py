"""ADK adapter, tested with the official Agent Config samples (see fixtures/adk/SOURCES.md)."""
import ast

import pytest

from agent_translator.report import Severity

from .adk_helpers import FIX, YAML, ad, agent_files, codes, core, roots, validate_agent_yaml
from .bedrock_helpers import FIX as BFIX
from .bedrock_helpers import ad as bedrock
from .bedrock_helpers import validate_template
from .kagent_helpers import FIX as KFIX
from .kagent_helpers import ad as kagent

ROOTS = roots()
ids = [f"{r.parent.parent.name}/{r.parent.name}" for r in ROOTS]


def sample(rel):
    return FIX / rel / "root_agent.yaml"


def emitted(tmp_path):
    return sorted(tmp_path.rglob("*.yaml"))


@pytest.mark.parametrize("root", ROOTS, ids=ids)
def test_fixtures_match_schema_and_are_detected(root):
    for f in agent_files(root):
        validate_agent_yaml(f)
        assert ad.detect(f)
        assert not kagent.detect(f) and not bedrock.detect(f)


@pytest.mark.parametrize("root", [r for r in ROOTS if "loop_config" not in str(r)
                                  or "multi_agent" in str(r)], ids=lambda r: r.parent.name)
def test_roundtrip_keeps_the_core_and_validates(root, tmp_path):
    ir, rep = ad.parse(root)
    assert ir.agents and rep.worst < Severity.error
    assert ad.emit(ir, tmp_path).worst < Severity.error
    for f in emitted(tmp_path):
        validate_agent_yaml(f)
    back, _ = ad.parse(next(tmp_path.rglob("root_agent.yaml")))
    assert core(back) == core(ir)


def test_python_files_always_compile(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "synthetic-multi-agent.yaml")
    ad.emit(ir, tmp_path)
    py = list(tmp_path.rglob("*.py"))
    assert {p.name for p in py} >= {"tools.py", "models.py", "__init__.py"}
    for p in py:
        ast.parse(p.read_text())


# ------------------------------------------------------------- official samples
def test_workflow_classes_become_collaboration_modes():
    ir, _ = ad.parse(sample("samples/multi_agent/multi_agent_seq_config"))
    seq = next(a for a in ir.agents if a.name == "CodePipelineAgent")
    assert seq.collaboration.mode == "sequential"
    assert seq.sub_agents == ["CodeWriterAgent", "CodeReviewerAgent", "CodeRefactorerAgent"]
    ir, _ = ad.parse(sample("samples/multi_agent/multi_agent_loop_config"))
    loop = next(a for a in ir.agents if a.name == "RefinementLoop")
    assert (loop.collaboration.mode, loop.collaboration.max_iterations) == ("loop", 5)


def test_sub_agents_are_handoff_and_agent_tools_are_supervisor():
    ir, _ = ad.parse(sample("samples/multi_agent/multi_agent_basic_config"))
    assert next(a for a in ir.agents if a.name == "root_agent").collaboration.mode == "handoff"
    ir, _ = ad.parse(sample("samples/tools/tool_agent_tool_config"))
    root = next(a for a in ir.agents if a.name == "research_assistant_agent")
    assert root.collaboration.mode == "supervisor"
    assert root.sub_agents == ["web_search_agent", "summarizer_agent"] and not root.tools


def test_tool_kinds():
    ir, rep = ad.parse(sample("samples/tools/tool_functions_config"))
    t = ir.agents[0].tools
    assert [x.impl_ref for x in t] == ["tool_functions_config.tools.roll_die",
                                       "tool_functions_config.tools.check_prime"]
    assert "function-signature-unknown" in codes(rep)
    ir, _ = ad.parse(sample("samples/tools/tool_builtin_config"))
    assert ir.agents[0].tools[0].kind.value == "builtin"
    ir, _ = ad.parse(sample("samples/hitl/tool_human_in_the_loop_config"))
    assert ir.agents[0].tools[1].extensions["adk"]["longRunning"] is True


def test_generation_config_split_between_params_and_extensions():
    ir, rep = ad.parse(sample("samples/config/core_generate_content_config_config"))
    a = ir.agents[0]
    assert a.model.params == {"temperature": 0.1, "max_tokens": 2000}
    assert "thinking_config" in a.extensions["adk"]["generateContentConfig"]
    assert (a.model.provider, a.model.id) == ("google", "gemini-2.5-flash")
    assert "generation-config-adk-only" in codes(rep)


def test_stdio_mcp_is_flagged_as_not_deployable_remotely():
    ir, rep = ad.parse(sample("samples/mcp/tool_mcp_stdio_notion_config"))
    assert "mcp-stdio" in codes(rep)
    assert ir.resources.mcp_servers[0]["protocol"] == "stdio"


def test_python_defined_pieces_are_reported():
    ir, rep = ad.parse(sample("samples/multi_agent/sub_agents_config"))
    root = next(a for a in ir.agents if a.name == "root_agent")
    assert [r.ref for r in root.remote_agents] == ["python:sub_agents_config.life_agent.agent"]
    assert "agent-code-reference" in codes(rep)
    ir, rep = ad.parse(sample("samples/config/core_custom_agent_config"))
    assert "agent-class-unsupported" in codes(rep)
    assert ir.agents[0].extensions["adk"]["fields"] == {"my_field": "my_field_value"}
    ir, rep = ad.parse(sample("samples/config/core_callback_config"))
    assert "callbacks-adk-only" in codes(rep)


def test_graph_workflow_is_skipped_but_its_agents_are_parsed():
    ir, rep = ad.parse(sample("samples/workflows/loop_config"))
    assert "workflow-graph-unsupported" in codes(rep)
    assert {a.name for a in ir.agents} == {"generate_headline", "evaluate_headline"}
    assert "instruction-placeholders" in codes(rep)


# ------------------------------------------------------------------- synthetic
def test_remote_tools_roundtrip_exactly(tmp_path):
    root = sample("synthetic/remote_tools")
    ir, _ = ad.parse(root)
    a = ir.agents[0]
    assert [t.kind.value for t in a.tools] == ["mcp", "mcp", "knowledge", "function", "builtin"]
    assert a.tools[0].tool_names == ["search_tickets", "close_ticket"]
    assert {s["protocol"] for s in ir.resources.mcp_servers} == {"streamable-http", "sse"}
    assert a.model.params == {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1024}
    ad.emit(ir, tmp_path)
    out = next(tmp_path.rglob("root_agent.yaml"))
    validate_agent_yaml(out)
    d = YAML(typ="safe", pure=True).load(out.read_text())
    o = YAML(typ="safe", pure=True).load(root.read_text())
    assert d == {"agent_class": "LlmAgent", **o} or d == o


def test_parallel_workflow_roundtrip(tmp_path):
    ir, _ = ad.parse(sample("synthetic/parallel_research"))
    root = next(a for a in ir.agents if a.name == "research_team")
    assert root.collaboration.mode == "parallel" and len(root.sub_agents) == 2
    ad.emit(ir, tmp_path)
    for f in emitted(tmp_path):
        validate_agent_yaml(f)


# --------------------------------------------------------------- cross platform
def test_bedrock_to_adk(tmp_path):
    ir, _ = bedrock.parse(BFIX / "cfn" / "synthetic-multi-agent.yaml")
    rep = ad.emit(ir, tmp_path)
    assert {"model-via-litellm", "function-stub-generated", "confirmation-dropped",
            "tool-dropped"} <= codes(rep)
    root = next(tmp_path.rglob("root_agent.yaml"))
    for f in emitted(tmp_path):
        validate_agent_yaml(f)
    back, _ = ad.parse(root)
    sup = next(a for a in back.agents if a.name == "support_supervisor")
    assert sup.collaboration.mode == "supervisor" and len(sup.sub_agents) == 2
    billing = next(a for a in back.agents if a.name == "billing_specialist")
    assert "invoices" in billing.description          # delegation text reaches the sub-agent
    assert "Supervisor.tools.get_invoice" in {t.impl_ref for t in billing.tools}


def test_kagent_to_adk_and_back(tmp_path):
    ir, _ = kagent.parse(KFIX / "v1alpha3" / "assistant-inline.yaml")
    rep = ad.emit(ir, tmp_path)
    assert rep.worst < Severity.error
    for f in emitted(tmp_path):
        validate_agent_yaml(f)


def test_adk_to_bedrock(tmp_path):
    ir, _ = ad.parse(sample("synthetic/remote_tools"))
    rep = bedrock.emit(ir, tmp_path)
    assert {"tool-dropped", "model-required"} <= codes(rep)
    validate_template(tmp_path / "template.yaml")


def test_adk_workflow_to_other_platforms_is_reported(tmp_path):
    ir, _ = ad.parse(sample("samples/multi_agent/multi_agent_seq_config"))
    rep = bedrock.emit(ir, tmp_path / "b")
    assert "collaboration-mode" in codes(rep)
    rep = kagent.emit(ir, tmp_path / "k")
    assert "collaboration-mode" in codes(rep)


def test_adk_to_kagent_validates_against_crds(tmp_path):
    from .conftest import validate_against_crds
    ir, _ = ad.parse(sample("samples/tools/tool_agent_tool_config"))
    assert kagent.emit(ir, tmp_path).worst < Severity.error
    validate_against_crds(tmp_path / "kagent.yaml")
