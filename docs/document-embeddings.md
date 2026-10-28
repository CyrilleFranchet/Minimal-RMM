# Document embedding profiles and saved results

See the [documentation index](README.md) and [workspace architecture](architecture.md).
The **Embeddings** button opens a dedicated editor in the WebUI. The
`embeddings` CLI command and MCP tools use the same authenticated REST API.
Profiles and saved result collections survive browser and service restarts.

## Install the runtime first

Use the updated Windows amd64 Go agent with `RMMRequiresWindowsLoader`
support, and install `document-embeddings-autonomous.dll` through the existing
[asset manager](operator-assets.md). The
[validated DLL artifact](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/actions/runs/40553323301/artifacts/12763491713)
is 36,232,192 bytes; SHA-256:
`a08000ab452a475f708293743f91231ca5f31317f4690b98cb41464841aae2e6`.
It includes ONNX Runtime and needs no separate Python, ONNX Runtime, or VC
runtime installation. Windows system libraries and HTTPS access to Hugging
Face/CDN remain required. The agent temporarily writes this DLL to disk,
loads it with the Windows loader, and removes it after its child exits.

For **browser upload or workspace plugin synchronization**, configure these
limits before starting the corresponding services:

```bash
# Central server (base64 upload/synchronization is about 48.3 MB)
export RMM_MAX_BODY_BYTES=67108864
# Local workspace service (browser upload)
export RMM_WORKSPACE_MAX_BODY=67108864
```

The defaults are 32 MiB and cannot accept this DLL as a base64 JSON upload.
For Docker Compose, set both variables in `.env`; the Compose services pass
them through, and `.env.example` includes the 64 MiB values. Recreate the
services after changing these environment settings.
The asset download limit defaults to 64 MiB. In server mode, copying the DLL
into the configured plugin directory avoids the JSON upload limit. In
workspace mode, install through the local asset manager; it owns the source
artifact and synchronizes its runtime copy to the central broker.

## Storage ownership

| Mode | Durable database | Scope |
| --- | --- | --- |
| Server | `RMM_logs/embeddings.db` | Shared operator namespace; no individual user accounts |
| Workspace | `RMM_WORKSPACE_DATA/workspace.db` | One operator workspace |

Profiles and results use the `embedding_records` table, separately from the
browser configuration blob. The database and containing directory are
owner-only. Do not share a workspace data directory between operators.
In workspace mode, point CLI and standalone MCP at the **local workspace
service URL** and use its local token. The central API rejects embedding
storage operations with `409 embeddings_are_workspace_local`; a workspace
token cannot access the shared server embedding store either.

The WebUI Config export does not include profiles or result collections.
Use the Embeddings panel's explicit JSON export/import controls instead.
An exported profile can be imported into another workspace and saved there.
No browser storage entry is the source of truth for profiles or results.

## Configure and run from the CLI

The example is an English-oriented MiniLM encoder. It does not establish
French classification quality. Start with the checked-in
[profile example](../examples/embedding-profile.json):

```bash
python rmm_cli.py embeddings profiles save strategy --file examples/embedding-profile.json
python rmm_cli.py embeddings profiles list
python rmm_cli.py embeddings profiles get strategy --output strategy.json
python rmm_cli.py embeddings run strategy --session SESSION_ID \
  --document 'C:\Lab\strategy.docx' --document 'C:\Lab\notes.txt'
python rmm_cli.py events --session SESSION_ID
```

Edit the JSON and save the same name to update it. Profile names allow letters,
digits, dots, underscores, and hyphens, up to 64 characters. Exported records
and plain configuration objects are both accepted by `profiles save --file`.
The command group is available in the standalone CLI; the existing interactive
shell retains generic `pe` execution.

A profile contains:

- `model`: Hugging Face repository, pinned 40-character revision, relative
  ONNX and tokenizer paths, ONNX SHA-256, ordered input tensor names,
  output tensor name, and `mean`, `cls`, or `direct` pooling.
- `auth.method`: `none` for public downloads or `env` to use `HF_TOKEN`
  **already present in the agent process environment**. This preference does
  not provision a token on the target. Raw tokens and unknown configuration
  fields are rejected. Do not put credentials in prompts or label text.
- `prompt` and `label_prompt`: literal embedding prefixes, up to 512 UTF-8
  bytes each. Empty label prefix uses the document prefix. These are not
  generative chat instructions.
- `max_tokens`: 8–512. Longer documents are truncated, not chunked.
- `labels`: up to 16 `{ "name", "text" }` descriptions; label text is limited
  to 512 UTF-8 bytes. Omit labels for embeddings without classification.

Each run supplies one to 32 explicit document paths. The complete serialized
plugin input must fit **4096 UTF-8 bytes**; oversize batches are rejected
before queuing rather than silently truncated. Model/tokenizer compatibility,
CPU graph constraints, supported file formats, and per-document limits remain
as specified by the
[DLL guide](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/blob/feature/autonomous-document-embeddings/docs/document-embeddings.md).
Text, Markdown, CSV, JSON, logs, and DOCX are supported; PDF/OCR is not.

## Web workflow

1. Connect to the server or your local workspace service and click **Embeddings**.
2. Select a saved profile, import a JSON profile, or fill the model fields.
   A new profile starts with the validated MiniLM model settings.
3. Set authentication, prefixes, token limit, and optional label JSON.
4. Click **Save profile**. Unsaved edits must be saved before running.
5. Enter a session ID or unique prefix and one Windows document path per line.
   Click **Run saved profile** and inspect the session transcript.
6. Click **Load output events for this session**, select its exact output
   event ID, and click **Save selected result**.
7. Use **Export JSON** on a saved collection to download it, or delete the
   saved copy. Refresh the result list after changes from another client.

The event picker reads up to ten pages of 1000 events per click and advances
its cursor. Click again to continue through long workspace histories or to
load new output. Server history starts with its latest page; an older known
ID can still be entered explicitly. Paths and vectors can be sensitive;
exports should be handled like the source documents.

## Result selection and retention

The existing plugin protocol does not supply a reliable run ID. This feature
therefore saves the **explicitly selected session/event**, never the next
output received after queuing. It validates the embedding result shape and
finite vector values, but does not prove that the selected output came from a
particular profile or DLL. It records the source session and event ID along
with the model, revision, vectors, document errors, and classifications.

```bash
python rmm_cli.py embeddings results save --session SESSION_ID --event 42
python rmm_cli.py embeddings results list
python rmm_cli.py embeddings results get RESULT_ID --output embeddings.json
python rmm_cli.py embeddings results delete RESULT_ID
python rmm_cli.py embeddings profiles delete strategy
```

Repeated saving of the same session/event is idempotent. Result listings omit
vectors; `get` returns the full exportable collection. Saved collections remain
until explicitly deleted, independently of transcript/history deletion.
Deleting a saved collection does not delete its original transcript event.
There is no automatic result retention limit or vector search index.
Classification scores are cosine similarity, not calibrated probabilities.

## REST and MCP

All routes use the existing operator authentication. Workspace ownership is
established by the service, never by a `user_id` or namespace in input JSON.

| Method and path | Body / response | MCP |
| --- | --- | --- |
| `GET /api/v1/embeddings/profiles` | `profiles` list | `embedding_profiles(action="list")` |
| `GET /api/v1/embeddings/profiles/{name}` | Full `record` | `embedding_profiles(action="get", name=…)` |
| `POST /api/v1/embeddings/profiles/{name}` | `{ "config": { … } }` | `embedding_profiles(action="save", name=…, config=…)` |
| `DELETE /api/v1/embeddings/profiles/{name}` | `{ "ok": true }` | `embedding_profiles(action="delete", name=…)` |
| `POST /api/v1/embeddings/run` | `{ "session_ref", "profile", "documents": […] }` | `run_embeddings` |
| `GET /api/v1/embeddings/results` | Collection metadata | `embedding_results(action="list")` |
| `GET /api/v1/embeddings/results/{id}` | Full `record`, including `result` | `embedding_results(action="get", name=…)` |
| `POST /api/v1/embeddings/results` | `{ "session_ref", "event_id": 42 }` | `embedding_results(action="save", session_ref=…, event_id=42)` |
| `DELETE /api/v1/embeddings/results/{id}` | `{ "ok": true }` | `embedding_results(action="delete", name=…)` |

These tools are registered in MCP and the Web AI tool catalog. **Workspace Web
AI currently executes centrally**, so it cannot access locally saved embedding
profiles/results; use the dedicated local WebUI, CLI, or standalone MCP pointed
at the local service. Central Web AI receives the same workspace-local error.
Server-mode Web AI can use the shared server-owned profiles.

## Code and validation reference

| Symbol | Responsibility |
| --- | --- |
| `EmbeddingError` | Invalid configuration/request/result error |
| `encode`, `text`, `fields` | Strict JSON, bounded UTF-8 strings, and field allowlists |
| `profile_config`, `plugin_request`, `result_payload` | Normalize profiles, bound final requests, validate selected output |
| `EmbeddingStore.__init__`, `close` | Owner-only SQLite setup and connection lifecycle |
| `EmbeddingStore.get`, `put`, `delete` | Parameterized record access and atomic writes |
| `embedding_api` | Shared profile, queue, and result REST dispatch |
| `RMMServer.embedding_store` | Lazy server-mode database, refused in workspace mode |
| `RMMHandler._handle_embeddings`, nested `queue`/`event` | Server scope guard, execution, exact history lookup |
| `WorkspaceService._embedding_api`, nested `queue`/`event` | Local persistence, central execution forwarding, owned event lookup |
| `RmmApiClient.embedding_profiles`, `run_embeddings`, `embedding_results` | CLI/MCP HTTP operations |
| `cmd_embeddings` | CLI JSON import/export and command dispatch |
| `tool_embedding_profiles`, `tool_run_embeddings`, `tool_embedding_results` | Shared AI/MCP tool handlers |
| MCP `embedding_profiles`, `run_embeddings`, `embedding_results` | Typed FastMCP wrappers |
| JS `message`, `request`, `perform` | Status messages, authenticated API calls, busy/error handling |
| JS `readForm`, `fillForm`, `download` | Edit/import/export configuration and result JSON |
| JS `refreshProfiles`, `refreshResults` | Load saved data and create safe text-only result controls |
| `_check_embedding_contract` | Check editor HTML/controller/documentation wiring in `make check` |
| JS test `fixture`, `Element`, `request`, `get`, `click`, `change` | Minimal DOM/API adapter for controller tests without a browser |
| `EmbeddingValidationTests`, `EmbeddingWorkspaceTests`, `EmbeddingServerTests`, `output_event` | Validation, reopen persistence, owner isolation, exact-event selection, HTTP/CLI/server checks |

Run `make check`, the embedding/workspace/plugin Python tests, and
`node --check web/embeddings.js`, and `node --test tests/test_embeddings_ui.cjs`. The native DLL's two-model acceptance is
recorded separately in [the progress log](progress.md).
