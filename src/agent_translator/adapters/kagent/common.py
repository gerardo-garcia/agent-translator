"""Helpers shared by the Kagent v1alpha2 / v1alpha3 codecs."""
from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML

from ...ir import AgentIR, AgentSpec, ModelSpec
from ...models import known
from ...report import Report, Severity

#: version key -> apiVersion string
API = {"v1alpha3": "api.kagent.dev/v1alpha3", "v1alpha2": "kagent.dev/v1alpha2"}
VERSION_OF = {v: k for k, v in API.items()}
KAGENT_GROUPS = ("api.kagent.dev", "kagent.dev")
DEFAULT_MODEL_CONFIG = "default-model-config"

# kagent provider -> model vendor used in the IR
VENDOR = {
    "Anthropic": "anthropic", "AnthropicVertexAI": "anthropic",
    "OpenAI": "openai", "AzureOpenAI": "openai",
    "Gemini": "google", "GeminiVertexAI": "google",
}
# IR vendor -> kagent provider (direct API)
PROVIDER_HOST = {"Anthropic": "anthropic", "OpenAI": "openai", "Gemini": "google"}
PROVIDER = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Gemini"}
PROVIDER_BLOCK = {
    "Anthropic": "anthropic", "AnthropicVertexAI": "anthropicVertexAI", "OpenAI": "openAI",
    "AzureOpenAI": "azureOpenAI", "Gemini": "gemini", "GeminiVertexAI": "geminiVertexAI",
    "Bedrock": "bedrock", "Foundry": "foundry", "Ollama": "ollama", "Mistral": "mistral",
}
PARAM_KEYS = {
    "temperature": "temperature", "topP": "top_p", "topK": "top_k", "maxTokens": "max_tokens",
    "maxCompletionTokens": "max_tokens", "maxOutputTokens": "max_tokens",
}


def yaml_safe() -> YAML:
    return YAML(typ="safe")


def num(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def dns(name: str) -> str:
    n = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return n[:63].strip("-") or "agent"


def doc_key(doc: dict) -> tuple[str, str]:
    md = doc.get("metadata", {}) or {}
    return md.get("namespace", ""), md.get("name", "")


def unique_ids(agents: list[AgentSpec]) -> list[AgentSpec]:
    seen: dict[str, int] = {}
    for a in agents:
        n = seen.get(a.id, 0)
        seen[a.id] = n + 1
        if n:
            old = a.id
            a.id = f"{old}-{n + 1}"
            for o in agents:
                if o is not a:
                    o.sub_agents = [a.id if s == old else s for s in o.sub_agents]
    return agents


def kext(a: AgentSpec) -> dict[str, Any]:
    return a.extensions.get("kagent", {})


def report_foreign_ext(a: AgentSpec, target: str, rep: Report) -> None:
    """Version-specific fields kept in the IR that the target version cannot carry."""
    for v in API:
        if v != target and kext(a).get(v):
            rep.add(Severity.lossy, "kagent-fields-dropped", f"agents[{a.id}]",
                    f"{v}-only fields {sorted(kext(a)[v])} not representable in {target}")


# IR protocol vocabulary: sse | streamable-http | stdio
PROTOCOL_IN = {"SSE": "sse", "STREAMABLE_HTTP": "streamable-http"}
PROTOCOL_OUT = {"sse": "SSE", "streamable-http": "STREAMABLE_HTTP"}


# ----------------------------------------------------------------- parse side
def servers_from(by_kind: dict, rep: Report) -> list[dict]:
    servers = []
    for (sns, n), d in by_kind.get("RemoteMCPServer", {}).items():
        sp = d.get("spec", {})
        extra = {k: v for k, v in sp.items() if k not in ("url", "protocol", "description")}
        entry = {"id": n, "namespace": sns or None, "url": sp.get("url"),
                 "protocol": PROTOCOL_IN.get(sp.get("protocol"), sp.get("protocol")),
                 "description": sp.get("description"),
                 "extensions": {"kagent": {VERSION_OF[d["apiVersion"]]: extra}} if extra else None}
        servers.append({k: v for k, v in entry.items() if v is not None})
        if extra:
            rep.add(Severity.lossy, "mcp-server-fields-preserved", f"RemoteMCPServer/{n}",
                    f"fields {sorted(extra)} kept only in extensions.kagent")
    return servers


def model_from_ref(by_kind: dict, version: str, ns: str, ref: dict | None, rep: Report,
                   path: str) -> tuple[ModelSpec, dict]:
    if not ref:
        return ModelSpec(), {}
    mns = ref.get("namespace", ns)
    mc = by_kind.get("ModelConfig", {}).get((mns, ref["name"]))
    ext: dict[str, Any] = {"name": ref["name"], **({"namespace": ref["namespace"]}
                                                   if "namespace" in ref else {})}
    if mc is None:
        rep.add(Severity.warning, "modelconfig-external", path,
                f"ModelConfig {ref['name']!r} not in file; model id unknown")
        return ModelSpec(), ext
    spec = mc["spec"]
    ext["spec"], ext["version"] = spec, version
    ext["docNamespace"] = mns          # namespace of the ModelConfig document itself
    block = spec.get(PROVIDER_BLOCK.get(spec.get("provider", ""), ""), {}) or {}
    params = {PARAM_KEYS[k]: num(v) for k, v in block.items() if k in PARAM_KEYS}
    return ModelSpec(provider=VENDOR.get(spec.get("provider")), id=spec.get("model"),
                     params=params), ext


# ------------------------------------------------------------------ emit side
def model_config_ref(a: AgentSpec, name: str, version: str, rep: Report,
                     model_docs: dict[str, dict]) -> dict[str, str]:
    """Return {"name", ["namespace"]} for the agent's model, adding a ModelConfig doc if needed."""
    ext = kext(a).get("modelConfig", {})
    api = API[version]
    if ext.get("spec") and ext.get("name"):
        if ext.get("version") == version:   # same API version: verbatim
            doc_ns = ext.get("docNamespace", ext.get("namespace"))
            model_docs.setdefault(ext["name"], {
                "apiVersion": api, "kind": "ModelConfig",
                "metadata": {"name": ext["name"], **({"namespace": doc_ns} if doc_ns else {})},
                "spec": ext["spec"]})
            return {"name": ext["name"], **({"namespace": ext["namespace"]}
                                            if ext.get("namespace") else {})}
        rep.add(Severity.lossy, "modelconfig-regenerated", f"ModelConfig/{ext['name']}",
                f"ModelConfig from {ext.get('version')} regenerated for {version}: "
                "endpoint/TLS/secret settings are not carried over")
    # apiKeySecret(+Key) mean the same in both versions: reuse the user's existing Secret
    carried = {k: ext["spec"][k] for k in ("apiKeySecret", "apiKeySecretKey")
               if ext.get("spec", {}).get(k)}
    carried_provider = ext.get("spec", {}).get("provider")
    m = a.model
    if not m.id:
        if ext.get("name") and not ext.get("spec"):
            return {"name": ext["name"], **({"namespace": ext["namespace"]}
                                            if ext.get("namespace") else {})}
        rep.add(Severity.warning, "model-missing", f"agents[{a.id}].model",
                f"no model; using {DEFAULT_MODEL_CONFIG!r}")
        return {"name": DEFAULT_MODEL_CONFIG}
    provider = PROVIDER.get(m.provider or "")
    if provider is None:
        rep.add(Severity.manual, "model-provider-unmapped", f"agents[{a.id}].model",
                f"provider {m.provider!r} unknown to Kagent adapter; using {DEFAULT_MODEL_CONFIG!r}")
        return {"name": DEFAULT_MODEL_CONFIG}
    if not known(m.id, PROVIDER_HOST[provider]):
        rep.add(Severity.info, "model-unverified", f"agents[{a.id}].model",
                f"model id {m.id!r} copied as-is; verify it for provider {provider}")
    mc = f"{name}-model"
    spec: dict[str, Any] = {"provider": provider, "model": m.id}
    if carried and carried_provider == provider:
        spec.update(carried)
        rep.add(Severity.info, "secret-reused", f"ModelConfig/{mc}",
                f"reusing Secret {carried['apiKeySecret']!r} from the source ModelConfig")
    else:
        spec.update(apiKeySecret=f"{mc}-key", apiKeySecretKey="api-key")
        rep.add(Severity.manual, "secret-required", f"ModelConfig/{mc}",
                f"create Secret {mc}-key with key 'api-key'")
    block: dict[str, Any] = {}
    for k, ir_k in (("temperature", "temperature"), ("topP", "top_p"), ("topK", "top_k"),
                    ("maxTokens", "max_tokens")):
        v = m.params.get(ir_k)
        if v is None:
            continue
        if provider == "Gemini":
            if ir_k == "max_tokens":
                block["maxOutputTokens"] = v
            else:
                rep.add(Severity.lossy, "param-dropped", f"agents[{a.id}].model.params.{ir_k}",
                        "not supported by Kagent 'gemini' block")
            continue
        block[k] = str(v) if k in ("temperature", "topP") else v
    if block:
        spec[PROVIDER_BLOCK[provider]] = block
    ns = kext(a).get("namespace")
    model_docs[mc] = {"apiVersion": api, "kind": "ModelConfig",
                      "metadata": {"name": mc, **({"namespace": ns} if ns else {})}, "spec": spec}
    return {"name": mc}


def mcp_docs(ir: AgentIR, used: set[str], version: str, rep: Report) -> list[dict]:
    known = {s.get("id"): s for s in ir.resources.mcp_servers}
    docs = []
    for name in sorted(used):
        s = known.get(name)
        if not s or not s.get("url"):
            rep.add(Severity.manual, "mcp-server-undefined", f"RemoteMCPServer/{name}",
                    "no URL known; define this RemoteMCPServer manually")
            continue
        spec = {"description": s.get("description") or name, "url": s["url"]}
        proto = PROTOCOL_OUT.get(str(s.get("protocol")).lower())
        if proto:
            spec["protocol"] = proto
        elif s.get("protocol"):
            rep.add(Severity.manual, "mcp-protocol", f"RemoteMCPServer/{name}",
                    f"protocol {s['protocol']!r} has no Kagent equivalent (SSE or STREAMABLE_HTTP "
                    "only); left to Kagent's default")
        extra = s.get("extensions", {}).get("kagent", {})
        spec.update(extra.get(version, {}))
        for v in API:
            if v != version and extra.get(v):
                rep.add(Severity.lossy, "kagent-fields-dropped", f"RemoteMCPServer/{name}",
                        f"{v}-only fields {sorted(extra[v])} not representable in {version}")
        docs.append({"apiVersion": API[version], "kind": "RemoteMCPServer",
                     "metadata": {"name": dns(name),
                                  **({"namespace": s["namespace"]} if s.get("namespace") else {})},
                     "spec": spec})
    return docs


def approval_in(raw: Any) -> bool | None:
    """IR requires_confirmation from a Kagent requireApproval value (bool or tool-name list)."""
    return None if raw is None else bool(raw)


def approval_raw(tool: Any) -> Any:
    """Kagent value to start from: the original (possibly per-tool list) unless the IR overrides."""
    raw = tool.extensions.get("kagent", {}).get("requireApproval")
    if tool.requires_confirmation is None:
        return None
    if not tool.requires_confirmation:
        return False if raw is not None else None
    return raw if raw else True


def approval_for(version: str, raw: Any, tool_names: list[str], path: str, rep: Report) -> Any:
    """requireApproval is a bool in v1alpha3 and a list of tool names in v1alpha2."""
    if raw is None:
        return None
    if version == "v1alpha3":
        if isinstance(raw, list):
            if raw and set(raw) != set(tool_names):
                rep.add(Severity.lossy, "require-approval-coarsened", path,
                        "v1alpha3 requireApproval is all-or-nothing; per-tool list collapsed")
            return bool(raw)
        return raw
    if isinstance(raw, bool):
        if raw and not tool_names:
            rep.add(Severity.lossy, "require-approval-dropped", path,
                    "v1alpha2 needs explicit toolNames to require approval")
            return None
        return list(tool_names) if raw else None
    return raw


GROUP = {"v1alpha2": "kagent.dev", "v1alpha3": "api.kagent.dev"}


def map_group(raw: str | None, version: str) -> str | None:
    """Translate a Kagent API group between versions; foreign groups are kept as-is."""
    if raw in GROUP.values():
        return GROUP[version]
    return raw
