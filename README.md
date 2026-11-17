# agent-translator

Translate AI agent definitions between platforms through a common intermediate representation (IR).

Platforms: AWS Bedrock Agents, GCP ADK, Azure Foundry Agent Service, Kagent, plus Oracle Agent Spec as an interchange format. See [DESIGN.md](DESIGN.md).

## Status

| Platform | Format | Parse | Emit |
|---|---|---|---|
| Kagent | `Agent` / `AgentTemplate` CRDs, `v1alpha2` and `v1alpha3` | yes | yes |
| AWS Bedrock Agents | CloudFormation `AWS::Bedrock::Agent`; API/CLI JSON (`get-agent` output, or a bundle with its action groups, knowledge bases and collaborators) | yes | CloudFormation (default) or API request files (`-O format=sdk`) |
| GCP ADK | Agent Config YAML (`root_agent.yaml` + referenced files) | yes | yes |
| Azure | Foundry `azure.yaml` prompt agents, Agent Framework `kind: Prompt` (`-O format=...`) | yes | yes |
| Oracle Agent Spec | `Agent`, `ManagerWorkers`, `Swarm` YAML/JSON (flows are reported, not translated) | yes | yes |

Everything is tested against official sample files (see `fixtures/*/SOURCES.md`); synthetic fixtures
are marked as such. Translations that cannot be exact say so in a report (`info` < `warning` <
`lossy` < `manual` < `error`), and `--strict` fails on `lossy` or worse.

### Fetching from a platform (read-only)

```bash
uv sync --extra fetch-aws --extra fetch-azure        # only what you need
agent-translator fetch bedrock AGENT_ID[:VERSION] -O region=us-east-1 [-O profile=NAME]
agent-translator fetch kagent [NAMESPACE/]NAME [-O context=CTX] [-O api_version=v1alpha2]
agent-translator fetch azure AGENT_NAME[:VERSION] -O endpoint=https://<acct>.services.ai.azure.com/api/projects/<proj>
# or in one step:
agent-translator translate AGENT_ID --fetch --from bedrock --fetch-option region=us-east-1 --to kagent
```

Fetching only *reads* (Bedrock `Get*`/`List*`, `kubectl get`, Azure `agents.get`), with your own
credentials (AWS default chain, your kubeconfig, `DefaultAzureCredential`); it never writes to the
platform and never reads secrets.

### Deploying (writes to your account — dry run by default)

```bash
agent-translator translate my-agent/ --to kagent -o out
agent-translator deploy kagent out/kagent.yaml -O context=lab -O namespace=kagent        # plan only
agent-translator deploy kagent out/kagent.yaml -O context=lab -O check=remote            # + read-only checks
agent-translator deploy kagent out/kagent.yaml -O context=lab --apply                    # asks for confirmation
agent-translator deploy azure out/azure.yaml -O endpoint=https://<acct>.services.ai.azure.com/api/projects/<proj> --apply --yes
agent-translator deploy bedrock out/ -O region=eu-west-1 -O model=<model-id> -O role_arn=<role-arn> --apply
```

`deploy` is conservative on purpose:

- **Nothing happens without `--apply`**, and `--apply` asks for confirmation (or `--yes`; without a
  terminal and without `--yes` it refuses). The plan is always printed first.
- **Create-only**: an object, agent or stack that already exists is an error — nothing is
  overwritten, updated or deleted (`-O new_version=true` lets Azure add a *version* to an existing agent).
  Every successful run prints how to remove what it created.
- Files that still contain `REPLACE-WITH-…` placeholders are refused; `Secret`s are never created
  (create the key Secret yourself, as the report says).
- Kagent: `kubectl create` per object (never `apply`/`replace`); `check=remote` is a server-side dry run.
  Azure: prompt agents via `agents.create_version`; the model *deployment* must already exist
  (this tool does not create deployments, which is where Azure's cost is). Bedrock: one
  CloudFormation stack from the emitted template (atomic, rolls back on failure, removed completely
  with `delete-stack`); the `-O format=sdk` output is not deployable. `-O wait=true` blocks until created.
- Same extras and credentials as `fetch`. Bedrock needs `cloudformation:CreateStack`/`DescribeStacks`/
  `ValidateTemplate`, `iam:PassRole` for the agent role, and the `bedrock:*` actions CloudFormation
  performs for the resources in the template.

### Models

Each platform names models differently (`anthropic.claude-sonnet-4-5-20250929-v1:0` on Bedrock,
`claude-sonnet-4-5` on the Anthropic API, `gpt-4.1` as an Azure deployment...). A built-in table
(`src/agent_translator/models/`, see its `SOURCES.md`) recognises them and adapts the id to the
target. `-O model_policy=...` chooses how far to go:

| policy | behaviour |
|---|---|
| `keep` | never touch the id |
| `rehost` (default) | same model, different host: swap in the id the target uses (`model-rehosted`). A different model is never chosen; if the target cannot serve it a same-size alternative is only suggested (`model-suggestion`) |
| `tier` | also substitute the newest model of the same size tier the target serves (`model-substituted`, reported as lossy) |

Ids not in the table are copied unchanged, as before.

See [docs/MATRIX.md](docs/MATRIX.md) for the outcome of every fixture translated to every target, and
`schema/ir.schema.json` for the JSON Schema of the IR (`agent-translator ir-schema`).

```bash
uv sync --extra dev --extra validate-azure --extra validate-agentspec
uv run agent-translator inspect fixtures/kagent/<file>.yaml
uv run pytest
```

## Why not [Agent Spec](https://github.com/oracle/agent-spec)?

Oracle's Open Agent Specification (Apache-2.0) is the closest existing project: a declarative format
for agents and workflows with adapters to LangGraph, AutoGen, CrewAI, OpenAI Agents, Microsoft Agent
Framework and its WayFlow runtime. We evaluated it (Agent Spec 26.3.1, `pyagentspec`) against our
fixtures and did not adopt it as the intermediate representation, because it targets *portable
execution*, not translating *deployment definitions* between clouds:

- `Agent` requires an `llm_config` and a `system_prompt`; many real agents have neither resolvable
  offline (external `ModelConfig`, prompts in a ConfigMap, parameters in a CloudFormation stack).
- It has no LLM configuration for Bedrock, Azure Foundry or Kagent.
- Memory, guardrails and knowledge bases (RAG) are not concepts of the spec.
- Deployment objects (IAM roles, aliases, `ModelConfig`/`Secret`, `AgentTemplate`) are out of scope.
- None of its adapters targets the four platforms above.

We borrowed what fits (`requires_confirmation` on tools, remote agents as collaborators) and plan an
Agent Spec adapter so that it can act as a bridge to those runtimes. Details and the criteria for
revisiting this decision are in [DESIGN.md](DESIGN.md#agent-spec-evaluation-after-m2).

License: Apache-2.0
