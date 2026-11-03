---
id: web-file-upload
title: File-upload trust-boundary investigation
description: Investigate upload validation, storage, retrieval, and conversion behavior with identifiable lab files; load for file handling or upload vulnerabilities.
default: false
---

# File-upload trust-boundary investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Follow the complete upload lifecycle

Map acceptance, temporary storage, validation, conversion, final storage, and
retrieval. Retain one valid baseline file, its hash, owner, content type,
filename, returned object ID, and download URL.
An upload accepted with HTTP 200 is not evidence that its contents execute.

## Choose one boundary to test

| Boundary | Controlled comparison | Evidence |
|----------|-----------------------|----------|
| Filename/extension | Same benign bytes with one changed name | Validation/storage-name behavior. |
| Declared MIME | Same file with changed declared media type | Whether the declaration affects handling. |
| Content identification | Valid small fixture versus mismatched harmless content | Content sniffing or decoder behavior. |
| Retrieval origin | Fetch accepted file as another designated lab viewer | Content disposition, media type, authorization, browser context. |
| Path handling | Unique lab filename with a normalization edge case | Actual canonical destination, not just echoed name. |
| Conversion | Compare original and derived artifact | Which parser touched the content and with what result. |

Change one dimension at a time; keep filenames unique and avoid overwriting
existing files. Use small files and archives. Large payloads and expansion
bombs test availability and are not part of routine validation.

## Bound the proof

Start with inert text/image fixtures. To assess executable serving, first
establish the server's handling and location using safe markers and observed
configuration; do not upload a persistent web shell as a default proof.
For stored browser content, use `web-xss` and an isolated lab viewer.
For XML document parsing, load `web-xxe` only for that branch.

A path-traversal finding needs a proven unauthorized destination or retrieval
boundary, not merely acceptance of "../" in a filename. Keep any intended
filesystem write within a designated disposable lab location; pre-existing
file replacement is governed by `operator-boundaries`.

## Interpret and continue

Account for antivirus quarantine, asynchronous validation, transcoding,
renaming, signed URLs, and caches. Poll the recorded job instead of uploading
again on timeout. Compare downloaded bytes/hashes with the submitted fixture.
A served file may be harmless under attachment disposition or a separate
origin; record these constraints rather than claiming execution.

## Detection and handoff

Correlate multipart upload, file/object metadata, converter events, and
retrieval. Report expected versus observed telemetry separately. Handoff the
artifact IDs/hashes, exact control pair, final location/handling evidence, and
cleanup status for agent-created test files.

Adapted from [Claude-Red offensive-file-upload](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-file-upload/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).
