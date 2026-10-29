---
id: document-embeddings
title: Document embeddings and classification
description: Explain and run document classification with saved profiles, inspect results, and diagnose missing execution feedback.
default: true
---

# Document embeddings and classification

Apply only to document-classification tasks or explanations. The autonomous
plugin runs ONNX embedding/classification on explicitly selected Windows-agent
files. Hugging Face serves model/tokenizer downloads, not document processing.
Results contain paths, vectors, labels, scores, and errors; treat them as
sensitive. Cosine similarity is not a confidence percentage or proof of content.
Without labels the plugin returns embeddings; it is not generative chat or a
vector search service.

## Execute through RMM tools

Use `embedding_profiles`, `run_embeddings`, and `embedding_results` directly.
RMM `exec_command` / `queue_command` run on the target: do not send operator
CLI commands or `embeddings` to those tools.

1. Establish the session, explicit paths, and classification purpose. Use
   `list_sessions` / `get_session` if needed; do not guess profiles or widen scope.
2. List profiles with `embedding_profiles(action="list")`, then retrieve the
   chosen one with `action="get", name=...`. For a new profile, retrieve
   `action="defaults"`, adapt labels/settings, and save with `action="save"`.
   Keep validated model settings unless the task requires a different model.
3. Review copying settings: defaults may copy a financial category. Do not
   silently enable copying. An explanation request does not authorize a run.
4. Call `run_embeddings(session_ref=..., profile=..., documents=[...])`.
   Use ordinary path strings in structured arguments; escape backslashes when
   constructing JSON text.
5. Inspect queue/dispatch/output events. Acceptance is not completion; do not
   repeat an accepted or uncertain run that may still be processing or copying.
6. Inspect per-document errors and save verified output using
   `embedding_results(action="save", session_ref=..., event_id=...)`.
   Retrieve saved results with `action="list"` or `action="get", name=...`.

There is no reliable run ID: do not assume the next output belongs to the last
profile. Identify the exact session/event before saving or attributing results.
Report what was queued, completed, and verified separately.

## Ownership and runtime constraints

- Workspace profiles/results belong to the local workspace service. If central
  Web AI tools return `embeddings_are_workspace_local`, direct the operator to
  the local panel, local-service CLI, or standalone MCP connected there. Do not
  recreate profiles centrally or bypass ownership with raw plugin commands.
- Requires the autonomous DLL in the plugin store and a Windows amd64 Go agent
  with PE and `RMMRequiresWindowsLoader` support. Updating the server/UI does not
  update the DLL. The older non-autonomous DLL requires a separate runtime.
- The autonomous plugin includes its inference runtime, needs compatible model
  files, outbound HTTPS to Hugging Face/CDN, and writable temporary storage.
  Windows loads a staged DLL; execution is not memory-only.
- Keep pinned model revision and SHA-256 settings. `auth.method=env` uses an
  existing agent-side `HF_TOKEN`; it does not provision one. Exclude credentials
  from profile text and output.
- Supports text, Markdown, CSV, JSON, logs, and DOCX; PDF/OCR are unsupported.
- One to 32 explicit paths, including UNC, with at most 32 documents after
  directory expansion. Configuration plus paths must fit 4096 UTF-8 bytes.
  Reduce batches/configuration on rejection; never silently truncate.

## Classification, copying, and transfer

For an authorized document-transfer task, classify relevant files locally first
and use task-specific label descriptions to prioritize what to move. Batch
within runtime limits. Classification returns metadata rather than document
content; it does not itself download files.

`copy_documents`, `copy_labels`, and `copy_threshold` can stage matches under a
target temporary directory. Copy paths appear in results. Copying, transfer,
and cleanup are separate actions governed by the operator's intent; a
classification run alone does not authorize exfiltration.

## UI and troubleshooting

In **Embeddings**, name and **Save profile**, select the saved profile, enter
one unquoted path per line, then **Run saved profile**. Edited/imported fields
must be saved again before execution. Inspect validation/queue messages and
the session transcript; older builds show feedback only at the dialog's top.

The WebUI session shell supports `embeddings run PROFILE --document "PATH"`
on versions with that command installed. Bare `embeddings` shows usage; profile
management belongs to the panel or operator CLI (`python rmm_cli.py embeddings
profiles list`). CLI commands must address the service owning the profile.
A Windows “not recognized” error indicates raw shell dispatch, not plugin run.

Check validation and the API response first: unsaved profiles, missing paths,
agent/DLL compatibility, request limits, and ownership can block queuing.
After acceptance, inspect `Plugin queued`, `Plugin dispatched`,
`Plugin downloaded`, and `Plugin completed`, then returned errors/results.
Target console silence is intentional; do not add target-side plugin logging.
Source code or passing tests alone do not establish successful deployment.
