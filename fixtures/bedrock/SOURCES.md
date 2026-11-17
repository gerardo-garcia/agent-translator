# Bedrock fixtures

Bedrock Agents are defined in CloudFormation as `AWS::Bedrock::Agent`. Every file below is a
CloudFormation template taken verbatim from AWS sample repositories (pinned to a commit), except
the one marked synthetic. Intrinsics (`!Ref`, `!GetAtt`, `!Sub`, ...) are preserved by the adapter.

| File | Origin (repo @ commit, path) | License | Kind |
|---|---|---|---|
| cfn/connected-house.yaml | awslabs/amazon-bedrock-agent-samples @ `3e55140baa52555fdf73142abb04b4e1962be91f`, `examples/agents/connected_house_agent/template.yml` | Apache-2.0 | official |
| cfn/cost-explorer.yaml | aws-samples/amazon-bedrock-samples @ `d481795702a043af8fcedfdf843b94ec20224675`, `agents-and-function-calling/bedrock-agents/use-case-examples/cost-explorer-agent/agent_template.yaml` | MIT-0 | official |
| cfn/hr-assistant.yaml | same repo and commit, `.../use-case-examples/hr-assistant/cfn/hr-resources.yml` | MIT-0 | official |
| cfn/api-expert.yaml | aws-samples/serverless-samples @ `d8b80a19a88dbc596d2e2ead2c466fc2780f1eb0`, `apigw-ai-agents/bedrock-agents/iac/api-expert-agent.yaml` | MIT-0 | official |
| cfn/competitive-intelligence.yaml | aws-samples/amazon-bedrock-agents-healthcare-lifesciences @ `74deb57bee036ce1b3659072208475734de77f60`, `multi_agent_collaboration/competitive_intelligence/competitive-intelligence-agent-cfn.yaml` | MIT-0 | official |
| cfn/medical-imaging.yaml | same repo and commit, `multi_agent_collaboration/cancer_biomarker_discovery/bedrock_agents/agents/Medical-imaging-expert.yaml` | MIT-0 | official |
| cfn/synthetic-multi-agent.yaml | written for this project: supervisor + two collaborators in one template, router mode, memory, literal KB id and Lambda ARN, `RequireConfirmation`, `RETURN_CONTROL` | n/a | synthetic |

## Schema
`schemas/aws-bedrock-agent.json` and `aws-bedrock-agentalias.json` are the official CloudFormation
resource schemas, extracted on 2026-11-16 from
`https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip`.
Tests validate the `Properties` of every agent (fixtures and emitted output) against it. Values
that are intrinsics cannot be checked offline, so they match any type or pattern in the validator
(`tests/bedrock_helpers.py`).

## Not covered yet
No official sample defines a collaborator *inside the same template*; the synthetic file does.
Agents created through the console or SDK (`GetAgent` output, `bedrock-agent` JSON) are not
supported yet; only CloudFormation is.

## api/ — Bedrock API/CLI JSON (GetAgent)

**Synthetic** (no public sample outputs exist): `getagent-minimal.json` (a `get-agent` response),
`getagent-bundle.json` (agent + the lists the Get/List calls for action groups, knowledge bases and
collaborators return) and `getagent-summaries.json` (an export made with `List*` calls, plus failure
reasons). Their field names and nesting are checked in `tests/test_bedrock_getagent.py` against the
`bedrock-agent` service model shipped in `botocore` (Apache-2.0; extra `validate-bedrock`), and the
CloudFormation output they translate to against the CloudFormation schema above.
Only the bundle layout (one document holding the separate calls' results) is our own convention.
