---
id: ad-collection-analysis
title: AD data collection and analysis
description: Collect scoped AD data with Python BloodHound and analyze the artifacts in the operator environment using direct JSON analysis or BloodHound.
default: false
---

# AD data collection and analysis

## Scope and precedence

When selected for an authorized AD collection task, prefer Python BloodHound
(BloodHound.py) in the operator environment, normally Exegol. This skill is
optional and disabled by default; selection does not request immediate execution.
Do not enable it automatically or change the operator's saved skill selection.

- For collection, this preference takes precedence over the ADWS-first guidance
  in `windows-ad-recon-stealth`. Direct operator instructions and scope prevail.
  Recommend deselecting that competing skill for a BloodHound session.
- For current-token identity or effective groups, retain
  `windows-identity-and-groups`: use narrow token checks, not domain collection.
  For a narrow directory question, reuse relevant evidence or establish whether
  broader collection is intended.
- Do not describe the collector as inherently LDAP-only or stealthy. Network
  activity and host interrogation depend on the selected collection methods.

## Prerequisites and execution environment

Establish these facts from existing context before collecting:

- Authorized domain/targets and the relationships needed to answer the request.
- Operator execution environment, target connectivity, and DNS resolution.
  A working RMM beacon does not prove Exegol can reach a domain controller.
- Installed collector executable, version, supported options, and compatibility
  with the intended BloodHound deployment if import is requested.
- Available authentication: the Windows agent's token is not automatically
  available to a collector running in Exegol.
- Output directory, artifact retention location, and whether transfer or import
  is requested.

Inspect live tool schemas and installed collector help. Run the collector with
operator/Exegol tools; RMM `exec_command` and `queue_command` execute on the
Windows agent. Do not send Linux collector commands to them. Use credentials
through supported mechanisms and exclude secrets from reports. Do not install
or upgrade packages without an operator request or established setup scope.

## Collection and artifact handling

1. Reuse existing data when its date and coverage satisfy the request. Otherwise
   choose the smallest collection scope that supplies the required relationships;
   do not default to every method or additional host interrogation.
2. Construct the command from installed help and execute in the established
   environment with the authorized targets and authentication. When a pivot is
   needed, follow `exegol-rmm-pivot` if available; a selected beacon alone does
   not authorize broad routes or proxy exposure.
3. Inspect exit status, warnings, output files, and coverage. Record complete,
   partial, or failed collection; do not blindly retry unchanged authentication
   or connectivity failures.
4. Verify and preserve the generated JSON/archive artifacts before analysis.
   Collection logs alone are not analysis evidence.
5. Analyze artifacts directly in Exegol when they are already accessible there.
   Download a copy when requested or needed by the analysis environment, using
   available file-transfer tools. For artifacts staged on Windows, use the RMM
   download tools for that session. Verify the destination before claiming a
   transfer succeeded. In-place analysis does not count as a download.
6. For large datasets, execute analysis near the files and return scoped results;
   do not stream entire JSON files into chat. Preserve originals when extracting,
   importing, or transforming data.

If the preferred collector is unavailable, incompatible, or blocked, identify
what is missing. Explain alternatives and obtain the operator's preference
before switching collection workflows.

## Analysis method and evidence

Inspect sample records and metadata first. Adapt field names, relationships,
and queries to the observed collector format and deployed BloodHound version.
Do not assume raw LDAP attributes appear unchanged in exported JSON.

- Use direct JSON filtering or scripts for object properties, memberships,
  delegation settings, and individual ACL relationships. Resolve identifiers
  and nested memberships as needed for the question.
- Use BloodHound for multi-hop graph questions when import is requested or a
  suitable deployment is available. A local graph traversal is also possible,
  but must account for edge direction, semantics, and missing coverage.
- Import is optional for questions answerable from JSON. Report the actual
  analysis method and which files or graph records support each finding.
- Distinguish observed properties and edges from validated attack paths.
  Privilege indicators, session records, and potential paths require context;
  they do not alone prove usable credentials or successful compromise.
- State coverage gaps explicitly. Missing relationships may reflect collection
  scope, permissions, failures, or data age rather than absence in the domain.

## Reporting

Summarize collector/version, domain and collection scope, artifact locations,
collection date, warnings, and findings relevant to the request. Report
collection, transfer, import, and analysis as separate statuses, including
“not requested” or “analyzed in place” where applicable. Cite supporting objects
or relationships, label hypotheses, and omit passwords, hashes, and tickets.
