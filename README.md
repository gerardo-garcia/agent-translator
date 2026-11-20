# agent-translator

Translate AI agent definitions between platforms: **AWS Bedrock Agents**, **GCP ADK**, **Azure Foundry
Agent Service** and **Kagent**, with Oracle Agent Spec as an interchange format. Every translation goes
through a common intermediate representation (IR), so N platforms need 2N adapters instead of N·(N-1)
translators, and anything that cannot be translated exactly is **reported, never dropped silently**.

The tool has three layers, in order of importance:

1. **Translate** — the core. Local files in, local files out, no network, no credentials.
2. **Fetch** — read an existing agent from a platform into local files (read-only), so you can translate it.
3. **Deploy** — create an agent on a platform from translated files (create-only, dry run by default).

Design notes and the reasoning behind each decision are in [DESIGN.md](DESIGN.md).

## Install

Not on PyPI yet; run it from a clone (Python ≥ 3.11, [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/gerardo-garcia/agent-translator && cd agent-translator
uv sync
uv run agent-translator --help
```

## 1. Translate

| Platform | Formats read | Formats written |
|---|---|---|
| Kagent | `Agent` / `AgentTemplate` CRDs, `v1alpha2` and `v1alpha3` | same (`-O api_version=v1alpha2\|v1alpha3`, default v1alpha3) |
| AWS Bedrock Agents | CloudFormation `AWS::Bedrock::Agent`; API/CLI JSON (`get-agent` output, or a bundle with its action groups, knowledge bases and collaborators) | CloudFormation (default) or API request files (`-O format=sdk`) |
| GCP ADK | Agent Config YAML (`root_agent.yaml` + referenced files) | same, plus generated `tools.py` / `models.py` when YAML is not enough |
| Azure Foundry | `azure.yaml` prompt agents, Agent Framework `kind: Prompt` | same (`-O format=foundry\|agent-framework`, default foundry) |
| Oracle Agent Spec | `Agent`, `ManagerWorkers`, `Swarm` YAML/JSON (flows are reported, not translated) | same |

The source format is autodetected; use `--from <platform>` if it is ambiguous. `agent-translator
list-platforms` prints the available adapters.

```bash
agent-translator translate SOURCE --to TARGET [-o OUT_DIR] [-O key=value ...] [--strict] [--report report.json]
agent-translator inspect SOURCE          # print the IR (what the translation is built from)
```

### Examples

The examples below use the sample agents shipped in [`fixtures/`](fixtures) (official samples from each
platform, see `fixtures/*/SOURCES.md`; the ones marked *synthetic* were written for this repo). The
output shown is real.

**Azure Foundry → Kagent.** A prompt agent with a remote MCP tool
([`fixtures/azure/foundry/03-mcp-tools.yaml`](fixtures/azure/foundry/03-mcp-tools.yaml)):

```bash
agent-translator translate fixtures/azure/foundry/03-mcp-tools.yaml --to kagent -o out
```
```
[info] model-unverified services.prompt-agent-mcp-tools model id 'gpt-5.4-mini' copied as-is
[manual] secret-required ModelConfig/prompt-agent-mcp-tools-model create Secret prompt-agent-mcp-tools-model-key with key 'api-key'
[info] harness-default prompt-agent-mcp-tools harnessRef='kagent-default' (created by `kagent install`)
[info] written out/kagent.yaml Kagent v1alpha3
status: partial
```

`out/kagent.yaml` contains a `ModelConfig`, a `RemoteMCPServer` (`https://learn.microsoft.com/api/mcp`,
`STREAMABLE_HTTP`) and the `Agent` that references both. The one `manual` item is something no tool can do
for you: create the API-key Secret in your cluster.

**Bedrock → Kagent.** An official AWS sample, the HR assistant
([`fixtures/bedrock/cfn/hr-assistant.yaml`](fixtures/bedrock/cfn/hr-assistant.yaml)):

```bash
agent-translator translate fixtures/bedrock/cfn/hr-assistant.yaml --to kagent -o out
```
```
[manual] schema-external Resources.HRAgent.ActionGroups[0] OpenAPI schema lives in S3 or a reference; contents unknown
[warning] prompt-override-platform-specific Resources.HRAgent.PromptOverrideConfiguration custom prompt templates kept in extensions.bedrock only
[lossy] tool-unsupported agents[HRAgent].tools[send-email] tool kind 'openapi' has no Kagent equivalent; expose it as an MCP server and reference it
  ... (4 action groups, one such line each)
status: partial
```

This is what an honest translation between very different platforms looks like. Kagent has no OpenAPI
action groups, so the four tools are reported `lossy` with the way out (expose them as an MCP server);
Bedrock-only features such as prompt overrides are kept in `extensions` so a later translation back to
Bedrock does not lose them.

**Choosing the model.** Model ids are not portable. The same Azure agent to Bedrock, with the default
policy and then with `-O model_policy=tier`:

```bash
agent-translator translate fixtures/azure/foundry/03-mcp-tools.yaml --to bedrock -o out
agent-translator translate fixtures/azure/foundry/03-mcp-tools.yaml --to bedrock -O model_policy=tier -o out
```
```
# default (rehost): never changes the model, only suggests
[info] model-suggestion ... 'gpt-5.4-mini' is not available on bedrock; a same-size alternative is 'anthropic.claude-haiku-5-5' (use -O model_policy=tier to substitute it automatically)
[manual] model-required ... model 'gpt-5.4-mini' (openai) is not a Bedrock model id; choose one

# tier: substitutes the newest model of the same size and says so
[lossy] model-substituted ... 'gpt-5.4-mini' (openai, small) is not available on bedrock: replaced by 'anthropic.claude-haiku-5-5'
```

See [Models](#models) below. Both runs also report that Bedrock cannot call an MCP server directly
(`tool-dropped`, `lossy`) and that the CloudFormation template needs an IAM role ARN (`role-required`,
`manual`), which you supply as a stack parameter.

**Migrating Kagent between API versions.** Translating a platform to itself is a migration. This renders
the official `promql` agent (Kagent 0.x, `v1alpha2`) as `v1alpha3`:

```bash
agent-translator translate fixtures/kagent/v1alpha2/promql-agent.yaml --to kagent -O api_version=v1alpha3 -o out
```
```
[lossy] kagent-fields-dropped agents[promql-agent] v1alpha2-only fields ['declarative'] not representable in v1alpha3
[info] written out/kagent.yaml Kagent v1alpha3
```

**Multi-agent: ADK → Azure.** A *synthetic* ADK team that researches in parallel
([`fixtures/adk/synthetic/parallel_research/`](fixtures/adk/synthetic/parallel_research)):

```bash
agent-translator translate fixtures/adk/synthetic/parallel_research/root_agent.yaml --to azure -o out
```
```
[manual] model-unmapped agents[papers_researcher].model 'gemini-2.5-flash' (google) copied as-is; Foundry needs the name of a deployment that serves it
[lossy] collaboration-unsupported agents[research_team] prompt agents cannot call sub-agents (parallel); ['news_researcher', 'papers_researcher'] are emitted as independent agents. Expose them over A2A ...
```

Foundry prompt agents cannot delegate to sub-agents, so the team is emitted as three independent agents
and the report tells you what to do to rebuild the delegation. Targeting `--to kagent` instead keeps the
sub-agents as tools of the parent (`collaboration-mode`, `lossy`).

**Bedrock API output as input, and as output.** `aws bedrock-agent get-agent` JSON is a valid source
(a *synthetic* sample: [`fixtures/bedrock/api/getagent-minimal.json`](fixtures/bedrock/api/getagent-minimal.json)),
and Bedrock can be written as a CloudFormation template or as ready-to-run API request files:

```bash
agent-translator translate fixtures/bedrock/api/getagent-minimal.json --to kagent -o out
agent-translator translate fixtures/bedrock/cfn/hr-assistant.yaml --to bedrock -O format=sdk -o out
ls out   # 01-HRAgent-create-agent.json ... 07-HRAgent-create-agent-alias.json README.md
```

The request files are numbered in execution order and can be fed to
`aws bedrock-agent <operation> --cli-input-json file://<file>`; ids only known after a previous call are
`REPLACE-WITH-…` placeholders, listed in the generated `README.md`.

**Agent Spec as a bridge.** Any platform can be translated to or from Agent Spec, which in turn has
adapters to LangGraph, AutoGen, CrewAI, OpenAI Agents and Microsoft Agent Framework:

```bash
agent-translator translate fixtures/agentspec/howto_swarm.yaml --to kagent -o out   # a Swarm of three agents
agent-translator translate fixtures/kagent/v1alpha3/assistant-inline.yaml --to agentspec -o out
```

[docs/MATRIX.md](docs/MATRIX.md) shows the worst report severity of **every fixture translated to every
target**, which is the quickest way to see how well a given pair works.

### Reading the report

Every translation prints a report (and writes it as JSON with `--report report.json`). Items have a
severity, from harmless to blocking:

| severity | meaning |
|---|---|
| `info` | something notable, nothing lost |
| `warning` | translated, but check it |
| `lossy` | semantics were lost or approximated (e.g. a tool kind the target lacks) |
| `manual` | needs a human action the tool cannot do (create a Secret, pass an IAM role ARN, fill in a model) |
| `error` | the target file could not be written |

The final `status` is `ok`, `partial` (something `lossy` or `manual` remains) or `failed`. `--strict`
makes the command exit non-zero on `lossy` or worse, which is what you want in CI. Information that has
no equivalent on the target is kept in the IR's `extensions`, so a round trip A → IR → A preserves the
core fields and A's own platform-specific settings (this is covered by the test suite).

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

Ids not in the table are copied unchanged. The table is a snapshot, so very new ids may be unknown.

### How well is it tested?

Every adapter is tested against official sample files, and every output is validated against the target
platform's own schema or SDK where one exists (Kagent CRDs, the `AWS::Bedrock::Agent` CloudFormation
schema and botocore's request models, `azure-ai-projects`, `pyagentspec`, ADK's `AgentConfig.json`).
The cross-platform matrix runs every fixture through every target. This validates the *shape* of the
output; it cannot tell you the agent behaves identically on the other platform, which is why the report
lists what changed.

## 2. Fetch (read-only)

Bring an agent that already exists on a platform into local files, in a format the adapters read:

```bash
# fetch talks to the platforms through their SDKs, which are optional extras so that plain
# translation stays dependency-light: boto3 (`fetch bedrock`) and azure-ai-projects +
# azure-identity (`fetch azure`), google-auth (`fetch adk`). `fetch kagent` only needs `kubectl`.
# `deploy` uses the same SDKs.
# --all-extras installs them all; `uv sync --extra X` alone would also uninstall the dev tools.
uv sync --all-extras
agent-translator fetch bedrock AGENT_ID[:VERSION] -O region=us-east-1 [-O profile=NAME]
agent-translator fetch kagent [NAMESPACE/]NAME [-O context=CTX] [-O api_version=v1alpha2]
agent-translator fetch azure AGENT_NAME[:VERSION] -O endpoint=https://<acct>.services.ai.azure.com/api/projects/<proj>
agent-translator fetch adk ENGINE_ID -O project=<project> -O region=europe-west1     # Vertex AI Agent Engine

# or fetch and translate in one step:
agent-translator translate AGENT_ID --fetch --from bedrock --fetch-option region=us-east-1 --to kagent
```

Fetching only *reads* (Bedrock `Get*`/`List*`, `kubectl get`, Azure `agents.get`) with your own
credentials (the AWS default chain, your kubeconfig, `DefaultAzureCredential`, Google Application Default
Credentials). It never writes to the platform and never reads Secrets (environment-variable values of an
Agent Engine are redacted). Agent Spec has no `fetch`: it is already local files.

`fetch adk` reads a Vertex AI Agent Engine (`reasoningEngines.get`). **Limit, verified against the real service:**
the API does not return the source of an engine that is already deployed (`sourceCodeSpec.inlineSource` comes back
empty), so `fetch adk` gives you the resource metadata as JSON (name, framework, identity, methods) plus a
`source-not-returned` report item, not a translatable agent. Keep the project you deployed from; `translate` that.

Verification status: tested against fakes (botocore's `Stubber`, a fake `kubectl`, fake Azure and Google clients);
Azure, Kagent and ADK/Google were also run once against a real Foundry project, a lab cluster and a Google Cloud
project. **Bedrock fetch has not been run against a real account yet**, and new AWS accounts can no longer create classic Bedrock agents to fetch.

## 3. Deploy (writes to your account — dry run by default)

Create an agent from files written by `translate`:

```bash
agent-translator translate my-agent/ --to kagent -o out
agent-translator deploy kagent out/kagent.yaml -O context=lab -O namespace=kagent        # plan only
agent-translator deploy kagent out/kagent.yaml -O context=lab -O check=remote            # + read-only checks
agent-translator deploy kagent out/kagent.yaml -O context=lab --apply                    # asks for confirmation
agent-translator deploy azure out/azure.yaml -O endpoint=https://<acct>.services.ai.azure.com/api/projects/<proj> --apply --yes
agent-translator deploy bedrock out/ -O region=eu-west-1 -O model=<model-id> -O role_arn=<role-arn> --apply
agent-translator translate my-agent/ --to adk -o adk-out
agent-translator deploy adk adk-out -O project=<project> -O region=europe-west1 --apply   # Agent Engine
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
- ADK: runs Google's own `adk deploy agent_engine` (it owns the packaging) after the same checks; unimplemented
  stubs in the generated `tools.py` are refused, and `check=remote` refuses a display name that already exists.
  `google-adk[gcp]` must be installed in that environment (it cannot share a lock with the Azure SDK extras; the
  `gcp` extra provides `agentplatform`, without which `adk deploy agent_engine` stops). For example:
  `uv run --with "google-adk[gcp]" agent-translator deploy adk …`. An
  Agent Engine bills while it exists; the undo command (a `DELETE` with the engine's id) is printed.
- Same extras and credentials as `fetch`. Bedrock needs `cloudformation:CreateStack`/`DescribeStacks`/
  `ValidateTemplate`, `iam:PassRole` for the agent role, and the `bedrock:*` actions CloudFormation
  performs for the resources in the template.

Verification status: `deploy azure`, `deploy kagent` and `deploy adk` were each run once against a real environment (a disposable
Foundry project, a lab K3s cluster and a Google Cloud project: create, read back, cleanup). **`deploy bedrock`
has only been tested against fakes** (a real attempt was blocked: AWS closed Bedrock Agents Classic to new accounts on 2026-07-30, so a stack can only create agents in accounts that already used the service; AgentCore is not supported), so try it on a throwaway target first. Details in [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Development

```bash
uv sync --all-extras          # NOTE: `uv sync --extra X` alone removes the dev tools
uv run pytest
uv run ruff check .
uv run agent-translator ir-schema    # JSON Schema of the IR, published as schema/ir.schema.json

# Load every emitted ADK project with the real google-adk (kept out of --all-extras: google-adk
# needs openai<3, which conflicts with the Azure SDK extras)
uv run --all-extras --with "google-adk[extensions]" --with mcp pytest tests/test_adk_real_loader.py
```

[AGENTS.md](AGENTS.md) describes the architecture and conventions for contributors (human or AI).

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

We borrowed what fits (`requires_confirmation` on tools, remote agents as collaborators) and ship an
Agent Spec adapter so that it can act as a bridge to those runtimes. Details and the criteria for
revisiting this decision are in [DESIGN.md](DESIGN.md#agent-spec-evaluation-after-m2).

License: Apache-2.0
