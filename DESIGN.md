# agent-translator — Design

Translator of AI agent definitions between platforms, through a common language (the IR).

Initial platforms: **AWS Bedrock Agents**, **GCP ADK**, **Azure OpenAI / Foundry Agents**, **Kagent**.

> Status: draft v0.1. The field mappings were first written from memory and **must be validated against each platform's current documentation/schemas** before being relied on (marked with ⚠️).

## 1. Principles

1. **Always two steps**: `source → IR → target`. With N platforms there are 2N adapters instead of N·(N-1) translators.
2. **Lossy translation, made explicit**: no field is dropped silently. Every translation returns a `TranslationReport`.
3. **Small core + extensions**: the IR only models what is common to ≥2 platforms; anything specific goes in `extensions[<platform>]` and is preserved for round trips.
4. **Declarative first**: parsing arbitrary code (ADK Python) is fragile. A well-defined subset is supported (ADK Agent Config YAML and a recognizable code pattern); as a target, code is generated from templates.
5. **No cloud calls by default**: the translator works on files (JSON/YAML/Python). Optionally, `fetch` and `deploy` use the platform SDKs to read/create agents, but they are not part of the core.
6. **Align with standards**: evaluate [Oracle's Agent Spec](https://oracle.github.io/agent-spec/) as an alternative serialization of the IR (exporter/importer), without coupling the IR to it.

## 2. Architecture

```
 source file ──► [source Adapter].parse() ──► AgentIR ──► [target Adapter].emit() ──► target files
                          │                        │                    │
                          └──── warnings ──────────┴──── lossy ─────────┴──► TranslationReport
```

```
agent_translator/
  ir/            # pydantic IR models + validation + (de)serialization
  adapters/
    base.py      # Adapter Protocol: detect(), parse(), emit()
    kagent/      # CRD YAML
    bedrock/     # JSON (CreateAgent + action groups + KBs + collaborators)
    adk/         # Agent Config YAML + Python code generator
    azure/       # Foundry/Assistants JSON
  report.py      # TranslationReport, Loss, Warning
  registry.py    # adapter discovery (entry points → external plugins)
  cli.py         # translate, validate, inspect, list-platforms
tests/
  golden/        # source fixture ↔ IR ↔ target pairs
```

### Adapter contract

```python
class Adapter(Protocol):
    name: str                                   # "kagent", "bedrock", ...
    def detect(self, path: Path) -> bool: ...   # format autodetection
    def parse(self, path: Path) -> tuple[AgentIR, Report]: ...
    def emit(self, ir: AgentIR, out: Path) -> Report: ...
    capabilities: Capabilities                  # which IR features it supports
```

Adapters are registered through *entry points* (`agent_translator.adapters`), so a new platform is a separate package that does not touch the core.

## 3. The IR (`AgentIR`)

Pydantic v2 models. Published and versioned JSON Schema (`agent-ir.schema.json`, `irVersion: "0.1"`).

```yaml
irVersion: "0.1"
agents:                      # one file can hold a multi-agent system
  - id: support-router       # stable identifier, unique within the document
    name: support-router
    description: Routes support queries
    instructions: |          # system prompt (text)
      You are a support agent...
    model:
      provider: anthropic    # anthropic | openai | google | amazon | other
      id: claude-sonnet-5-5  # native id; see "Models" below
      params: {temperature: 0.2, max_tokens: 2048, top_p: null}
    tools:
      - id: kb-search
        kind: mcp            # mcp | function | openapi | knowledge | builtin | agent
        ...
    sub_agents: [billing, tech]   # refs to ids (collaboration / delegation)
    collaboration: {mode: router} # none | supervisor | router | sequential
    memory: {enabled: true, ttl_seconds: 900}
    guardrails: [...]
    io: {input_schema: null, output_schema: null, output_key: null}
    extensions:
      bedrock: {...}
      kagent: {...}
resources:                   # shared resources, referenced by id
  mcp_servers: [...]
  knowledge_bases: [...]
  functions: [...]           # function signatures with JSON Schema
```

### Tool types (`tools[].kind`)

| kind | Fields | Notes |
|---|---|---|
| `mcp` | `server_ref`, `tool_names[]` | Most portable format; the recommended convergence point |
| `function` | `name`, `description`, `parameters` (JSON Schema), `impl_ref` | `impl_ref` points to a Lambda ARN, Python function, URL… it cannot be translated, only preserved |
| `openapi` | `spec` (inline or URL), `operations[]` | Bedrock action groups, Azure OpenAPI tool |
| `knowledge` | `kb_ref`, `top_k` | Bedrock KB, Azure file_search / AI Search, ADK retrieval |
| `builtin` | `name` ∈ {code_interpreter, web_search, file_search, …} | Table-based mapping; many platforms have no equivalent |
| `agent` | `agent_ref` | Agent exposed as a tool (Kagent `type: Agent`, ADK `AgentTool`) |

### Models

Model ids are not portable (`anthropic.claude-…` on Bedrock, a deployment name on Azure, `gemini-…` on ADK, a `ModelConfig` on Kagent). The IR stores a logical `provider` + `id`, and each adapter resolves it with a **configurable model table** (`models.yaml`): logical → native id per platform. If there is no entry, the adapter emits the id as-is together with a `model-unmapped` warning.

## 4. Field mapping per platform ⚠️

| IR | Kagent (`Agent` CRD) | Bedrock Agents | ADK | Azure (Foundry / Assistants) |
|---|---|---|---|---|
| `name` | `metadata.name` (DNS-1123) | `agentName` | `name` (Python identifier) | `name` |
| `description` | `spec.description` | `description` | `description` | `description` |
| `instructions` | `spec.declarative.systemMessage` | `instruction` (min. 40 chars) | `instruction` | `instructions` |
| `model` | `spec.declarative.modelConfig` → ref to a `ModelConfig` | `foundationModel` | `model` | `model` (*deployment* name) |
| `model.params` | in the `ModelConfig` | `promptOverrideConfiguration` | `generate_content_config` | `temperature`, `top_p` |
| tools `mcp` | `tools[].mcpServer {name, kind, toolNames}` | action group with `customControl`/Lambda (adaptation) or AgentCore Gateway | `McpToolset(...)` | `mcp` tool (Foundry) |
| tools `function` | — (via MCP) | action group `functionSchema` + Lambda | Python function / `FunctionTool` | `function` tool |
| tools `openapi` | — (via MCP) | action group `apiSchema` | `OpenAPIToolset` | `openapi` tool |
| `knowledge` | — (via MCP) | `knowledgeBases[]` | retrieval tool | `file_search` / AI Search |
| `sub_agents` | `tools[]` with `type: Agent` | `agentCollaboration` + collaborators | `sub_agents` | connected/agent tool (⚠️ depends on the API) |
| `memory` | `memory` (if present in that version) | `memoryConfiguration` | sessions/memory service | threads |
| `guardrails` | — | `guardrailConfiguration` | callbacks / safety settings | content filters on the deployment |
| `io.output_key` | — | — | `output_key` | — |

Notes:
- **Kagent (decided: v1alpha3 and v1alpha2)**. The adapter detects the version from `apiVersion`; when emitting, it is chosen with `-O api_version=v1alpha2|v1alpha3` (default v1alpha3). `--from kagent --to kagent -O api_version=…` works as a migration between versions. Layout: `adapters/kagent/{common,v1alpha2,v1alpha3,adapter}.py`. Fields of each version with no IR equivalent are kept in `extensions.kagent.<version>` and reported as `kagent-fields-dropped` when switching version.

  | | v1alpha3 (`api.kagent.dev`, Kagent ≥ 1.0) | v1alpha2 (`kagent.dev`, Kagent 0.x) |
  |---|---|---|
  | Agent | `Agent` = `template` (inline/ref to `AgentTemplate`) + `harnessRef` | `Agent` with `type: Declarative` and `declarative{}`; `BYO` cannot be translated (`byo-agent`) |
  | Prompt | `systemPrompt` / `systemPromptFrom` | `systemMessage` / `systemMessageFrom` |
  | Model | `modelConfig {name, namespace?}` | `modelConfig` (string, same namespace) |
  | MCP | `tools[].mcp {server, tools, requireApproval: bool}` | `tools[] {type: McpServer, mcpServer{toolNames, requireApproval: [names]}}` |
  | Sub-agent | `subAgent` → an `AgentTemplate` that runs inside the parent | `type: Agent` → another, independent `Agent` (A2A) |
  | Memory | in the `Harness` (`spec.kagent.memory`) → manual task when emitting | `declarative.memory {modelConfig (embeddings), ttlDays}` |
  | Output | `AgentTemplate` only for sub-agents | every agent as an `Agent` |

  Conversion between versions, with declared losses: `requireApproval` (list ↔ bool: `require-approval-coarsened`), sub-agent semantics (`subagent-semantics` / `subagent-standalone`), regenerated `ModelConfig` (`modelconfig-regenerated`; the existing Secret is reused) and the API group mapping (`kagent.dev` ↔ `api.kagent.dev`). The `kagent.dev/v1alpha1` API is rejected (`unsupported-api-version`). Tools with no equivalent (`function`, `openapi`, `knowledge`, `builtin`) are dropped with `tool-unsupported`: they must be exposed as an MCP server.
- **ADK**: two emission modes: (a) *Agent Config YAML* when every tool is supported; (b) Python code (`agent.py`) from Jinja templates when there are function tools.
- **Azure**: ⚠️ the Assistants API is in transition towards Responses/Foundry Agents. Decide the supported target for v1 (proposal: Foundry Agent Service) and isolate the difference in the adapter.

## 5. Translation report

Every run produces a `TranslationReport` (JSON + console summary):

```json
{
  "source": "bedrock", "target": "kagent",
  "status": "partial",
  "items": [
    {"severity": "lossy",   "code": "guardrail-dropped", "path": "agents[0].guardrails[0]",
     "message": "Kagent has no native guardrails"},
    {"severity": "manual",  "code": "impl-missing", "path": "agents[0].tools[2]",
     "message": "Lambda arn:aws:... is not portable; implement it as an MCP server"},
    {"severity": "info",    "code": "model-mapped", "path": "agents[0].model"}
  ]
}
```

Severities: `info` < `warning` < `lossy` (semantics were lost) < `manual` (needs human action) < `error` (cannot be emitted). The `--strict` flag fails on `lossy` or worse.

## 6. CLI

```bash
agent-translator translate --from bedrock --to kagent agent.json -o out/
agent-translator translate agent.yaml --to adk            # --from autodetected
agent-translator inspect agent.json                       # shows the IR
agent-translator validate ir.yaml                         # validates against the schema
agent-translator list-platforms                           # adapters + capabilities
```

Common options: `--models models.yaml`, `--strict`, `--report report.json`, `--format {yaml,json}`.

## 7. Testing strategy

- **Golden files**: real fixtures per platform (`tests/golden/<platform>/*.{json,yaml}`) and their expected IR.
- **Round trip**: `X → IR → X` must be idempotent for core fields; `extensions` are preserved.
- **Cross matrix**: a test parametrized over every (source, target) pair with a "canonical" agent that checks the expected report.
- **Output validation**: Kagent against the CRD (`kubectl --dry-run=client` or JSON Schema), Bedrock/Azure against the SDK schemas (`boto3`/`azure-ai-projects`, dev only), ADK by loading the YAML with `google-adk` in an optional test.
- Property tests (hypothesis) on the IR: stable serialization, unique ids, resolved refs.

## 8. Stack

Python ≥ 3.11 · pydantic v2 · ruamel.yaml (preserves comments) · Jinja2 (code templates) · typer (CLI) · pytest + hypothesis · uv/ruff.

## 9. Roadmap

| Milestone | Content |
|---|---|
| M0 ✅ | Skeleton, IR v0.1, `TranslationReport`, CLI `inspect/validate/translate` (publishing the IR JSON Schema was still missing) |
| M1 ✅ | **Kagent v1alpha3 + v1alpha2** adapter (parse + emit, migration between versions), official fixtures and validation against the CRDs |
| M2 ✅ | **Bedrock** adapter (CloudFormation: parse + emit, action groups, KBs, collaborators) → first Bedrock ↔ Kagent pair. Pending then: `GetAgent`/SDK JSON, model table |
| M3 ✅ | **ADK** adapter (Agent Config YAML + code generator) |
| M4 ✅ | **Azure/Foundry** adapter |
| M5 ✅ | Full cross matrix, Agent Spec adapter |
| M6 ✅ | Read-only `fetch` (Bedrock, Kagent, Azure) |
| M7 ✅ | `deploy` (Kagent, Azure, Bedrock via CloudFormation), dry run by default |
| M8 ✅ | `agentcore` target (emit-only, CloudFormation `AWS::BedrockAgentCore::Harness`) |
| M9 ✅ | `langgraph` target (emit-only, Python project) |
| M10 ✅ | `crewai` adapter (read and write JSON crew projects) |
| M11 ✅ | `llamaindex` target (emit-only, Python project) |

## 10. Decisions taken

1. **Own IR** in v0.1; Agent Spec was re-evaluated after M2 (2026-11-16): the own IR is kept and Agent Spec joins as one more adapter (see "Agent Spec evaluation").
2. **Azure**: supported target = **Foundry Agent Service**.
3. **Scope**: v1 = single agents; multi-agent systems afterwards (the IR already provides for them with `sub_agents`/`collaboration`).
4. **Files only** at first; `fetch` (read-only) and `deploy` (create-only, dry run by default) were added later as separate layers (see M6/M7 below).
5. **License**: Apache-2.0.
6. **Repository language**: English (code, docs, notes). The maintainer talks to coding agents in Spanish, but nothing in the repository is written in Spanish.

## 11. Fixture policy

Everything is tested with **real files**: first, official sample artifacts of each platform (samples/docs repositories), copied into `fixtures/<platform>/` with their origin (URL + commit) and license in `fixtures/<platform>/SOURCES.md`. If there is no official sample for a case, one is written by hand and marked `synthetic` in `SOURCES.md`.


## Bedrock adapter (M2) — decisions

- **Format**: CloudFormation templates with `AWS::Bedrock::Agent` (the only official declarative form). Intrinsics (`!Ref`, `!Sub`, `!GetAtt`…) are read in their long JSON form, so that they travel in `extensions`.
- **Values unknown offline**: `!Ref` to a parameter with a `Default`, and `!Sub` over them, are resolved (this is how the model id is obtained). The original intrinsic is kept in `extensions.bedrock.intrinsics` and reused when emitting if the value did not change.
- **References to resources the emitted template does not define** (IAM role, Lambda, S3 bucket, KB, alias of another stack) → turned into *Parameters* with a `reference-externalised` warning (manual). The emitted template never has dangling references (test).
- **Mapping**: action group with `ApiSchema` → `openapi`; with `FunctionSchema` → one `function` per function (JSON Schema); `AMAZON.UserInput`/`CodeInterpreter` → `builtin`; `KnowledgeBases` → `knowledge`; `AgentCollaborators` → `sub_agents` + `collaboration.mode` (`SUPERVISOR`→supervisor, `SUPERVISOR_ROUTER`→router).
- **Collaborators**: if the alias points to an agent in the same template it is resolved to `sub_agents`; otherwise (the usual case in the official samples) it stays in `extensions.bedrock.unresolvedCollaborators` with `collaborator-unresolved` (manual). When emitting, each agent generates an `AgentAlias` and the sub-agents are linked through `GetAtt`. Cycles are rejected (`collaboration-cycle`).
- **No equivalent**: MCP tools (`tool-dropped`, lossy), `top_k` (set at invocation time), guardrails from other platforms, `sequential` mode. Long descriptions are truncated to the schema maximums with `text-truncated`.
- **Validation**: each agent's `Properties` (fixtures and output) against the official `AWS::Bedrock::Agent` schema. This caught a real bug (descriptions > 200 characters).
- **Limitations**: there is no model table at this stage (non-Bedrock ids ask for a parameter, `model-required`); conditions (`Conditions`) and macros (`Transform`) are not expanded; the content of OpenAPI schemas in S3 is unknown (`schema-external`).


## Agent Spec evaluation (after M2)

Source: [oracle/agent-spec](https://github.com/oracle/agent-spec) (Apache-2.0, active; latest release 26.3.1, JSON schema 26.4.0 on `main`), tried with `pyagentspec` 26.3.1 on the 21 agents of our 16 fixtures (Bedrock + Kagent).

**Decision**: keep the own IR; add Agent Spec as an adapter (import/export) in M5.

Why not adopt it as the IR:
- `Agent` requires `llm_config` and `system_prompt`. 10 of 21 agents have no resolvable model (external ModelConfig/prompt) and 2 have no instructions; our IR represents them with warnings.
- There is no LLM configuration for Bedrock, Azure Foundry or Kagent (only OpenAI, OpenAI-compatible, Gemini, OCI GenAI, Ollama, vLLM). 10 agents use a provider with no class.
- Memory, guardrails and knowledge bases (RAG) do not exist as concepts; `Datastore` only covers databases. They would end up in free-form `metadata`, unstandardized.
- It is a specification of *portable execution*, not of *deployment* configuration: IAM roles, aliases, ModelConfig/Secret and `AgentTemplate` are out of scope.
- Its adapters cover LangGraph, AutoGen, CrewAI, OpenAI Agents, Microsoft Agent Framework and WayFlow; none of our four platforms.

Why yes as an adapter: going through Agent Spec gives, almost for free, a bridge to those six runtimes. A workable mapping: `function`→`ServerTool`/`ClientTool`, `mcp`→`MCPToolBox`, `openapi`→one tool per operation, multi-agent→`ManagerWorkers`/`Swarm`.

Ideas we do adopt in the IR (to be implemented at the time):
1. ✅ `requires_confirmation` as a first-class field on `Tool` (`True`/`False`/`None`). Bedrock `RequireConfirmation` and Kagent `requireApproval` fill it; Kagent v1alpha2's per-tool list stays in `extensions` for the same version. On Bedrock it only applies to functions; for other types it is reported with `confirmation-dropped`.
2. ✅ `AgentSpec.remote_agents` (`RemoteAgent`: id, name, description, instruction, ref, extensions) for collaborators outside the IR. Bedrock (alias outside the template, warning `collaborator-remote`) and Kagent (`Agent`/`subAgent` without a resource in the file) fill it. They are emitted as-is on the same platform; across different platforms, Kagent drops them with `remote-agent-dropped` (lossy) and Bedrock asks for the alias with `remote-agent-ref-required` (manual).
3. If workflows (flows with nodes) are ever needed, use its vocabulary (`Flow`, `Node`) instead of inventing another one.

Revisit if this changes: Agent Spec adding memory/KB/guardrails or LLM configurations for Bedrock/Azure.


## ADK adapter (M3) — decisions

- **Input**: `root_agent.yaml` and the files it references (`config_path`, resolved recursively and without cycles). Validated against `AgentConfig.json` by `agent_class`.
- **Output**: one folder per root agent (`<pkg>/root_agent.yaml` + one YAML per sub-agent) and, when YAML is not enough, `tools.py` (stubs generated from the JSON Schema, `function-stub-generated`), `models.py` (`LiteLlm` for non-Gemini models, `model-via-litellm`) and the OpenAPI spec as JSON. All generated Python is checked with `ast.parse`.
- **IR**: new `Collaboration.mode` values: `handoff` (an LlmAgent's `sub_agents`), `parallel`, `loop` (+ `max_iterations`); `sequential` already existed. `AgentTool` → `supervisor`. New `Collaboration.instructions` (sub-agent → when to delegate; Bedrock `CollaborationInstruction`, Kagent `subAgent.description`, description in ADK).
- **ADK limitations discovered**: there is no `require_confirmation` in the YAML (`confirmation-dropped`); `OpenAPIToolset` cannot be declared in YAML (Python is generated); stdio MCP servers require `ADK_ALLOW_CONFIG_STDIO_MCP_SERVERS=1` (`mcp-stdio`, manual); `model_code` only accepts a reference to Python; `agent_class: Workflow` graphs (`edges`) are not in the schema and are not translated (`workflow-graph-unsupported`); callbacks and custom classes are kept in `extensions.adk`.
- **Other adapters**: Bedrock and Kagent report `collaboration-mode` (lossy) when receiving `sequential/parallel/loop/handoff`.
- **Unresolved at the time**: no model table (Bedrock ids → `bedrock/<id>` via LiteLLM, unverified); the list of ADK builtin tools is incomplete (unknown names → `tool-unknown`).


## Azure adapter (M4) — decisions

- **There is no ARM/Bicep resource for the agent**: Foundry Agent Service receives the definition through the data API (SDK/REST/`azd`). That is why two file formats are supported, chosen when emitting with `-O format=foundry` (default) or `-O format=agent-framework`:
  - `foundry`: `host: azure.ai.agent` / `kind: prompt` services of `azure.yaml` (several agents per file) and the bare `kind: prompt` definition (YAML/JSON, also `{"definition": {...}}`). Validated with the `azure-ai-projects` SDK classes (optional extra `validate-azure`).
  - `agent-framework`: Microsoft Agent Framework `kind: Prompt`, one file per agent; validated with `agent-framework-declarative`.
- **Mapping**: `function`→function (with JSON Schema), `mcp`→mcp + `resources.mcp_servers` (`require_approval` always/never → `requires_confirmation`), `openapi`→openapi, `azure_ai_search`/`file_search`→knowledge, `a2a`→`remote_agents`, the rest (`web_search`, `code_interpreter`…)→builtin. Whatever has no IR field is kept in `extensions.azure` and re-emitted.
- **Limitations**: a `prompt` agent cannot call sub-agents (`collaboration-unsupported`, lossy; children are emitted as independent agents: they must be exposed through A2A or a workflow used); there is no `top_k`/`max_tokens` in Foundry (`param-dropped`); memory and guardrails are not part of the agent definition; toolboxes, skills and `harness` are Foundry resources with no equivalent (they are kept); the model's `=Env.X` expressions in Agent Framework are kept unresolved (`model-expression`, manual); `hosted`/`workflow`/`voice` are not translated.
- **Unresolved at the time**: no model table (the id is copied as-is and a warning is given if it is not OpenAI); the list of tool types comes from SDK 2.11.


## Agent Spec adapter and cross matrix (M5) — decisions

- **`agentspec` adapter** (YAML/JSON, `$component_ref`/`$referenced_components`). Input: `Agent`, `ManagerWorkers` (manager + workers → `supervisor` with `collaboration.instructions` = the worker's description; `A2AAgent` workers → `remote_agents`) and `Swarm` (`relationships` → `sub_agents`, `handoff` mode). `Flow`, `OciAgent` and a root `A2AAgent` are reported (`component-unsupported`, manual). Output: one file per root agent (`ManagerWorkers` if there are sub-agents in supervisor/router mode, `Swarm` if `handoff`; `sequential/parallel/loop` are a `Flow` in Agent Spec and are reported as lossy `collaboration-mode`).
- **Mapping**: `function`→`ServerTool`/`ClientTool`/`RemoteTool` (JSON Schema ↔ property list; an optional parameter is emitted as `anyOf [T, null]` with `default: null`), `mcp`→`MCPToolBox` (SSE/StreamableHTTP; stdio is dropped, manual), `openapi`→one `RemoteTool` per operation, `knowledge`→a search `ServerTool` without an executor (`knowledge-as-tool`, lossy), `requires_confirmation`→a tool field (version ≥ 25.4.2, ≥ 26.1.2 on toolbox). LLM configuration: `OpenAiConfig` / `GeminiConfig`; any other model → `OpenAiCompatibleConfig` with an endpoint to fill in (`model-via-openai-compatible`, manual). Shared LLMs are emitted once in `$referenced_components`. `agentspec_version` is computed as the minimum that supports what was emitted.
- **Validation**: `pyagentspec` 26.3.1 (optional extra `validate-agentspec`). It does not load the `A2AAgent` format of the most recent official example nor `agentspec_version: 26.4.0`; the tests document this and avoid it.
- **Cross matrix** (`tests/test_matrix.py`): 60 fixtures × 7 targets/variants. Every output is validated with the target platform's schema/SDK and parsed again with its adapter; each fixture must be detected by exactly one adapter. Summary in `docs/MATRIX.md` (`scripts/gen_matrix.py`). It found three real bugs, since fixed: MCP protocol vocabulary (the IR uses `sse | streamable-http | stdio`; Kagent uses `SSE`/`STREAMABLE_HTTP`), Bedrock instructions shorter than 40 characters (padded with an `instruction-padded` warning) and ambiguous detection between ADK and Agent Framework.
- **IR schema** published at `schema/ir.schema.json` (command `agent-translator ir-schema`, with a test that it is up to date).
- **Postponed at the time**: `--fetch`/`--deploy` against clouds (the maintainer's decision); model table (done, see below).


## Model table — decisions

- **`agent_translator.models` module**: recognizes a model id in any platform format (prefixes `bedrock/`, `vertex_ai/`..., regional profiles `us./eu./apac./global.`, `@date`, ids with and without a date) and adapts it to the target. It is applied once in each adapter's `emit` (`adapt_ir`), on a copy of the IR; the emitters do not change.
- **Data**: `catalog_data.py` is generated by `scripts/gen_model_catalog.py` from the LiteLLM catalog (MIT, commit pinned in `LITELLM_COMMIT`), which is the most complete source of ids per provider; there is no single official list. 75 families: Claude (Anthropic/Bedrock/Vertex), GPT-4o/4.1/5.x (OpenAI/Azure), Gemini (Gemini API/Vertex), Nova, Llama and Mistral on Bedrock. One wrong catalog entry was discarded (the Claude 3.7 Sonnet date).
- **Policy** (`-O model_policy=`): `keep`, `rehost` (default) and `tier`. By default the model is never changed: only the provider hosting the same model, and if the target does not serve it, a same-size alternative is suggested without applying it. `tier` substitutes the most recent model of the same size (large/medium/small) and declares it `lossy`.
- **What each target serves unchanged** (`HOSTS`): Bedrock its own ids; Kagent direct Anthropic/OpenAI/Gemini; ADK Gemini and, via LiteLlm, Anthropic/OpenAI/Bedrock; Azure Foundry OpenAI names (the id is the *deployment* name); Agent Spec OpenAI/Google/Anthropic.
- **Known limits**: *tiers* are a name-based judgement, not a benchmark; the catalog is a snapshot of one commit and new ids take time to appear (it must be regenerated); models outside the table (e.g. Titan, local models) pass through unchanged; availability per region and quota are not checked; on Bedrock recent models may require an inference profile id (a warning is given).


## Bedrock `GetAgent` (API/CLI JSON) — decisions

- **Input**: besides CloudFormation, the `bedrock` adapter reads the output of `aws bedrock-agent get-agent` (`{"agent": …}`, also `get-agent-version`) as JSON or YAML. It is detected by shape (`agent.agentName`, no `Resources`).
- **Bundle**: `GetAgent` only returns the agent; action groups, knowledge bases and collaborators come from other calls. Own convention: a document with `agent` plus `agentActionGroups`, `agentKnowledgeBases` and `agentCollaborators` (lists of what the `Get*` calls return, with or without the envelope). The `List*` summaries are accepted but a warning is given (`getagent-summaries`, `action-group-incomplete`): they carry no schemas or executors.
- **Implementation**: the API objects have the same fields as the CloudFormation properties in camelCase; they are converted into an `AWS::Bedrock::Agent` resource (`getagent.py`) and go through the existing parser, so the IR is identical to that of a template. Checked against botocore's service model: the only naming difference is `parentActionSignature` ↔ `ParentActionGroupSignature`; `parentActionGroupSignatureParams` does not exist in CloudFormation (dropped with a `lossy` warning). Maps with user-defined keys (`parameters`) are not transformed.
- **Discarded**: ids, ARNs, status, dates, `clientToken`, `failureReasons` (a warning is given if present) and `recommendedActions` (service fields with no equivalent).
- **Output**: CloudFormation and, with `-O format=sdk`, API request files (see below).
- **Tests**: synthetic fixtures (there are no public samples) validated with botocore (extra `validate-bedrock`), included in the cross matrix.


## Bedrock SDK output (`-O format=sdk`) — decisions

- **What it writes**: files numbered in execution order, ready for `aws bedrock-agent <op> --cli-input-json file://NN-….json`: `create-agent`, one `create-agent-action-group` per group, `associate-agent-knowledge-base`, `associate-agent-collaborator`, `prepare-agent` and `create-agent-alias`, plus a `README.md` with the commands. It sends nothing to any cloud.
- **Built from the CloudFormation template** the emitter already generates (`sdk.py`), so both outputs always agree. Conversion: PascalCase → camelCase (maps with user-defined keys, such as `parameters`, are left alone); `Ref` to parameters is replaced by its `Default`; `AutoPrepare` and the other stack-only switches are dropped.
- **Placeholders**: whatever is only known after a previous call (`agentId`, the alias ARN of a local collaborator) comes out as `REPLACE-WITH-…`, listed in the README and as `manual` (`sdk-placeholder`). Agents are ordered so that collaborators are created before their supervisors.
- **Validation**: each file is validated with botocore's input model for its operation (`ParamValidator`), for all CloudFormation fixtures and the API ones, and is in the cross matrix (this output is write-only: it is not parsed back). That validation caught that `SkipResourceInUseCheckOnDelete` also appears inside action groups.
- **Limits**: the calls are not chained (ids have to be copied by hand; for deployment use the CloudFormation template, see `deploy`); no new guardrails or knowledge bases are emitted (only existing ones are associated), as in CloudFormation.


## `fetch` (M6) — decisions

- **Read-only, into local files**: `agent-translator fetch <platform> <ref>` leaves a file in a format the adapter already knows how to read; `translate --fetch` chains both steps. Registered as `agent_translator.fetchers` entry points; each fetcher optionally receives a `client` so it can be tested with fakes.
- **Bedrock** (`boto3`, extra `fetch-aws`): `GetAgent`/`GetAgentVersion` + `List*`/`Get*` for action groups, knowledge bases and collaborators (with pagination), dumped into the GetAgent JSON *bundle*. Options `region`, `profile`; credentials from the default AWS chain.
- **Kagent** (`kubectl get`, no dependencies): the `Agent` and what it references (ModelConfig, RemoteMCPServer, AgentTemplate, agents used as tools), as multi-document YAML without `status` or server metadata. It never requests a `Secret`. A reference that does not exist in the cluster is a warning, not an error. Options `context`, `namespace`, `api_version`, `kubectl`.
- **Azure Foundry** (`azure-ai-projects` + `azure-identity`, extra `fetch-azure`): `agents.get` (latest version) or `get_version`, written as `{"name", "definition"}`. Project endpoint via `-O endpoint=` or `AZURE_AI_PROJECT_ENDPOINT`.
- **No fetch**: ADK and Agent Spec are local files by nature.
- **Network-free tests**: Bedrock with botocore's `Stubber` (validates requests and responses against the real service model; the test checks that only `Get*`/`List*` operations are invoked); Kagent with a fake `kubectl` executable that serves the official fixtures and records the calls (only `get`, never secrets); Azure with a fake client. Real pagination, IAM/RBAC permissions and network errors are only verified against the SDK contracts.
- **Real test (2026-11-17)**: `fetch azure` against a disposable Foundry project (an `AIServices` S0 account with no model deployments, deleted afterwards; expected cost 0). It found a bug the fakes did not cover (`translate` on the directory written by `fetch azure` produced a traceback); fixed in the Azure adapter. `fetch kagent` was also run against a real lab cluster the same day (it found the same class of bug for directories). Bedrock fetch has not been run against a real environment: AWS closed Bedrock Agents Classic to new accounts on 2026-07-30, so a fresh account has no agents to fetch (see `docs/VERIFICATION.md`).
- **Google (ADK on Vertex AI Agent Engine)**: registered as platform `adk`, like the adapter, so `translate --from adk --fetch` works. An Agent Engine is not a YAML file: it is a `ReasoningEngine` resource whose spec carries either a source archive (`sourceCodeSpec.inlineSource`) or a pickled object (`packageSpec`). **Verified against the real service (2026-11-18): the GET returns `sourceCodeSpec.inlineSource` empty (v1 and v1beta1), so a deployed engine cannot be translated back from the cloud**; fetch yields the metadata JSON plus a `source-not-returned` item. The extraction code stays for the case the API changes, untested. Plain REST with `google-auth` instead of the Vertex SDK, to avoid pulling `google-cloud-aiplatform` into the dependency set. Environment-variable values are redacted. Verified once against a real project (metadata only).
- **`deploy`**: see the next section.

## `deploy` (M7) — decisions

- **Same pattern as fetch**: `deploy <platform> <path>` with modules registered under `agent_translator.deployers`, each with `plan(path, options, client=None)` and `apply(plan, options, client=None)`; the injectable `client` allows testing them with fakes.
- **Safe by construction**: (1) the plan is always computed and printed, with no network except `-O check=remote` (reads only: existence, `ValidateTemplate`, `--dry-run=server`); (2) without `--apply` nothing is changed; with `--apply` there is an interactive confirmation or `--yes`, and with neither a terminal nor `--yes` it refuses; (3) **create-only**: anything existing is an error, never overwritten, updated or deleted (Azure only adds a version with `-O new_version=true`); (4) files with `REPLACE-WITH-…` are refused; (5) `Secret`s are never created or read; (6) after creating, how to undo it is printed.
- **Kagent**: `kubectl create` object by object (never `apply`), stops at the first failure; only `kagent.dev` resources (refuses `Secret` and foreign kinds).
- **Azure**: `agents.create_version(body={definition, description})` for `prompt` agents; the model *deployment* must already exist (deployments are not created: that is where the cost is).
- **Bedrock**: a **CloudFormation** stack from the emitted template, instead of chaining `Create*` calls: it is atomic (`OnFailure=ROLLBACK`), resolves the ids between agent/groups/alias by itself and is removed entirely with `delete-stack`. That is why the `-O format=sdk` output is not deployable. This closes the "calls are not chained" limit: deployment uses the template, not the SDK.
- **ADK / Agent Engine**: runs Google's own `adk deploy agent_engine` instead of re-implementing its packaging over the API (wrapper app, requirements, source upload), which changes with ADK and cannot be checked without an account. The tool contributes the safety rules: dry run by default, placeholders and unimplemented generated stubs refused, never `--agent_engine_id` (so nothing is updated), `check=remote` lists the region's engines and refuses an existing display name, and the printed undo is a `DELETE` on the created engine's id (parsed from the CLI output). `google-adk` is not a dependency: it needs `openai<3`, which cannot share a resolution with the Azure SDK, and the deploy needs its `gcp` extra (`agentplatform`). Verified once against a real project: the CLI accepted the emitted YAML-config folder and the printed `DELETE` removed the engine.
- **Tests**: botocore Stubber for CloudFormation (the test fails if an operation that was not queued is attempted, e.g. `create_stack` on an existing stack), a recording `kubectl` (only `create`, with `--context`/`-n`), a fake Azure client, and the CLI (dry run makes no calls, `--apply` without consent = exit 2). **Real test (2026-11-17)**: `deploy azure` against a disposable Foundry project (see `docs/VERIFICATION.md`); `deploy kagent` was run against a real lab K3s cluster in a throwaway namespace (found and fixed an undo hint that omitted the namespace). `deploy bedrock` has not been tried against a real environment.

## AgentCore target (M8) — decisions

- **Why**: a real `deploy bedrock` (2026-11-20) was refused by AWS: Bedrock Agents Classic is closed to new accounts since 2026-07-30 and AgentCore is the recommended replacement. The classic Bedrock adapter stays for existing agents.
- **Emit-only**: `agentcore` has no parser or `fetch` (`detect` is always false, `parse` reports `parse-unsupported`); there is no AgentCore-specific deployer either, `deploy bedrock` creates the stack because it only runs CloudFormation (it accepts `role_arn` for `…ExecutionRoleArn` and names the stack after `HarnessName`). Reading harnesses back and fetching are future work; AWS documents no import from classic agents and only an export to Strands code.
- **Output**: a CloudFormation template, one `AWS::BedrockAgentCore::Harness` per agent. Required properties are `HarnessName`, `ExecutionRoleArn` (a parameter: `role-required`) and `Model`. Tests validate against the official resource schema (`fixtures/agentcore/schemas/`, extracted 2026-11-20 from the CloudFormation schema zip) with the same intrinsic-aware validator as Bedrock.
- **Mapping**: instructions → `SystemPrompt`; model + `temperature/top_p/max_tokens` (`top_k` only for Gemini) → `BedrockModelConfig` / `OpenAiModelConfig` / `GeminiModelConfig` (the last two need `ApiKeyArn`, a parameter, `secret-required`); `mcp` tools → `remote_mcp` (one per server; tool filters dropped as `tool-filter-dropped` because `AllowedTools` semantics for MCP tools are not documented, and an `AllowedTools` list would also block built-in tools); `function` → `inline_function` (the call is returned to the caller; Lambda `impl_ref` dropped as lossy); other tool kinds → `tool-unsupported` (manual).
- **Memory**: `Memory: {Disabled: {}}` unless the IR enables it, because the harness creates a managed memory by default (a billed resource); TTL maps to `EventExpiryDuration` clamped to 3-365 days.
- **Multi-agent**: no harness multi-agent mode is documented; sub-agents become separate harnesses and `collaboration-dropped` (lossy) is reported.
- **Model hosts**: `HOSTS["agentcore"]` serves Bedrock, OpenAI and Google ids as they are and rehosts other vendors (Claude by Anthropic API id) to Bedrock ids.
- **Real test (2026-11-21)**: a minimal agent deployed through `deploy bedrock` reached `READY` and was removed with `delete-stack` (harness and runtime gone). New accounts are not blocked, but `AmazonBedrockFullAccess` does not include `bedrock-agentcore:*`; creation needed Create/Get/Delete on Harness, AgentRuntime, AgentRuntimeEndpoint, Memory and WorkloadIdentity. `iam:PassRole` was not an issue (the user had IAM full access). Still untested: tools, non-Bedrock models, memory enabled, invocation, and the delete behaviour of a managed memory.

## LangGraph target (M9) — decisions

- **Emit-only**: a LangGraph agent is Python code, not a document. Parsing it back would mean interpreting arbitrary Python (loops, conditionals, custom nodes), so `detect` is always false and `parse` reports `parse-unsupported`. There is no `fetch`/`deploy` either: the platform deploys code, it has no agent object to read or create.
- **Output**: one self-contained module per root agent (`graph.py`, or `<root>.py` when there are several roots; no relative imports, because the LangGraph server loads files by path), `langgraph.json` and `requirements.txt` (only the provider packages the models need). Every generated module is `ast.parse`d before it is written.
- **API choice**: `langchain.agents.create_agent` (LangChain/LangGraph 1.x; `langgraph.prebuilt.create_react_agent` is deprecated in its favour) and `init_chat_model("provider:id", **params)`. Providers by vendor: anthropic, openai, google (`google_genai`), Bedrock ids (`bedrock_converse`). `HOSTS["langgraph"]` serves those ids as they are and rehosts others to Anthropic/OpenAI/Google ids. Unknown models are copied and reported (`model-unmapped`); a missing model reads `AGENT_MODEL` from the environment (`model-required`). `top_k` is emitted only for Anthropic and Google models.
- **Multi-agent**: supervisor/router (and `none` with sub-agents) → each sub-agent is a `@tool` that invokes it, described by the collaboration instruction; `handoff` is the same but reported `handoff-as-tool` (control is not transferred); `sequential`/`parallel` → `StateGraph(MessagesState)` over the sub-agents (a workflow agent has no LLM, so its own instructions/model/tools are `workflow-agent-llm-dropped`); `loop` → one sequential pass (`loop-single-pass`). No `langgraph-supervisor` dependency.
- **Tools**: MCP → `MultiServerMCPClient` (`streamable_http` or `sse`; headers are not copied, `mcp-headers-dropped`); tool filters are kept as a name filter. Because MCP tools are fetched asynchronously, such a module exposes `async def make_graph()` instead of `graph`, and `langgraph.json` points at it. Function, knowledge and OpenAPI tools are typed stubs raising `NotImplementedError` (`function-stub-generated`, manual); builtin tools are dropped (`tool-unsupported`). Remote agents are stubs too.
- **Memory**: no checkpointer is generated (LangGraph Platform rejects custom ones and persists threads itself); enabled memory is reported as `memory-checkpointer`.
- **Tests**: `tests/test_langgraph.py` checks the generated source; the matrix runs every fixture through the target. `tests/test_langgraph_real_loader.py` loads the emitted modules with real `langgraph`/`langchain` and a fake chat model (so no keys or network), checking nodes, edges and that sequential/parallel graphs and supervisor tools run. It needs `uv run --all-extras --with langgraph --with langchain --with langchain-mcp-adapters` (CI job `langgraph-loader`; langchain is not added to the lock). Verified once against a real model (2026-11-25): a supervisor + sub-agent project with a hand-implemented `add` stub ran through the lab's agentgateway (vLLM `qwen3-8`, Anthropic Messages API; `langchain-anthropic` reads the gateway from `ANTHROPIC_API_URL` and the token from `ANTHROPIC_API_KEY`): the supervisor called `call_calc`, the sub-agent called `add`, and the answer was correct. **Not verified against the official LangGraph server in Kubernetes.**
- **Running it** (checked 2026-11-25 with `langgraph-cli[inmem]`, Python 3.12): `langgraph dev` loads the generated project with no LangSmith key (`LANGSMITH_TRACING=false`, `LANGGRAPH_CLI_NO_ANALYTICS=1`), lists the graph as an assistant named after the root agent, and a run reached the model (it failed only because the key was fake). It needs Python >= 3.11. The emitter also writes `.env.example` (one variable per provider used, `AGENT_MODEL` when the source has no model) and a `README.md` with these steps.
- **Self-hosting the official server** (web search, 2026-11-25; not tested by us): the docs describe a standalone server that reads `LANGGRAPH_CLOUD_LICENSE_KEY` and needs a LangSmith API key plus Postgres and Redis, with a Helm chart for Kubernetes. Sources disagree on whether a free tier may self-host it (the FAQ lists "free self-hosted", third parties say Enterprise licence). Confirm with LangChain before deploying it in a shared cluster; for a lab, `langgraph dev` or wrapping `graph.py` in a small service avoids the question. There is deliberately no LangGraph `deploy`.

## CrewAI adapter (M10) — decisions

- **Formats.** Read: JSON crew projects (`crew.jsonc` + `agents/*.jsonc`), standalone `agent.yaml/json`,
  classic `config/agents.yaml` + `tasks.yaml`. The classic Python `crew.py` is never executed or read
  (`python-config-not-read`). Declarative flows (`crewai.flow/v1`) are not agents and are not detected.
  Written: the JSON crew project, validated with CrewAI's own `json_loader` in `tests/test_crewai_real_loader.py`.
- **No IR changes.** A crew has agents *and tasks*; the IR has agents only. A sequential crew becomes a root
  workflow agent (mode `sequential`) over its agents; a hierarchical crew becomes a supervisor (the
  `manager_agent`, or a synthesized one for `manager_llm`). Each agent's task text is folded into its
  instructions (`compose`: role, backstory, goal, task, expected output).
- **Lossless round trip.** Persona, tasks, settings and unknown crew keys are kept in `extensions.crewai`.
  The emitter restores them only when the instructions still equal the composed text; otherwise it
  derives role/goal/backstory from the IR and synthesizes tasks (`task-synthesized`).
- **Models** are LiteLLM strings (`provider/model`); `HOSTS["crewai"]` allows all providers. Unknown ids are
  `manual` (`model-unmapped`). A hierarchical crew always needs a manager model: borrowed from a worker, else
  a flagged placeholder.
- **Not translated:** run-time delegation (`allow_delegation`), apps, knowledge sources, skills, A2A, headers
  of MCP servers (possible credentials), per-tool confirmation. Function/knowledge/OpenAPI tools become
  `tools/<name>.py` BaseTool stubs reported as `manual`.
- **Licensing.** `crewAI-examples` has no license, so no file from it is copied; fixtures are synthetic or
  from the MIT `crewai` package (`fixtures/crewai/SOURCES.md`).

## LlamaIndex target (M11) — decisions

- **Emit-only**, for the same reason as LangGraph: an agent is Python code (`FunctionAgent`), so there is
  no declarative document to parse, fetch or deploy.
- **Mapping.** LLM agent → `FunctionAgent`. A supervisor/router/handoff agent with sub-agents → an
  `AgentWorkflow` where the parent `can_handoff_to` the children (and children back to the parent unless
  the mode is `handoff`): this is a real handoff, not a tool call (`supervisor-as-handoff`, warning).
  Sequential/parallel/loop → a small `Pipeline` class emitted in the module (loop is one pass,
  parallel outputs are concatenated: both `lossy`). A workflow nested below another agent is flattened
  into its parent (`workflow-flattened`). `agent` tools become `FunctionTool`s that run the other agent.
- **Models.** `HOSTS["llamaindex"]`: Anthropic, OpenAI, GoogleGenAI and BedrockConverse (the LLM
  classes accept only the params they have; the rest are `param-dropped`). A missing or unknown model
  is `manual`: the agent is emitted without `llm=` so `Settings.llm` applies.
- **Tools.** Function/knowledge/OpenAPI → stubs (`manual`), MCP → `McpToolSpec(BasicMCPClient(url))`
  fetched in `build()`, filtered by tool name; headers are never copied.
- **Verification.** The real-loader test imports every emitted module with the real `llama-index`
  and calls `build()` (agents are validated by LlamaIndex). Modules with MCP tools or `GoogleGenAI` are
  skipped there because constructing them opens a network connection. A run against a real model has
  not been done.

## Platforms evaluated and not implemented (2026-11-26)

- **Langflow**: a flow is a visual graph (nodes/edges with embedded component code), not an agent
  definition; only the `Agent` node would map, and writing a valid flow back is fragile. Declined.
- **AutoGen**: in maintenance mode; Microsoft Agent Framework, its successor, is covered by Azure
  `format=agent-framework`.
- **OpenShift Lightspeed**: `OLSConfig` (`ols.openshift.io/v1alpha1`) configures the assistant (LLM
  provider, RAG, MCP servers); it has no per-agent instructions, sub-agents or tools. Its `mcpServers`
  schema changed between operator 1.0.9 and 1.0.10 and the feature is in preview. Possible future
  piece: generate the `mcpServers` fragment from the IR's MCP servers once the schema is stable.
- **Red Hat OpenShift AI / Llama Stack**: agents are created through the `/v1alpha/agents` API
  against a server configured by `run.yaml` / `LlamaStackDistribution`; there is no declarative
  per-agent file. A possible emit-only target (a Python script using the client) if agents are ever
  run there; deferred because the API is still `v1alpha`.
