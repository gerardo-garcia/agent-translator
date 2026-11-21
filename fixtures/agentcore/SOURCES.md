# AgentCore fixtures

There are no sample agents here: `agentcore` is an emit-only target, so tests translate the other
platforms' fixtures into it.

## Schema
`schemas/aws-bedrockagentcore-harness.json` is the official CloudFormation resource schema of
`AWS::BedrockAgentCore::Harness`, extracted on 2026-11-20 from
`https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip`.
Tests validate the `Properties` of every emitted harness against it (`tests/agentcore_helpers.py`);
intrinsics match any type or pattern, as for Bedrock.
