"""Regenerate src/agent_translator/models/catalog_data.py from a LiteLLM model catalog snapshot.

    python scripts/gen_model_catalog.py model_prices_and_context_window.json <litellm-commit-sha>

LiteLLM (MIT) keeps a community-maintained list of the model ids each provider accepts. We use it
only as a reference to *build* our table; the snapshot itself is not redistributed. Selection,
tiers and versions below are our own rules.
"""
import json
import re
import sys
from pathlib import Path

snap = json.loads(Path(sys.argv[1]).read_text())
sha = sys.argv[2]
chat = {k: v for k, v in snap.items()
        if isinstance(v, dict) and v.get("mode") == "chat"}


def keys(provider: str, prefix: str = "") -> set[str]:
    out = set()
    for k, v in chat.items():
        if v.get("litellm_provider") == provider and k.startswith(prefix):
            rest = k[len(prefix):]
            if "/" not in rest:
                out.add(rest)
    return out


BAD = re.compile(r"(cyber|luna|sol|terra|tts|robotics|computer-use|omni|customtools|search|audio|"
                 r"realtime|chat|codex|instruct$|ft:|mythos|exp|learnlm|gemma|lyria|deep-research|"
                 r"argon|carbon|hydrogen|latest)")
entries: dict[str, dict] = {}


def add(key, vendor, tier, **ids):
    e = entries.setdefault(key, {"vendor": vendor, "tier": tier, "ids": {}, "aliases": []})
    for h, i in ids.items():
        e["ids"].setdefault(h, i)


# ------------------------------------------------------------------ Anthropic
bedrock_ids = keys("bedrock_converse") | keys("bedrock")
anth = {k for k in keys("anthropic") if not re.search(r"-\d{8}$", k) and "preview" not in k}
vertex = keys("vertex_ai-anthropic_models", "vertex_ai/")


def claude_tier(k):
    return "large" if re.search(r"opus|fable", k) else "medium" if "sonnet" in k else "small"


def claude_key(name):
    return re.sub(r"-v\d.*$", "", re.sub(r"@.*$", "", name))


def base_of(k):
    """Family key: the undated alias when Anthropic has one (claude-sonnet-4-5-2025... -> claude-sonnet-4-5)."""
    b = re.sub(r"-\d{8}$", "", k)
    return b if b != k and b in entries else k


# the catalog lists a date for 3.7 Sonnet that never existed (it is 20250219)
WRONG = {"anthropic.claude-3-7-sonnet-20240620-v1:0"}


for k in sorted(anth):
    if BAD.search(k.replace("fable", "")):
        continue
    add(k, "anthropic", claude_tier(k), anthropic=k)
for b in sorted(bedrock_ids):
    if not b.startswith("anthropic.claude-") or BAD.search(b.replace("fable", "")) or "@" in b:
        continue
    if b.startswith(("anthropic.claude-v", "anthropic.claude-instant")) or b in WRONG:
        continue
    raw = claude_key(b.removeprefix("anthropic."))
    k = base_of(raw)
    add(k, "anthropic", claude_tier(k), bedrock=b)
    if raw != k:
        entries[k]["aliases"].append(raw)
for d in sorted(keys("anthropic")):
    if re.search(r"-\d{8}$", d) and base_of(d) != d:
        entries[base_of(d)]["aliases"].append(d)
for v in sorted(vertex):
    k = base_of(claude_key(v))
    if k in entries:
        ids = [x for x in vertex if claude_key(x) == k]
        dated = sorted(x for x in ids if re.search(r"@\d{8}$", x))
        entries[k]["ids"].setdefault("vertex", dated[-1] if dated else k)
        if dated:
            entries[k]["ids"]["vertex"] = dated[-1]

# --------------------------------------------------------------------- OpenAI
GPT = re.compile(r"^gpt-(4o(-mini)?|4\.1(-mini|-nano)?|5(\.\d)?(-mini|-nano)?)$")
oai, az = keys("openai"), keys("azure", "azure/")
for k in sorted(oai):
    if GPT.match(k):
        tier = "small" if re.search(r"mini|nano", k) else "medium" if re.match(r"gpt-4", k) else "large"
        add(k, "openai", tier, openai=k)
        if k in az:
            entries[k]["ids"]["azure"] = k

# --------------------------------------------------------------------- Gemini
GEM = re.compile(r"^gemini-\d(\.\d)?-(pro|flash|flash-lite)(-preview)?$")
gem = keys("gemini", "gemini/")
vgem = keys("vertex_ai-language-models")
for k in sorted(gem | vgem):
    if GEM.match(k) and not BAD.search(k.replace("preview", "")):
        tier = "large" if "-pro" in k else "small" if "flash-lite" in k else "medium"
        add(k, "google", tier)
        if k in gem:
            entries[k]["ids"]["google"] = k
        if k in vgem:
            entries[k]["ids"]["vertex"] = k

# ------------------------------------------------------------ Bedrock-only families
for b in sorted(bedrock_ids):
    m = re.match(r"^(amazon\.nova-[\w:-]+|meta\.llama[\w.:-]+|mistral\.[\w.:-]+)$", b)
    if not m or BAD.search(b) or "voxtral" in b or "pixtral" in b:
        continue
    vendor = b.split(".")[0]
    tier = None
    if vendor == "amazon":
        tier = ("large" if "premier" in b or "pro-preview" in b else "medium" if "pro" in b else "small")
    add(re.sub(r"(-v\d)?:0$", "", b).removeprefix(vendor + "."), vendor, tier, bedrock=b)

out = Path("src/agent_translator/models/catalog_data.py")
for e in entries.values():
    e["aliases"] = sorted(set(e["aliases"]))
    if not e["aliases"]:
        del e["aliases"]
body = json.dumps(dict(sorted(entries.items())), indent=1, sort_keys=True)
out.write_text(
    '"""Model table: family -> vendor, size tier and the id each hosting platform uses.\n\n'
    "GENERATED by scripts/gen_model_catalog.py, do not edit by hand. Source: the community-\n"
    f"maintained BerriAI/litellm model catalog (MIT), commit {sha}.\n"
    '"""\n\n'
    f'LITELLM_COMMIT = "{sha}"\n\nFAMILIES: dict[str, dict] = {body}\n'.replace(
        "null", "None").replace("true", "True").replace("false", "False"))
print(len(entries), "families")
