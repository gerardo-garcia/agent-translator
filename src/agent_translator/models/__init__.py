"""Model table: recognise a model id from any platform and adapt it to the target platform.

Policies (target option ``model_policy``):

* ``keep``   never touch the id (old behaviour);
* ``rehost`` (default) same model, different host: when the target cannot use the source id
  (e.g. a Bedrock Claude id for Kagent, which calls the Anthropic API) swap in the id the same
  model has there. Different model families are never substituted;
* ``tier``   additionally, when the target cannot serve the model at all (e.g. Claude for Azure
  Foundry), substitute the newest model of the same size tier from a family the target serves.

Whenever the id is not touched but a substitute exists, a ``model-suggestion`` is reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir import ModelSpec
from ..report import Report, Severity
from .catalog_data import FAMILIES, LITELLM_COMMIT

POLICIES = ("keep", "rehost", "tier")
__all__ = ["FAMILIES", "LITELLM_COMMIT", "POLICIES", "Match", "adapt", "lookup"]


@dataclass(frozen=True)
class Host:
    serves: frozenset[str]   # host keys whose ids the target platform can use as they are
    prefer: tuple[str, ...]  # host keys to rehost to, in order
    tier_vendors: tuple[str, ...]  # vendors to substitute with (after the source's own vendor)
    note: str = ""


HOSTS = {
    "bedrock": Host(frozenset({"bedrock"}), ("bedrock",), ("anthropic",),
                    "recent Bedrock models may need a cross-region inference profile id "
                    "(prefix us./eu./apac./global.); set one if invoking the agent fails"),
    "kagent": Host(frozenset({"anthropic", "openai", "google"}),
                   ("anthropic", "openai", "google"), ("anthropic",)),
    "adk": Host(frozenset({"google", "vertex", "anthropic", "openai", "bedrock"}),
                ("google", "vertex", "anthropic", "openai"), ("google",)),
    "azure": Host(frozenset({"azure", "openai"}), ("azure",), ("openai",),
                  "the Foundry `model` is a deployment name: create a deployment with this name"),
    "agentspec": Host(frozenset({"openai", "google", "anthropic"}),
                      ("openai", "google", "anthropic"), ("openai",)),
}

_PREFIXES = re.compile(r"^(?:bedrock/(?:converse/|invoke/)?|vertex_ai/|anthropic/|openai/|azure/|"
                       r"gemini/|models/)")
_REGION = re.compile(r"^(?:us|eu|apac|global|us-gov|jp|au|ca)\.(?=(?:anthropic|amazon|meta|mistral)\.)")


@dataclass(frozen=True)
class Match:
    family: str
    vendor: str
    tier: str | None
    hosts: frozenset[str]  # host keys that use exactly this id


def _norm(model_id: str) -> str:
    s = _PREFIXES.sub("", model_id.strip())
    return _REGION.sub("", s)


def _index() -> dict[str, dict[str, set[str]]]:
    ix: dict[str, dict[str, set[str]]] = {}
    for fam, e in FAMILIES.items():
        for host, i in e["ids"].items():
            ix.setdefault(i, {}).setdefault(fam, set()).add(host)
            ix.setdefault(i.split("@")[0], {}).setdefault(fam, set()).add(host)
        for a in (fam, *e.get("aliases", [])):
            ix.setdefault(a, {}).setdefault(fam, set())
            if e["vendor"] == "anthropic" and "anthropic" in e["ids"]:
                ix[a][fam].add("anthropic")
    return ix


_IX = _index()


def lookup(model_id: str | None) -> Match | None:
    """Recognise `model_id` (any platform's spelling); None if it is not in the table."""
    if not model_id:
        return None
    key = _norm(model_id)
    hit = _IX.get(key) or _IX.get(key.split("@")[0])
    if not hit:
        return None
    fam = min(hit, key=len)
    e = FAMILIES[fam]
    return Match(fam, e["vendor"], e["tier"], frozenset(hit[fam]))


def known(model_id: str | None, host_key: str) -> bool:
    m = lookup(model_id)
    return bool(m and host_key in m.hosts)


def _version(fam: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", re.sub(r"\d{8}", "", fam))]
    return (tuple(nums), "preview" not in fam)


def _pick(vendor: str, tier: str, host: Host, exclude: str = "") -> tuple[str, str, str] | None:
    """(family, host_key, id) of the newest `vendor`/`tier` model the target can use."""
    best = None
    for fam, e in FAMILIES.items():
        if e["vendor"] != vendor or e["tier"] != tier or fam == exclude:
            continue
        for hk in host.prefer:
            if hk in e["ids"]:
                cand = (_version(fam), fam, hk, e["ids"][hk])
                if best is None or cand[0] > best[0]:
                    best = cand
                break
    return best[1:] if best else None


def _rehost(entry: dict, host: Host) -> tuple[str, str] | None:
    for hk in host.prefer:
        if hk in entry["ids"]:
            return hk, entry["ids"][hk]
    return None


def _provider(vendor: str, fallback: str | None) -> str | None:
    return vendor if vendor in ("anthropic", "openai", "google", "amazon", "meta", "mistral") \
        else fallback


def adapt(m: ModelSpec, target: str, options: dict | None, rep: Report, path: str) -> ModelSpec:
    """Return the model as `target` should use it (see module docstring for the policies)."""
    policy = (options or {}).get("model_policy", "rehost")
    if policy not in POLICIES:
        rep.add(Severity.warning, "model-policy-unknown", path,
                f"model_policy={policy!r} ignored; use one of {', '.join(POLICIES)}")
        policy = "rehost"
    host = HOSTS.get(target)
    match = lookup(m.id)
    if policy == "keep" or host is None or match is None:
        return m
    if match.hosts & host.serves:
        return m
    entry = FAMILIES[match.family]
    moved = _rehost(entry, host)
    if moved:
        hk, new_id = moved
        rep.add(Severity.warning if hk not in ("azure",) else Severity.info,
                "model-rehosted", path,
                f"{m.id!r} -> {new_id!r}: same model, id used by {hk}"
                + (f" ({host.note})" if host.note else ""))
        return ModelSpec(provider=_provider(match.vendor, m.provider), id=new_id, params=m.params)
    # the target cannot serve this model: find a same-size model it can
    sub = None
    if match.tier:
        for vendor in dict.fromkeys((match.vendor, *host.tier_vendors)):
            sub = _pick(vendor, match.tier, host)
            if sub:
                break
    if sub and policy == "tier":
        fam, hk, new_id = sub
        rep.add(Severity.lossy, "model-substituted", path,
                f"{m.id!r} ({match.vendor}, {match.tier}) is not available on {target}: replaced "
                f"by {new_id!r}, the newest {FAMILIES[fam]['vendor']} {match.tier}-tier model"
                + (f" ({host.note})" if host.note else ""))
        return ModelSpec(provider=_provider(FAMILIES[fam]["vendor"], None), id=new_id,
                         params=m.params)
    if sub:
        rep.add(Severity.info, "model-suggestion", path,
                f"{m.id!r} is not available on {target}; a same-size alternative is {sub[2]!r} "
                "(use -O model_policy=tier to substitute it automatically)")
    return m


def adapt_ir(ir, target: str, options: dict | None, rep: Report):
    """Copy of `ir` whose agent models are adapted for `target` (reports into `rep`)."""
    out = ir.model_copy(deep=True)
    for a in out.agents:
        if a.model and a.model.id:
            a.model = adapt(a.model, target, options, rep, f"agents[{a.id}].model")
    return out
