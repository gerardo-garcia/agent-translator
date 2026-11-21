# Verification status: fetch and deploy

What has actually been exercised, and against what. Update this file whenever a real-environment test is run.

Legend: **real** = run against a live cloud/cluster; **fakes** = run in the test suite against injected fake clients (botocore `Stubber`, fake `kubectl`, fake Azure client).

| Platform | fetch | deploy |
|----------|-------|--------|
| Azure Foundry | **real** (once, 2026-11-17) + fakes | **real** (once, 2026-11-17) + fakes |
| AWS Bedrock | fakes only | fakes only (a real attempt on 2026-11-20 was blocked by AWS, see below) |
| Kagent | **real** (once, 2026-11-17) + fakes | **real** (once, 2026-11-17) + fakes |
| AWS Bedrock AgentCore | none (emit-only target) | **real** (once, 2026-11-21) through the Bedrock CloudFormation deployer; no fetch |
| GCP ADK (Agent Engine) | **real** (once, 2026-11-18; metadata only, see below) + fakes | **real** (once, 2026-11-18) + fakes |

## Real test: `fetch kagent` and `deploy kagent` (2026-11-17)

- Cluster: a single-node K3s lab cluster (v1.29) with kagent CRDs at `v1alpha2` (served and storage) and 16 existing agents in the `kagent` namespace. Access through a temporary kubeconfig (deleted afterwards).
- `fetch kagent` on an existing agent (`promql-agent`, read-only): wrote the Agent plus its ModelConfig, with no Secret, status or managedFields; the Secret appears only by name. `translate` to Azure and back to Kagent worked.
- `deploy kagent` into a new namespace `agtr-test` (the 16 existing agents were not touched; their count was unchanged afterwards):
  - `-O check=remote` (`--dry-run=server`): the real API server accepted ModelConfig, RemoteMCPServer and Agent.
  - `--apply --yes` created the three objects; the kagent controller set the Agent `ACCEPTED=True`, `READY=False` (the pod stays in `CreateContainerConfigError` until the model API key Secret exists, as the `secret-required` report item says; this tool never creates Secrets).
  - A second apply was refused by the server (`AlreadyExists`) and nothing was overwritten.
  - `fetch kagent agtr-test/prompt-agent-mcp-tools` read it back; the round trip differs from the emitted file only in namespace, key order and two server defaults (`terminateOnClose`, `runtime: python`).
- Bugs found and fixed:
  - `translate` on the directory written by `fetch kagent` failed with "could not autodetect format" (same class as the Azure one); the Kagent adapter now resolves a directory with a single YAML file.
  - The undo hint omitted `--context` and `-n`, so it would have targeted the wrong namespace; it now carries both.
- Cleanup: the namespace was deleted (which removed every object in it). Cost: none.
- Not covered: v1alpha3 (this cluster only has v1alpha2), `AgentTemplate`, Secrets, a multi-object failure part-way (a later object failing after earlier ones were created is reported but not rolled back).

## Real test: `deploy adk` and `fetch adk` on Vertex AI Agent Engine (2026-11-18)

- Setup: project with the Vertex AI API newly enabled and ADC credentials of the user; region `europe-west1`. Input: a minimal one-agent ADK project (`translate --from adk --to adk`, a single `root_agent.yaml`, Gemini 2.5 Flash, no tools). Cost: the engine existed about 15 minutes; expected cents.
- `deploy adk` (dry run with `-O check=remote`, then `--apply --yes`): the real `adk deploy agent_engine` accepted the YAML-config folder as emitted, and created the engine; the engine name was parsed from its output and the printed `DELETE` command removed it (GET then returned 404, list empty).
- Found: the first attempt failed with `No module named 'agentplatform'`; `google-adk` needs its `gcp` extra (`google-adk[gcp]`) for `adk deploy agent_engine`. The error now carries that hint and the docs say so.
- Found: `fetch adk` works but the API does **not** return the source of a deployed engine: `spec.sourceCodeSpec.inlineSource` is `{}` in both v1 and v1beta1. So fetch yields metadata JSON only, with a `source-not-returned` report item; an engine cannot be translated back from the cloud. The archive-extraction code is kept for the case the API starts returning it, but it is untested against the real service.
- Side effects checked: no buckets, no Artifact Registry repo, Cloud Build not enabled, no new APIs.
- Not covered: tools, sub-agents, MCP, service accounts, invoking the agent, and non-Gemini models (LiteLLM) on Agent Engine.

## GCP / ADK loader (2026-11-17)

No cloud call was made. The emitted ADK projects were loaded with the real `google-adk` 2.14.1 (`from_config`, the first step of `adk run` / `adk web`): `tests/test_adk_real_loader.py`, 28 loaded, 35 skipped because their reports already ask for the user's own code or manual work (stubs, Python functions, callbacks, stdio MCP, custom agent classes).

- Bug found and fixed: ruamel wrote `threshold: OFF` unquoted; PyYAML (YAML 1.1) read it as `False` and ADK rejected the config. All emitters now go through `yamlio.new_yaml()`, which quotes such strings (also helps Go-based Kubernetes readers).
- ADK refuses stdio MCP servers in agent configs (security policy), which matches the `mcp-stdio` report item.
- The model is never called: no GCP project, API or credentials are involved.

## Real test: `fetch azure` (2026-11-17)

- Setup: a disposable `AIServices` S0 account plus one Foundry project in a test resource group, no model deployments (expected cost 0). Role "Azure AI User" (id `53ca6127-db72-4b80-b1b0-d745d6d5456d`) was needed for the agent data plane.
- Result: fetch worked end to end. It found one bug the fakes did not cover: `translate` on the directory written by `fetch azure` crashed with `IsADirectoryError`. Fixed in the Azure adapter (`_resolve`) with a regression test.
- Findings about the platform:
  - Foundry projects are managed under `…/accounts/<name>/projects/<proj>`.
  - Deleting the account requires deleting the nested project first.
  - Purging a soft-deleted account needs a subscription-scope permission (`deletedAccounts/delete`); without it the account stays soft-deleted (not billed) until auto-purge.
- Cleanup: everything was deleted except the soft-deleted account `agtr-test-11170153`, which auto-purges on 2026-11-19. The account from the deploy test (`agtr-dep-11171141`) is in the same state; both can be purged by an admin. An admin can purge it earlier with `az cognitiveservices account purge -g <rg> -n agtr-test-11170153 -l westeurope`.
- The existing team Foundry projects were not used: listing agents returned 403 (no `agents/read`), and no roles were granted on shared resources.

## Real test: `deploy azure` (2026-11-17)

- Setup: a new disposable `AIServices` S0 account + project, and one `gpt-4.1-mini` GlobalStandard deployment with capacity 1 (`gpt-4o-mini` was refused as deprecating for new deployments). Pay-per-token, no model calls were made, so cost was ~0.
- Input: `azure.yaml` produced by `translate fixtures/azure/foundry/03-mcp-tools.yaml --to azure`, with `model:` edited to the existing deployment name (the emitted `gpt-5.4-mini` did not exist in the account).
- Verified:
  - Dry run with `-O check=remote` reads the project and reports the agent as absent.
  - `--apply --yes` created the agent (version 1): the `create_version(body={definition, description})` call shape is correct.
  - `fetch azure` read the agent back.
  - A second deploy is refused (`agent-exists`, status failed, nothing changed).
  - `-O new_version=true` with an identical definition returns the existing version 1 (Azure deduplicates); with a changed definition it creates version 2.
- Cleanup: agent, model deployment, project and account deleted. The account `agtr-dep-11171141` stays soft-deleted (purge is denied, same missing subscription-scope permission); not billed.
- Not covered: `kind` other than `prompt` (refused by design), tools that need credentials, invoking the agent.

## Attempted: `deploy bedrock` (2026-11-20), blocked by AWS

- Account: a corporate AWS account with no previous Bedrock Agents usage; IAM user with IAM, Bedrock and CloudFormation permissions; region `eu-west-1`; model `eu.amazon.nova-lite-v1:0`; a single service role created for the test and deleted afterwards.
- `deploy bedrock -O check=remote` (dry run): `ValidateTemplate` accepted the emitted template and the plan listed the stack with the agent and its alias. This part is real.
- `--apply --yes`: CloudFormation created the stack and rolled it back (`ROLLBACK_COMPLETE`, `AccessDenied` on `AWS::Bedrock::Agent`). Calling `bedrock-agent create-agent` directly returned the cause: *Bedrock Agents is in Maintenance Mode. New agent creation is not available for accounts without prior service usage.* Per the AWS documentation, Bedrock Agents Classic stopped accepting new customers on 2026-07-30 and AWS recommends migrating to Amazon Bedrock AgentCore.
- So this is not a tool bug, but it means a real create/fetch round trip could not be run. The failed stack, the role and its policy were deleted; no agent was ever created, and nothing is left in the account.
- Consequence: `fetch bedrock` and `deploy bedrock` only apply to accounts that already use Bedrock Agents Classic. Targeting AgentCore is not implemented.

## Real test: `translate --to agentcore` + `deploy bedrock` (2026-11-21)

- Same AWS account and region as the Bedrock attempt above (`eu-west-1`), a minimal agent (Nova Lite through the `eu.` inference profile, no tools), one execution role (trust `bedrock-agentcore.amazonaws.com`, only `bedrock:InvokeModel`) created for the test.
- `translate --to agentcore`, then `deploy bedrock -O stack_name=… -O param.<Agent>ExecutionRoleArn=…` (dry run with `check=remote`, then `--apply --yes -O wait=true`): the stack reached `CREATE_COMPLETE`. `get-harness` returned status `READY` with the emitted model, system prompt and `memory: {disabled: {}}`; AWS added a runtime (`harness_<name>`), a 75-iteration / 3600 s default loop limit and summarization truncation. `delete-stack` removed the harness and its runtime (verified: no harnesses, runtime `ResourceNotFound`, no memories).
- Permissions: `AmazonBedrockFullAccess` does NOT cover the `bedrock-agentcore` service. The first stack failed (rolled back, nothing left) on `bedrock-agentcore:CreateHarness`, the second on `CreateAgentRuntimeEndpoint`; the third passed with `Create/Get/Delete` on `Harness`, `HarnessEndpoint`, `AgentRuntime`, `AgentRuntimeEndpoint`, `Memory`, `WorkloadIdentity`, plus `List*` and `TagResource`, scoped to the region (granted to the test user and removed afterwards). `ListAgentRuntimes` stayed denied and was not needed.
- No account allowlist applied: a corporate account with no Bedrock Agents Classic usage could create a harness.
- Not covered: tools (`remote_mcp`, `inline_function`), model providers other than Bedrock, memory enabled, invoking the harness, `Hooks`/`Skills`, and any `fetch`. The `deploy bedrock` code is shared (a template with `AWS::BedrockAgentCore::Harness`), so it also has no AgentCore-specific consent or undo checks beyond the stack's.

## Tested with fakes only

- `fetch adk` (`tests/test_adk_cloud.py`): `reasoningEngines.get` through a fake REST client; extraction of an inline source archive (path traversal and links refused; the real service does not return one), env-value redaction, pickled/Python-only engines reported as `manual`.
- `deploy adk`: stubs/placeholders refusal, the create-only name check (paginated list), consent rules and the missing-extra hint, besides the real run above.

- `fetch bedrock`: `Get*/List*` calls, checked with `Stubber` (un-queued calls are rejected).
- `deploy` for all three platforms (tests in `tests/test_deploy.py`; Azure and Kagent were also run for real, above): dry-run default, `--apply` with confirmation / `--yes`, no tty without `--yes` → exit 2, placeholder refusal, create-only (existing objects/agents/stacks are errors), undo hint, `-O check=remote` read-only checks.

## Not verified

- `translate --to agentcore`: besides the schema check on every fixture, only one minimal agent was deployed (see above); tools, other model providers and memory were not, and there is no `fetch` for AgentCore.
- A successful real `deploy bedrock` (agent creation): only `ValidateTemplate` and the failure path (rollback, then stack deletion) were seen against real AWS.
- Any real `fetch bedrock`: it needs an existing classic agent, which a new account can no longer create.
- Kagent `v1alpha3` against a real cluster.
- GCP: only the minimal agent above; richer agents (tools, sub-agents) have not been deployed.

## Cheapest next real tests

1. Bedrock fetch, then deploy, but only in an AWS account that already has Bedrock Agents Classic agents (new accounts cannot create them); use a role with only `bedrock:InvokeModel` and delete the stack afterwards.
