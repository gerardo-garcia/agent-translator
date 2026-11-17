#!/usr/bin/env bash
# Regenerates fixtures/kagent/v1alpha2 from the official kagent v0.10.6 sources.
# Agents are Helm charts upstream, so they are rendered with `helm template`.
set -euo pipefail
TAG=v0.10.6
RAW=https://raw.githubusercontent.com/kagent-dev/kagent/$TAG
OUT="$(cd "$(dirname "$0")/.." && pwd)/fixtures/kagent/v1alpha2"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
curl -fsS "$RAW/helm/kagent/templates/_helpers.tpl" -o "$W/main-helpers.tpl"
printf 'global:\n  imagePullSecrets: []\n' > "$W/values.yaml"

render() { # name, outfile, extra helm args...
  local a=$1 out=$2; shift 2
  local d="$W/$a"; mkdir -p "$d/templates"
  for f in templates/agent.yaml values.yaml templates/_helpers.tpl Chart-template.yaml; do
    curl -fsS "$RAW/helm/agents/$a/$f" -o "$d/$f"
  done
  sed -e "s/\${VERSION}/${TAG#v}/g" "$d/Chart-template.yaml" > "$d/Chart.yaml"
  cp "$W/main-helpers.tpl" "$d/templates/_main_helpers.tpl"
  helm template "$a" "$d" --namespace kagent -f "$W/values.yaml" "$@" > "$out"
}

render promql    "$OUT/promql-agent.yaml"
render promql    "$OUT/promql-agent-memory.yaml" \
  --set memory.enabled=true --set memory.modelConfigRef=embedding-model-config
# multi-agent: observability uses promql-agent as an `Agent` tool
render observability "$W/obs.yaml"; render k8s "$W/k8s.yaml"
{ cat "$OUT/promql-agent.yaml"; cat "$W/k8s.yaml"; cat "$W/obs.yaml"; } > "$OUT/observability-multiagent.yaml"
# official RemoteMCPServer manifests, verbatim
curl -fsS "$RAW/contrib/tools/k8sgpt-mcp-server/k8sgpt-remote-mcpserver.yaml" -o "$OUT/k8sgpt-remote-mcpserver.yaml"
curl -fsS "$RAW/contrib/tools/server-everything/server-everything-remote-mcpserver.yaml" \
  -o "$OUT/server-everything-remote-mcpserver.yaml"
