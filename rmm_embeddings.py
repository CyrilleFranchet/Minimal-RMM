"""Saved embedding profiles and explicitly selected results for one storage owner."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

PLUGIN = "document-embeddings-autonomous.dll"
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / "examples" / "embedding-profile.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class EmbeddingError(ValueError):
    """Invalid embedding configuration, request, or result."""


def encode(value):
    """Encode strict compact JSON for storage and the plugin byte limit."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def text(value, label, limit=1024, empty=False, multiline=False):
    """Validate a bounded string without control characters."""
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > limit or any(ord(c) < 32 and not (multiline and c in "\n\t") for c in value):
        raise EmbeddingError(f"invalid {label}")
    return value


def fields(value, allowed, label):
    """Reject unexpected fields, including credentials and ownership selectors."""
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise EmbeddingError(f"invalid {label} fields")
    return value


def profile_config(value):
    """Validate and normalize a reusable secret-free plugin configuration."""
    fields(value, ("model", "auth", "prompt", "label_prompt", "max_tokens", "labels", "copy_documents", "copy_labels", "copy_threshold"), "profile")
    model = fields(value.get("model"), ("repo", "revision", "onnx_file", "tokenizer_file", "sha256", "input_names", "output_name", "pooling"), "model")
    if not isinstance(model.get("repo"), str) or not REPO.fullmatch(model["repo"]) or ".." in model["repo"]:
        raise EmbeddingError("model.repo must be owner/repository")
    for key, length in (("revision", 40), ("sha256", 64)):
        if not isinstance(model.get(key), str) or not re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", model[key]):
            raise EmbeddingError(f"model.{key} must be a {length}-character hex digest")
    for key in ("onnx_file", "tokenizer_file"):
        path = text(model.get(key), key, 256)
        suffix = ".onnx" if key == "onnx_file" else ".json"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", path) or ".." in path or any(p in ("", ".") for p in path.split("/")) or not path.lower().endswith(suffix):
            raise EmbeddingError(f"invalid model.{key}")
    inputs = model.get("input_names")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 3:
        raise EmbeddingError("model.input_names needs one to three tensor names")
    for name in inputs:
        text(name, "input name", 128)
    if len(set(inputs)) != len(inputs):
        raise EmbeddingError("duplicate input names")
    text(model.get("output_name"), "output name", 128)
    if model.get("pooling") not in ("mean", "cls", "direct"):
        raise EmbeddingError("pooling must be mean, cls, or direct")
    auth = fields(value.get("auth", {"method": "none"}), ("method",), "auth")
    if auth.get("method") not in ("none", "env"):
        raise EmbeddingError("auth.method must be none or env (agent HF_TOKEN)")
    tokens = value.get("max_tokens", 256)
    if type(tokens) is not int or not 8 <= tokens <= 512:
        raise EmbeddingError("max_tokens must be between 8 and 512")
    labels = value.get("labels", [])
    if not isinstance(labels, list) or len(labels) > 16:
        raise EmbeddingError("at most 16 labels are supported")
    for label in labels:
        fields(label, ("name", "text"), "label")
        text(label.get("name"), "label name", 128)
        text(label.get("text"), "label text", 512, multiline=True)
    if len({label["name"] for label in labels}) != len(labels):
        raise EmbeddingError("duplicate label names")
    if "copy_documents" in value and type(value["copy_documents"]) is not bool:
        raise EmbeddingError("copy_documents must be a boolean")
    copy_documents = bool(value.get("copy_documents", False))
    copy_labels = value.get("copy_labels", [])
    copy_threshold = value.get("copy_threshold", 0)
    if not copy_documents and (copy_labels or copy_threshold):
        raise EmbeddingError("copy_labels and copy_threshold require copy_documents")
    if not isinstance(copy_labels, list) or len(copy_labels) > 16:
        raise EmbeddingError("at most 16 copy labels are supported")
    label_names = {label["name"] for label in labels}
    for name in copy_labels:
        text(name, "copy label", 128)
        if name not in label_names:
            raise EmbeddingError("copy labels must reference defined label names")
    if type(copy_threshold) not in (int, float) or not 0 <= copy_threshold <= 1:
        raise EmbeddingError("copy_threshold must be between 0 and 1")
    if copy_threshold > 0 and not labels:
        raise EmbeddingError("copy_threshold requires labels")
    result = dict(model=dict(model), auth=dict(auth), max_tokens=tokens, labels=labels,
                  prompt=text(value.get("prompt", ""), "prompt", 512, empty=True, multiline=True),
                  label_prompt=text(value.get("label_prompt", ""), "label_prompt", 512, empty=True, multiline=True),
                  copy_documents=copy_documents, copy_labels=copy_labels, copy_threshold=copy_threshold)
    if len(encode(result).encode()) > 4000:
        raise EmbeddingError("profile leaves insufficient room in the 4096-byte plugin input")
    return result


def default_profile():
    """Load and validate the repository-owned default without changing saved profiles."""
    return profile_config(json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8")))


def plugin_request(config, documents):
    """Build a bounded request for explicit documents without silent truncation."""
    if not isinstance(documents, list) or not 1 <= len(documents) <= 32:
        raise EmbeddingError("provide one to 32 document paths")
    for path in documents:
        text(path, "document path", 1024)
    value = encode({**profile_config(config), "documents": documents})
    if len(value.encode("utf-8")) > 4096:
        raise EmbeddingError("plugin input exceeds 4096 bytes; reduce the document batch or profile")
    return {"plugin": PLUGIN, "export": "Run", "input": value}


def result_payload(event):
    """Validate explicitly selected output before saving an embedding collection."""
    if not isinstance(event, dict) or event.get("type") != "output":
        raise EmbeddingError("select an output event containing embedding JSON")
    raw = event.get("body")
    if not isinstance(raw, str) or len(raw.encode()) > 8 * 1024 * 1024:
        raise EmbeddingError("invalid embedding result size")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise EmbeddingError("event does not contain embedding JSON") from exc
    fields(value, ("model", "revision", "copy_directory", "results"), "result")
    if not isinstance(value.get("model"), str) or not REPO.fullmatch(value["model"]):
        raise EmbeddingError("invalid result model")
    if not isinstance(value.get("revision"), str) or not re.fullmatch(r"[a-fA-F0-9]{40}", value["revision"]):
        raise EmbeddingError("invalid result revision")
    if value.get("copy_directory") is not None:
        text(value["copy_directory"], "copy directory", 1024)
    rows = value.get("results")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise EmbeddingError("invalid document results")
    dimension = None
    embedded = 0
    for row in rows:
        fields(row, ("path", "embedding", "classification", "error", "copied_to"), "document result")
        text(row.get("path"), "result path")
        vector = row.get("embedding")
        if row.get("error"):
            text(row["error"], "document error", 4096)
            if vector or row.get("classification") or row.get("copied_to"):
                raise EmbeddingError("failed document has an embedding, classification, or copy destination")
            continue
        embedded += 1
        if embedded > 32:
            raise EmbeddingError("too many embedded documents")
        if not isinstance(vector, list) or not 1 <= len(vector) <= 4096 or any(type(v) not in (int, float) or abs(v) > 3.4028235e38 or not math.isfinite(v) for v in vector):
            raise EmbeddingError("invalid embedding vector")
        if row.get("copied_to") is not None:
            text(row["copied_to"], "copied path", 1024)
        if dimension is not None and dimension != len(vector):
            raise EmbeddingError("inconsistent embedding dimensions")
        dimension = len(vector)
        classification = row.get("classification")
        if classification is not None:
            fields(classification, ("label", "score"), "classification")
            text(classification.get("label"), "classification label", 128)
            score = classification.get("score")
            if type(score) not in (int, float) or abs(score) > 1.0001 or not math.isfinite(score):
                raise EmbeddingError("invalid cosine score")
    return value


class EmbeddingStore:
    """Own profile and result tables in a server or workspace SQLite database."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS embedding_records (kind TEXT, name TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,name))")
        self.db.commit()

    def close(self):
        """Release the database connection."""
        with self.lock:
            self.db.close()

    def get(self, kind, name=None):
        """Read a named record or list records without full result vectors."""
        with self.lock:
            if name is not None:
                row = self.db.execute("SELECT payload FROM embedding_records WHERE kind=? AND name=?", (kind, name)).fetchone()
                return json.loads(row[0]) if row else None
            rows = self.db.execute("SELECT payload FROM embedding_records WHERE kind=? ORDER BY name", (kind,)).fetchall()
        return [{k: v for k, v in json.loads(row[0]).items() if k != "result"} for row in rows]

    def put(self, kind, name, payload):
        """Atomically upsert a validated record and its UTC update timestamp."""
        payload = {**payload, "name": name, "updated_at": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            self.db.execute("INSERT INTO embedding_records VALUES(?,?,?) ON CONFLICT(kind,name) DO UPDATE SET payload=excluded.payload", (kind, name, encode(payload)))
            self.db.commit()
        return payload

    def delete(self, kind, name):
        """Remove a named record without deleting its original session transcript."""
        with self.lock:
            cursor = self.db.execute("DELETE FROM embedding_records WHERE kind=? AND name=?", (kind, name))
            self.db.commit()
        return bool(cursor.rowcount)


def embedding_api(store, method, parts, body=None, queue=None, event=None):
    """Dispatch the same local-owner API for server, workspace, CLI, and MCP."""
    body = {} if body is None else body
    try:
        if parts == ["embeddings", "defaults"]:
            if method != "GET":
                return 405, {"error": "method_not_allowed"}
            try:
                return 200, {"config": default_profile()}
            except (OSError, ValueError, TypeError):
                return 500, {"error": "default_embedding_profile_unavailable"}
        if method == "POST" and parts == ["embeddings", "run"]:
            fields(body, ("profile", "session_ref", "documents"), "run")
            profile = store.get("profiles", text(body.get("profile"), "profile", 64))
            if profile is None:
                return 404, {"error": "profile_not_found"}
            return queue(text(body.get("session_ref"), "session reference", 128), plugin_request(profile["config"], body.get("documents")))
        if len(parts) not in (2, 3) or parts[0] != "embeddings" or parts[1] not in ("profiles", "results"):
            return 404, {"error": "not_found"}
        kind = parts[1]
        name = parts[2] if len(parts) == 3 else None
        if name is not None and not NAME.fullmatch(name):
            raise EmbeddingError("invalid record name")
        if method == "GET":
            record = store.get(kind, name)
            return (404, {"error": "not_found"}) if record is None else (200, {kind if name is None else "record": record})
        if method == "DELETE" and name:
            return (200, {"ok": True}) if store.delete(kind, name) else (404, {"error": "not_found"})
        if method == "POST" and kind == "profiles" and name:
            fields(body, ("config",), "profile request")
            return 200, {"record": store.put(kind, name, {"config": profile_config(body.get("config"))})}
        if method == "POST" and kind == "results" and name is None:
            fields(body, ("session_ref", "event_id"), "save result")
            event_id = body.get("event_id")
            if type(event_id) is not int or event_id < 1:
                raise EmbeddingError("event_id must be a positive integer")
            sid, selected = event(text(body.get("session_ref"), "session reference", 128), event_id)
            if selected is None:
                return 404, {"error": "event_not_found"}
            value = result_payload(selected)
            # Stable ID makes repeated saves of the same event idempotent.
            name = uuid.uuid5(uuid.NAMESPACE_URL, f"{sid}/{event_id}").hex
            return 200, {"record": store.put(kind, name, {"session_id": sid, "event_id": event_id, "model": value["model"], "document_count": len(value["results"]), "result": value})}
        return 405, {"error": "method_not_allowed"}
    except (EmbeddingError, TypeError, ValueError) as exc:
        return 400, {"error": str(exc)}
