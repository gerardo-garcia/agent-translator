import pytest

from agent_translator.ir import ModelSpec
from agent_translator.models import FAMILIES, HOSTS, adapt, lookup
from agent_translator.registry import load_adapters
from agent_translator.report import Report

from .test_matrix import SOURCES

ADAPTERS = load_adapters()


def run(model_id, target, policy="rehost", provider=None):
    rep = Report()
    out = adapt(ModelSpec(provider=provider, id=model_id), target, {"model_policy": policy}, rep, "p")
    return out, {i.code for i in rep.items}


def test_table_is_consistent():
    for fam, e in FAMILIES.items():
        assert e["vendor"] and e["ids"], fam
        for host, i in e["ids"].items():
            m = lookup(i)
            assert m and m.family == fam and host in m.hosts, (fam, host, i)
        for a in e.get("aliases", []):
            assert lookup(a).family == fam, a


@pytest.mark.parametrize("model_id, family, vendor", [
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5", "anthropic"),
    ("claude-sonnet-4-5", "claude-sonnet-4-5", "anthropic"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5", "anthropic"),
    ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5", "anthropic"),
    ("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet-20241022", "anthropic"),
    ("us.amazon.nova-lite-v1:0", "nova-lite", "amazon"),
    ("gpt-5.4-mini", "gpt-5.4-mini", "openai"),
    ("gemini-2.5-pro", "gemini-2.5-pro", "google"),
])
def test_lookup_recognises_every_spelling(model_id, family, vendor):
    m = lookup(model_id)
    assert (m.family, m.vendor) == (family, vendor)


def test_unknown_ids_are_left_alone():
    assert lookup("amazon.titan-text-premier-v1:0") is None
    assert lookup("example-model") is None
    out, codes = run("example-model", "bedrock", "tier")
    assert out.id == "example-model" and not codes


def test_rehost_same_model_between_hosts():
    out, codes = run("claude-sonnet-4-5", "bedrock", provider="anthropic")
    assert out.id == "anthropic.claude-sonnet-4-5-20250929-v1:0" and "model-rehosted" in codes
    out, _ = run("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "kagent")
    assert out.id == "claude-sonnet-4-5" and out.provider == "anthropic"
    out, codes = run("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "bedrock")
    assert out.id.startswith("us.") and not codes          # already usable: untouched
    out, codes = run("gpt-4.1", "azure")
    assert out.id == "gpt-4.1" and not codes


def test_never_changes_family_unless_asked():
    for policy in ("keep", "rehost"):
        out, _ = run("gpt-4.1", "bedrock", policy)
        assert out.id == "gpt-4.1"
    _, codes = run("gpt-4.1", "bedrock", "rehost")
    assert codes == {"model-suggestion"}
    assert run("gpt-4.1", "bedrock", "keep")[1] == set()


def test_tier_substitutes_same_size_model():
    out, codes = run("gemini-2.5-flash", "azure", "tier")
    assert FAMILIES[lookup(out.id).family]["tier"] == "medium" and out.provider == "openai"
    assert codes == {"model-substituted"}
    out, _ = run("gpt-5.4-mini", "bedrock", "tier")
    assert lookup(out.id).vendor == "anthropic" and lookup(out.id).tier == "small"
    out, _ = run("amazon.nova-pro-v1:0", "bedrock", "tier")
    assert out.id == "amazon.nova-pro-v1:0"                # Bedrock serves it


def test_substitutes_exist_on_the_target():
    """Every tier substitution lands on an id the target platform actually uses."""
    for target, host in HOSTS.items():
        for fam, e in FAMILIES.items():
            if e["tier"] is None:       # no size tier (Llama, Mistral): never substituted
                continue
            out, _ = run(next(iter(e["ids"].values())), target, "tier")
            m = lookup(out.id)
            assert m and (m.hosts & host.serves), (target, fam, out.id)
    out, codes = run("meta.llama3-3-70b-instruct-v1:0", "azure", "tier")
    assert out.id.startswith("meta.") and codes == set()


def test_params_survive_adaptation():
    rep = Report()
    m = ModelSpec(provider="openai", id="gpt-4.1", params={"temperature": 0.2})
    assert adapt(m, "bedrock", {"model_policy": "tier"}, rep, "p").params == {"temperature": 0.2}


def test_bad_policy_is_reported_and_ignored():
    out, codes = run("claude-sonnet-4-5", "bedrock", "nonsense")
    assert "model-policy-unknown" in codes and out.id.startswith("anthropic.")


@pytest.mark.parametrize("src", [s for s in SOURCES if s[0] == "bedrock"], ids=lambda s: s[1].name)
def test_bedrock_samples_to_direct_apis(src, tmp_path):
    """Official Bedrock samples (Claude on Bedrock) -> Kagent: ids the Anthropic API has are
    swapped; older models it does not list are kept and a suggestion is reported."""
    ir, _ = ADAPTERS["bedrock"].parse(src[1])
    rep = ADAPTERS["kagent"].emit(ir, tmp_path, {"api_version": "v1alpha3"})
    text = (tmp_path / "kagent.yaml").read_text()
    for a in ir.agents:
        m = lookup(a.model.id)
        if m and m.vendor == "anthropic" and "anthropic" in FAMILIES[m.family]["ids"]:
            assert FAMILIES[m.family]["ids"]["anthropic"] in text
            assert any(i.code == "model-rehosted" for i in rep.items)
        elif m and m.vendor == "anthropic":
            assert any(i.code == "model-suggestion" for i in rep.items)


def test_azure_official_sample_models_are_recognised():
    ids = {a.model.id for n, f in SOURCES if n == "azure" for a in ADAPTERS[n].parse(f)[0].agents
           if a.model.id}
    assert {"gpt-4.1", "gpt-4.1-mini", "gpt-5.4-mini"} <= ids
    assert all(lookup(i) for i in ids), [i for i in ids if not lookup(i)]


def test_tier_policy_end_to_end(tmp_path):
    f = next(f for n, f in SOURCES if n == "azure" and "gpt-4.1" in f.read_text())
    ir, _ = ADAPTERS["azure"].parse(f)
    rep = ADAPTERS["bedrock"].emit(ir, tmp_path, {"model_policy": "tier"})
    assert any(i.code == "model-substituted" for i in rep.items)
    assert "FoundationModel: anthropic." in (tmp_path / "template.yaml").read_text()
