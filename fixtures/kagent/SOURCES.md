# Kagent fixtures

All upstream material is from https://github.com/kagent-dev/kagent (Apache-2.0).

## v1alpha3 (`api.kagent.dev/v1alpha3`, Kagent 1.0)
Tag v1.0.1, commit `558e3d761d8ed2b0d23bfef1867a9d1915210121`.

| File | Origin | Kind |
|---|---|---|
| v1alpha3/assistant-inline.yaml | docs/architecture/configuration-and-compilation.md | official |
| v1alpha3/team-a-shared-template.yaml | docs/architecture/cross-namespace-references.md | official |
| v1alpha3/data-extractor.yaml | docs/architecture/structured-output.md | official |
| v1alpha3/code-reviewer.yaml | docs/architecture/egress-policies.md (verbatim; its AgentTemplate/Harness are not in the doc block) | official |
| v1alpha3/reviewer-subagent.yaml | built from the `subAgent` snippet in docs/architecture/a2a-subagents.md | synthetic |

## v1alpha2 (`kagent.dev/v1alpha2`, Kagent 0.10.x)
Tag v0.10.6, commit `e36246357df0b8ad248e421ddfcce534be1d6816`.

| File | Origin | Kind |
|---|---|---|
| v1alpha2/promql-agent.yaml | `helm/agents/promql`, rendered with `helm template` | official (rendered) |
| v1alpha2/promql-agent-memory.yaml | same chart with `memory.enabled=true` | official (rendered) |
| v1alpha2/observability-multiagent.yaml | `helm/agents/{promql,k8s,observability}` rendered and concatenated; observability uses promql-agent as an `Agent` tool | official (rendered) |
| v1alpha2/k8sgpt-remote-mcpserver.yaml | contrib/tools/k8sgpt-mcp-server | official |
| v1alpha2/server-everything-remote-mcpserver.yaml | contrib/tools/server-everything | official |
| v1alpha2/synthetic-assistant.yaml | hand-written to match the CRDs | synthetic |

Regenerate the rendered ones with `scripts/render_kagent_v1alpha2_fixtures.sh` (needs `helm`).

Not used: upstream `examples/modelconfig-with-tls.yaml` — its `Agent` (`framework:`/`config:`) and
`apiKeySecretRef` do not exist in the CRDs, i.e. the example is stale.

## CRDs (`crds/v1alpha2`, `crds/v1alpha3`)
Verbatim from the same tags. Tests validate every fixture and every emitted manifest against their
`openAPIV3Schema`.
