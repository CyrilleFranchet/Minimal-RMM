---
id: web-idor
title: Object authorization investigation
description: Test horizontal, vertical, and tenant object-access boundaries using known lab identities and objects; load for IDOR or broken object-level authorization.
default: false
---

# Object authorization investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Build an ownership matrix

Use designated lab identities A and B, their known roles/tenants, and one
known object owned by each. Record the expected sharing policy first.
Prefer existing fixtures; creating accounts or modifying access configuration
is governed by the actual task and `operator-boundaries`.

Map references in path, query, JSON, headers, nested objects, download URLs,
and asynchronous export jobs. Random-looking IDs are not authorization.
Predictable IDs are not a vulnerability if access is correctly checked.

## Compare matched requests

| Request | Expected purpose |
|---------|------------------|
| A reads A's object | Positive control for route and credentials. |
| B reads B's object | Positive control for the second identity. |
| A requests B's known private object | Horizontal boundary test. |
| Lower role requests a known privileged function/object | Vertical boundary test. |
| Same role crosses a tenant boundary | Tenant-isolation test. |

Keep object ID as the only changed variable where possible. Preserve token,
HTTP method, content type, and pagination. Compare actual fields and artifact
contents, not just status codes: some APIs return HTTP 200 with denial data.
Use a nonexistent object only as an additional error-behavior control.

## Select the relevant branch

If read access is denied, do not infer write/delete access is protected.
Test other operations only when included in the task and with disposable
fixtures; discovery of an ID is not permission to alter pre-existing data.
If an export is asynchronous, check job ownership and artifact retrieval
independently. If a nested resolver exposes the object, use
`web-graphql` for resolver-specific comparison.

Avoid large ID enumeration. One known foreign fixture establishes the access
question more clearly and produces less ambiguous evidence.

## Interpret and continue

Public/shared objects, inherited memberships, administrator roles, cached
responses, and signed download URLs can explain unexpected access. Validate
the declared policy against the actual test account and fresh requests.
Distinguish object existence disclosure from content access and from mutation.
Document field-level exposure even when the whole object is not returned.

## Detection and handoff

Expected evidence is ordinary authenticated application traffic, often without
distinctive payload strings. Record actor, object owner, tenant, route, time,
request ID, and application audit outcome. Do not claim detection from HTTP
success. Handoff the ownership matrix and smallest redacted reproduction.

Adapted from [Claude-Red offensive-idor](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-idor/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).
