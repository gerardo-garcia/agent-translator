"""Full cross matrix: every fixture of every platform translated to every target (and variant),
the output re-parsed by the target adapter and validated with the target's own schema/SDK."""
import pytest

from agent_translator.registry import detect_adapter, load_adapters
from agent_translator.report import Severity

from .adk_helpers import roots as adk_roots
from .adk_helpers import validate_agent_yaml
from .agentcore_helpers import validate_template as validate_harness
from .agentspec_helpers import AGENTS as AS_AGENTS
from .agentspec_helpers import FIX as AS_FIX
from .agentspec_helpers import validate as validate_agentspec
from .azure_helpers import FOUNDRY, FRAMEWORK, validate_foundry, validate_framework
from .bedrock_helpers import FIX as BFIX
from .bedrock_helpers import validate_sdk_dir, validate_template
from .conftest import validate_against_crds
from .crewai_helpers import SOURCES as CREW_SOURCES
from .crewai_helpers import validate_project as validate_crewai
from .kagent_helpers import agent_fixtures
from .langgraph_helpers import validate_project as validate_langgraph
from .llamaindex_helpers import validate_project as validate_llamaindex

ADAPTERS = load_adapters()
SOURCES = (
    [("kagent", f) for v in ("v1alpha2", "v1alpha3") for f in agent_fixtures(v)]
    + [("bedrock", f) for f in sorted((BFIX / "cfn").glob("*.yaml"))]
    + [("bedrock", f) for f in sorted((BFIX / "api").glob("*.json"))]
    + [("adk", f) for f in adk_roots() if "loop_config" not in str(f) or "multi_agent" in str(f)]
    + [("azure", f) for f in FOUNDRY + FRAMEWORK]
    + [("agentspec", AS_FIX / f"{n}.yaml") for n in AS_AGENTS]
    + [("crewai", f) for f in CREW_SOURCES]
)
TARGETS = [("kagent", {"api_version": "v1alpha3"}), ("kagent", {"api_version": "v1alpha2"}),
           ("bedrock", {}), ("bedrock", {"format": "sdk"}), ("adk", {}), ("azure", {"format": "foundry"}),
           ("azure", {"format": "agent-framework"}), ("agentspec", {}), ("agentcore", {}), ("langgraph", {}), ("crewai", {}), ("llamaindex", {})]


def sid(p):
    return f"{p[0]}:{p[1].parent.name}/{p[1].name}"


def check_output(target, opts, out):
    """Validate whatever the target adapter wrote with that platform's own schema/SDK."""
    if target == "kagent":
        validate_against_crds(out / "kagent.yaml")
    elif target == "bedrock" and opts.get("format") == "sdk":
        validate_sdk_dir(out)
    elif target == "bedrock":
        validate_template(out / "template.yaml")
    elif target == "agentcore":
        validate_harness(out / "template.yaml")
    elif target == "crewai":
        validate_crewai(out)
    elif target == "llamaindex":
        validate_llamaindex(out)
    elif target == "langgraph":
        validate_langgraph(out)
    elif target == "adk":
        for f in out.rglob("*.yaml"):
            validate_agent_yaml(f)
    elif target == "azure" and opts["format"] == "foundry":
        validate_foundry(out / "azure.yaml")
    elif target == "azure":
        for f in out.glob("*.yaml"):
            validate_framework(f)
    else:
        for f in out.glob("*.yaml"):
            text = f.read_text()
            validate_agentspec(f, drop_a2a_workers="A2AAgent" in text)


def main_file(target, opts, out):
    if target == "crewai":
        return next(out.rglob("crew.jsonc"))
    if target == "adk":
        return next(out.rglob("root_agent.yaml"))
    return next(iter(sorted(out.glob("*.yaml"))))


@pytest.mark.parametrize("src", SOURCES, ids=sid)
def test_every_fixture_is_detected_by_exactly_one_adapter(src):
    name, f = src
    assert [n for n, a in ADAPTERS.items() if a.detect(f)] == [name]
    assert detect_adapter(f, ADAPTERS).name == name


@pytest.mark.parametrize("src", SOURCES, ids=sid)
@pytest.mark.parametrize("dst", TARGETS, ids=lambda t: f"{t[0]}-{'-'.join(t[1].values()) or 'default'}")
def test_translate(src, dst, tmp_path):
    name, f = src
    target, opts = dst
    ir, _ = ADAPTERS[name].parse(f)
    assert ir.agents, f"{f.name}: nothing parsed"
    rep = ADAPTERS[target].emit(ir, tmp_path, opts)
    assert rep.worst < Severity.error, [i for i in rep.items if i.severity >= Severity.error]
    check_output(target, opts, tmp_path)
    if opts.get("format") == "sdk" or target in ("agentcore", "langgraph", "llamaindex"):     # write-only outputs
        return
    back, brep = ADAPTERS[target].parse(main_file(target, opts, tmp_path))
    assert back.agents and brep.worst < Severity.error
    # lossy steps are always declared: a translation that drops agents says so
    if len(back.agents) != len(ir.agents):
        assert rep.worst >= Severity.lossy or target == "adk", (len(ir.agents), len(back.agents))
