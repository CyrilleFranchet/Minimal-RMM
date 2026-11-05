# Playbook: [short task name]

Authoring note: Replace every [placeholder] and remove unused steps before
execution. Keep this document concise. Numbered phases organize execution;
the current runtime loads the entire playbook, not one phase at a time.
Submit the specific task and input values in the chat prompt separately.

## Objective

[Describe one concrete outcome.]

## Scope and inputs

- Authorized targets and networks: [explicit scope]
- Selected agent: [full session ID, or how to identify the intended agent]
- Execution environment: [agent, RMM workspace, or Exegol]
- Required input values: [names and meanings; avoid embedding secrets]
- Permitted changes: [changes the operator requested]
- Resources to preserve: [existing sessions, relays, files, or configuration]

If a required value is missing or ambiguous, resolve it from available evidence
or ask the operator. Never execute placeholder values or expand the scope.

## Global execution rules

- Use the available RMM and Exegol MCP tools. Do not load other skills.
- Inspect tool results for success, errors, and the actual execution state.
- A queued operation is not completed. Correlate results with its operation ID,
  session, and requested action before taking dependent steps.
- Before retrying an uncertain action, inspect existing results and resources.
  If its state remains unknown, report it as inconclusive instead of repeating it.
- Continue after prerequisite checks until the objective is verified or blocked.
- Keep output concise; reference artifacts instead of copying large tool output.
- Never include credentials or tokens in checkpoints or the final report.

## Phase 1: Verify prerequisites

Entry conditions: [what must already be available]

1. [Identify the intended session, environment, and existing resources.]
2. [Check the specific capabilities and prerequisites needed for this task.]

Completion evidence: [observable results required before Phase 2]

On failure: [a supported alternative, or a concrete blocker to report]

## Phase 2: Perform the requested action

Entry conditions: Phase 1 completion evidence is available.

1. [Execute the scoped action using the appropriate MCP tool.]
2. [Wait for or retrieve its correlated result before dependent actions.]

Completion evidence: [operation result and resulting resource or state]

On failure: [what to inspect; when to stop without retrying]

## Phase 3: Verify and finish

Entry conditions: The action's execution state is known.

1. [Independently check the requested outcome in the correct environment.]
2. [Perform explicitly requested cleanup; preserve pre-existing resources.]

Completion evidence: [the exact acceptance criteria for the overall task]

On failure: Report partial completion or an inconclusive result, including
remaining resources and unfinished work. Do not claim success from startup
or submission alone.

## Execution checkpoint

After each phase, retain a concise checkpoint in the conversation:

- Current phase and verified completed steps
- Relevant session and execution environment
- Evidence references and pending operation IDs
- Resources created or changed
- Next unfinished step or concrete blocker

## Final report

- Outcome: verified success, partial completion, confirmed failure, or inconclusive
- Work completed: [actions actually performed]
- Evidence: [results supporting the outcome]
- Unfinished work or blocker: [what remains and why]
- Resource state: [what remains active or changed; verified cleanup if applicable]
