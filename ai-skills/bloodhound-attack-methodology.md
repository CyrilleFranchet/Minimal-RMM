---
id: bloodhound-attack-methodology
title: BloodHound schema reference and attack-path methodology
description: AD/Azure/ADCS node, property, and edge reference plus interpretation methodology to support bloodhound-analysis. Reference knowledge only; it does not expand the executable query catalog.
default: false
---

# BloodHound schema reference and attack-path methodology

Reference knowledge to support `bloodhound-analysis`; `operator-boundaries`
applies. This skill does not add an execution path: every Cypher fragment
below is domain-schema reference data to interpret edges and plan the next
catalog query, never instructions or permission to execute arbitrary Cypher.
Execution remains limited to `bloodhound(action="query", ...)` against
`bloodhound-queries/catalog.json`; `library_search`/`library_get` expose the
complete upstream library the same way, as inspectable originals, not an
expanded allowlist. If a question below needs an edge family the catalog does
not cover, report the coverage gap per `bloodhound-analysis` rather than
finding another way to run the Cypher shown here.

Adapted and reorganized from the reference resources of
[mwnickerson/bloodhound_mcp](https://github.com/mwnickerson/bloodhound_mcp)
at revision `cdb17097e761c8a8622cb93bc3ba49a9e150bb6e`. The upstream GPLv3
license is preserved in
[third_party/bloodhound-mcp/LICENSE](../third_party/bloodhound-mcp/LICENSE).
That project is listed on SpecterOps' "Open-source Tools" page but is an
independent, community-maintained server, not a SpecterOps product; see
[tech-plan.md](../docs/tech-plan.md#evaluate-mwnickersonbloodhound_mcp-as-a-workspace-scoped-pathfinding-tool)
for why it is not integrated directly. Its own pathfinding and free-Cypher
tools are not adopted here — only the schema/methodology prose, reframed to
this project's catalog-only execution boundary.

## Schema reference

All property names are lowercase; a wrong case silently returns no results,
not an error. Names are stored uppercase with the domain suffix, e.g.
`"JSMITH@CORP.LOCAL"` or `"DOMAIN ADMINS@CORP.LOCAL"` — filter on the exact
uppercase string or `TOUPPER()`, never the mixed-case display form. List
properties (`serviceprincipalnames`, etc.) may be null; interpretation that
ignores `COALESCE(...)` risks treating a null as an absent capability rather
than an unknown one.

Useful properties beyond identity: `admincount` correlates with privilege via
AdminSDHolder but is not definitive; `hasspn`+`enabled` flags a Kerberoastable
candidate, `dontreqpreauth` an AS-REP-roastable one — both are directory
configuration, not a completed ticket request. `unconstraineddelegation` and
`haslaps` are computer-level flags. `description` fields occasionally carry
operator-left secrets; treat any finding there as a credential-exposure lead,
not a verified credential.

**AD node types:** `User`, `Computer`, `Group`, `Domain`, `OU`, `GPO`,
`Container`, `CertTemplate`, `RootCA`, `EnterpriseCA`, `AIACA`, `NTAuthStore`.
**Azure/Entra node types:** `AZUser`, `AZGroup`, `AZApp`, `AZServicePrincipal`,
`AZTenant`, `AZDevice`, `AZRole`, `AZSubscription`, `AZResourceGroup`,
`AZManagementGroup`, `AZKeyVault`.

**AD edge families**, grouped by what they establish (not by what they prove
was exercised): membership (`MemberOf`, `Contains`, `GPLink`); ACL control
(`GenericAll`, `GenericWrite`, `WriteOwner`, `WriteDacl`, `Owns`,
`OwnsLimitedRights`, `WriteOwnerLimitedRights`, `AllExtendedRights`);
credential/session (`HasSession`, `DCSync`, `GetChanges`, `GetChangesAll`,
`GetChangesInFilteredSet`, `ReadLAPSPassword`, `ReadGMSAPassword`,
`SyncLAPSPassword`, `DumpSMSAPassword`); remote access (`AdminTo`, `CanRDP`,
`CanPSRemote`, `ExecuteDCOM`, `SQLAdmin`); delegation (`AllowedToDelegate`,
`AllowedToAct`, `AddAllowedToAct`, `CoerceToTGT`); mutation
(`ForceChangePassword`, `AddMember`, `AddSelf`, `WriteSPN`,
`AddKeyCredentialLink`, `WriteAccountRestrictions`, `WriteGPLink`); identity
abuse (`HasSIDHistory`, `SpoofSIDHistory`, `AbuseTGTDelegation`); trust
structure (`SameForestTrust`, `CrossForestTrust`); NTLM relay
(`CoerceAndRelayNTLMToSMB`/`ADCS`/`LDAP`/`LDAPS`); ADCS abuse (`ADCSESC1`,
`ESC3`, `ESC4`, `ESC6a`/`ESC6b`, `ESC9a`/`ESC9b`, `ESC10a`/`ESC10b`, `ESC13`,
`GoldenCert`). **Azure edge families:** `AZGlobalAdmin`,
`AZPrivilegedRoleAdmin`, `AZApplicationAdministrator`,
`AZCloudApplicationAdministrator`, `AZResetPassword`, `AZOwns`,
`AZExecuteCommand`, `AZAddMembers`, `AZGrantAccess`.

Two schema traps worth carrying into every interpretation:

- **DCSync targets `Domain`, not `Group`.** The pre-computed `DCSync` edge and
  the raw `GetChanges`/`GetChangesAll` edges all point at the domain object.
  A query shaped around a `Group` target returns nothing and is not evidence
  of absence. Both `GetChanges` and `GetChangesAll` are required together for
  DCSync; `GetChangesInFilteredSet` alone is a different, narrower privilege
  (used for `SyncLAPSPassword`), not DCSync.
- **GPO abuse is a chain, not an edge.** A write edge onto a `GPO` only
  matters together with `GPLink` (GPO → linked OU/container) and `Contains`
  (container → affected objects). Reporting write access to a GPO without the
  link and containment steps overstates what is shown.

## ADCS (ESC) interpretation

Certificate-services findings need the same "condition observed, not
confirmed exploited" framing as other edges. Per vector, record both the
underlying misconfiguration and that the corresponding `ADCSESC*` edge
reflects BloodHound's structural detection of that condition, not a request
that was actually made:

| Vector | Underlying condition |
|---|---|
| ESC1 | Template allows low-privilege enrollment and lets the enrollee supply a SAN with a client-auth EKU — enables impersonating an arbitrary principal. |
| ESC2 | Template has an Any-Purpose EKU or none — the issued certificate is usable for any purpose, including client auth. |
| ESC3 | An enrollment-agent template plus a second template accepting enrollment-on-behalf-of together let a low-priv enrollee request certs for others. |
| ESC4 | A low-privilege principal has write rights on the template object itself, which can be used to introduce ESC1-style conditions. |
| ESC6a/ESC6b | The CA has `EDITF_ATTRIBUTESUBJECTALTNAME2` set, so any request may carry an arbitrary SAN (ESC6b: via relay). |
| ESC8 | The CA exposes an HTTP enrollment endpoint that accepts NTLM, enabling relay of a coerced machine authentication into a certificate. |
| ESC9a/ESC9b | The template has `CT_FLAG_NO_SECURITY_EXTENSION`, dropping the security extension that strong certificate-mapping relies on. |
| ESC10a/ESC10b | Weak certificate-to-account mapping (e.g., UPN-based) is configured without strong binding. |
| ESC13 | An issuance-policy OID is linked to a group via `msDS-OIDToGroupLink`; enrolling grants that group's membership. |
| GoldenCert | Requires the CA's private key; a structural edge, not evidence the key has actually been exfiltrated. |

Before escalating an ESC finding, check (via the available directory/template
queries, not by inventing one): who can enroll, whether the enrollee supplies
the SAN, which EKUs are set, who has write access to the template, whether
manager approval is required, and whether enrollment agents are configured.
An ESC edge without these corroborating details is a lead, not a confirmed
path.

## Azure/Entra interpretation

Hybrid identity changes what "compromised" means: a `SyncedToADUser`/
`SyncedToEntraUser` edge means an on-prem principal and a cloud principal
share an identity, so a finding on one side carries to the other — state this
explicitly rather than treating the two directories as isolated. Global Admin
and privileged-role edges (`AZGlobalAdmin`, `AZPrivilegedRoleAdmin`,
`AZApplicationAdministrator`, `AZCloudApplicationAdministrator`) are tenant
control points; `AZResetPassword` chains and service-principal/app-role
abuse are the usual routes to them. Key Vault access edges indicate secret
*reachability*, not that a secret was read.

## OpenGraph / custom nodes

BloodHound CE can ingest custom (OpenGraph) node and edge kinds beyond AD and
Azure — environment-specific assets such as a SQL Server fleet or a web
application stack, modeled as directed, one-way edges connecting into the
existing AD/Azure graph. This project's collector
(`bloodhound-ce-python-collection`) and query catalog do not currently
produce or consume OpenGraph data; treat any custom node type mentioned by an
operator as outside current coverage, not as something queryable through the
existing adapter, and report that gap per `bloodhound-analysis` rather than
guessing at a schema.
