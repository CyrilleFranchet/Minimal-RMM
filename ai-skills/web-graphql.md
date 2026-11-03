---
id: web-graphql
title: GraphQL resolver investigation
description: Map GraphQL schema and resolver authorization with bounded lab queries; load for GraphQL queries, mutations, node references, or subscriptions.
default: false
---

# GraphQL resolver investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Confirm the endpoint and client contract

Start from observed application traffic. A small valid query such as
`{ __typename }` can identify a GraphQL response without a broad scan.
Record HTTP method, operation name, variables, authenticated identity,
content type, and any batching or subscription transport.

Use an available schema or targeted introspection where allowed by the
application. Disabled introspection is not proof that authorization is sound.
Do not brute-force the entire schema when client queries answer the question.

## Choose a resolver-level hypothesis

| Surface | Controlled question |
|---------|---------------------|
| Direct object/node lookup | Can identity A read identity B's known private fixture? |
| Nested relationship | Does traversal enforce the same boundary as direct lookup? |
| Field-level access | Are sensitive fields filtered consistently across roles? |
| Mutation | Is authorization checked at the action and referenced-object levels? |
| Aliases/batching | Are ordinary supported operations accounted for consistently? |
| Subscription | Does the designated test stream enforce identity and expiration? |

GraphQL HTTP 200 can contain errors or partial data. Inspect both `data` and
`errors`, including which fields resolved before an error. Keep variables
separate from the query structure to reproduce the exact resolver invocation.
Use `web-idor` for the ownership matrix when needed.

## Bound the proof

Select known fixture objects and shallow queries. Start with one operation;
a small alias/batch comparison is enough to investigate accounting behavior.
Do not use depth bombs, large alias fans, credential brute force, or expensive
recursive queries as a routine configuration check.
Mutations require disposable task-appropriate fixtures; do not alter real
records merely because a schema exposes an operation.

For subscriptions, retain handshake and per-operation authentication evidence;
a successful WebSocket handshake alone does not prove access to a stream.

## Interpret and continue

Schema visibility is discovery, not a vulnerability by itself. Relay IDs or
opaque node IDs are references, not authorization. Different nested and direct
results can reflect documented role rules or partial-data semantics.
If a resolver shows injection evidence, load the matching injection skill
rather than treating every GraphQL argument as SQL.

## Detection and handoff

Endpoint-only logs may hide operation names and fields. Record actual captured
operation/variable visibility, actor/object IDs, errors, and timestamps.
Handoff the smallest query and role comparison with redacted fixture data,
confirmed resolver boundary, and independent detection evidence.

Adapted from [Claude-Red offensive-graphql](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-graphql/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).
