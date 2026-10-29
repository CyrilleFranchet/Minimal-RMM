"""Fetch verified, versioned plugin releases at the operator storage owner."""
from __future__ import annotations

from rmm_settings import getenv as settings_getenv

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from rmm_asset_store import AssetError, AssetStore, _max_bytes, validate_asset_name
from rmm_plugin_sources import PluginSourceStore, normalize_source

LEGACY_TAG = re.compile(r"plugins-[0-9a-f]{40}")
VERSION_TAG = re.compile(r"plugins-([0-9a-f]{12})-v([1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
RELEASE_TAG = re.compile(r"(?:plugins-[0-9a-f]{40}|plugins-[0-9a-f]{12}-v[1-9][0-9]{0,8}\.(?:0|[1-9][0-9]{0,8}))")


class ReleaseError(AssetError):
    """Report a safe release error without exposing credentials or signed URLs."""


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    """Allow GitHub HTTPS asset redirects while dropping authorization headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or parsed.hostname not in {"api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}):
            raise ReleaseError("GitHub returned an unsupported asset redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


class PluginReleases:
    """Read one configured GitHub repository and verify its release manifests."""

    def __init__(self, source=None):
        source = source or {"id": "default", "repository": settings_getenv("RMM_PLUGIN_REPOSITORY", "CyrilleFranchet/Minimal-RMM-plugins"),
                            "branch": "main", "credential_env": "RMM_PLUGIN_GITHUB_TOKEN", "label": "Default"}
        config = normalize_source(source["id"], {key: source[key] for key in ("label", "repository", "branch", "credential_env")})
        self.repository = config["repository"]
        self.branch = config["branch"]
        self.branch_key = hashlib.sha256(self.branch.encode("utf-8")).hexdigest()[:12]
        self.token = settings_getenv(config["credential_env"], "").strip()
        self.base = f"https://api.github.com/repos/{self.repository}"
        self.opener = urllib.request.build_opener(ReleaseRedirect())

    def _request(self, endpoint: str, limit: int, *, binary: bool = False) -> bytes:
        """Fetch bounded GitHub content; credentials go only to the API origin."""
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "Minimal-RMM/plugin-releases", "X-GitHub-Api-Version": "2022-11-28"}
        request = urllib.request.Request(self.base + endpoint, headers=headers)
        if self.token:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(request, timeout=60) as response:
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise ReleaseError("GitHub asset exceeds the configured size limit")
                return content
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ReleaseError(f"GitHub returned HTTP {exc.code}; check the release and repository Contents read permission") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError("GitHub download failed; check connectivity and repository configuration") from None

    def _asset(self, asset: dict, limit: int) -> bytes:
        """Download a repository asset by numeric ID and verify GitHub's digest."""
        asset_id = asset.get("id")
        if type(asset_id) is not int or asset_id < 1:
            raise ReleaseError("invalid release asset ID")
        size = asset.get("size")
        if type(size) is not int or size < 1 or size > limit:
            raise ReleaseError("release asset exceeds the configured size limit")
        content = self._request(f"/releases/assets/{asset_id}", limit, binary=True)
        digest = asset.get("digest")
        if len(content) != size or (digest and digest != "sha256:" + hashlib.sha256(content).hexdigest()):
            raise ReleaseError("GitHub release asset checksum or size mismatch")
        return content

    def _latest_tag(self) -> str:
        """Select the greatest numeric version in this branch's tag namespace."""
        refs = json.loads(self._request(f"/git/matching-refs/tags/plugins-{self.branch_key}-v", 2 * 1024 * 1024))
        if not isinstance(refs, list):
            raise ReleaseError("invalid GitHub release tag list")
        versions = []
        for ref in refs:
            name = ref.get("ref", "") if isinstance(ref, dict) else ""
            if not isinstance(name, str) or not name.startswith("refs/tags/"):
                continue
            tag = name[len("refs/tags/"):]
            match = VERSION_TAG.fullmatch(tag)
            if match and match[1] == self.branch_key:
                versions.append((int(match[2]), int(match[3]), tag))
        if versions:
            return max(versions)[2]
        if self.branch != "main":
            raise ReleaseError("no published plugin version for this branch; publish it from the plugin repository first")
        # Existing installations published SHA tags before branch versions existed.
        return "latest"

    def resolve(self, tag: str = "latest") -> tuple[dict, dict]:
        """Resolve this branch's version and validate its source/manifest identity."""
        if not isinstance(tag, str) or (tag != "latest" and not RELEASE_TAG.fullmatch(tag)):
            raise ReleaseError("tag must be latest or an exact published plugin tag")
        if tag == "latest":
            tag = self._latest_tag()
        endpoint = "/releases/latest" if tag == "latest" else f"/releases/tags/{tag}"
        release = json.loads(self._request(endpoint, 2 * 1024 * 1024))
        if not isinstance(release, dict):
            raise ReleaseError("invalid GitHub release response")
        resolved = release.get("tag_name", "")
        if (not isinstance(resolved, str) or not RELEASE_TAG.fullmatch(resolved)
                or release.get("draft") or release.get("prerelease") or (tag != "latest" and tag != resolved)):
            raise ReleaseError("expected a published, commit-pinned plugin release")
        assets = release.get("assets")
        if not isinstance(assets, list) or not all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in assets):
            raise ReleaseError("invalid release assets")
        indexed = {a["name"]: a for a in assets}
        if len(indexed) != len(assets) or "plugins.json" not in indexed:
            raise ReleaseError("release manifest missing or duplicate asset names")
        manifest = json.loads(self._asset(indexed["plugins.json"], 1024 * 1024))
        if not isinstance(manifest, dict) or manifest.get("schema") != "minimal-rmm-plugin-bundle-v1":
            raise ReleaseError("invalid plugin manifest schema")
        commit = manifest.get("source_commit", manifest.get("version"))
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ReleaseError("manifest must contain the full source commit")
        version_match = VERSION_TAG.fullmatch(resolved)
        if version_match:
            version = f"{version_match[2]}.{version_match[3]}"
            if (version_match[1] != self.branch_key or manifest.get("source_branch") != self.branch
                    or manifest.get("version") != version):
                raise ReleaseError("manifest version or branch does not match the selected source")
        else:
            version = commit
            if self.branch != "main" or manifest.get("source_branch", "main") != "main" or commit != resolved[8:]:
                raise ReleaseError("legacy release does not match the main branch or source commit")
        plugins = manifest.get("plugins")
        if not isinstance(plugins, list) or not plugins or len(plugins) > 100:
            raise ReleaseError("invalid plugin manifest")
        seen = set()
        for row in plugins:
            if not isinstance(row, dict):
                raise ReleaseError("invalid plugin manifest entry")
            name = row.get("name")
            if (not isinstance(name, str) or validate_asset_name("plugins", name) != name
                    or name in seen or name not in indexed):
                raise ReleaseError("invalid or missing plugin release asset")
            seen.add(name)
            if (type(row.get("size")) is not int or row["size"] < 1 or row["size"] != indexed[name].get("size")
                    or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
                raise ReleaseError("invalid plugin size or checksum")
        return {"repository": self.repository, "branch": self.branch, "tag": resolved,
                "version": version, "source_commit": commit, "plugins": plugins}, indexed

    def install(self, store: AssetStore, tag: str, name: str, expected: str = "") -> dict:
        """Install one verified file, reusing matching bytes already in the store."""
        if not isinstance(tag, str) or not RELEASE_TAG.fullmatch(tag):
            raise ReleaseError("installation requires an exact published tag; browse latest first")
        release, assets = self.resolve(tag)
        plugin = next((row for row in release["plugins"] if row["name"] == name), None)
        if plugin is None:
            raise ReleaseError("plugin is not present in the selected release")
        if expected and expected != plugin["sha256"]:
            raise ReleaseError("plugin checksum changed since selection; browse the release again")
        if plugin["size"] > _max_bytes():
            raise ReleaseError("plugin exceeds RMM_ASSET_MAX_BYTES")
        cached = False
        try:
            content = store.read("plugins", name, enabled_only=False)
            cached = len(content) == plugin["size"] and hashlib.sha256(content).hexdigest() == plugin["sha256"]
        except AssetError:
            pass
        if not cached:
            content = self._asset(assets[name], _max_bytes())
        if len(content) != plugin["size"] or hashlib.sha256(content).hexdigest() != plugin["sha256"]:
            raise ReleaseError("plugin checksum mismatch; installed file was not replaced")
        row = store.add_bytes("plugins", name, content,
                              source_url=f"https://github.com/{self.repository}/releases/tag/{tag}")
        return {"asset": row, "release": {key: release[key] for key in ("repository", "branch", "tag", "version", "source_commit")}, "cached": cached}


def plugin_release_api(store: AssetStore, method: str, *, tag="latest", source="default", body=None, publish=None):
    """Share release browsing/install behavior across server and workspace APIs."""
    try:
        if method != "GET":
            if not isinstance(body, dict) or set(body) - {"source", "source_revision", "tag", "name", "sha256"}:
                raise ReleaseError("expected source, tag, name, and optional sha256/source_revision")
            source = body.get("source", "default")
        config = PluginSourceStore(store.root).get(source)
        if method != "GET" and body.get("source_revision") and body["source_revision"] != config["revision"]:
            raise ReleaseError("plugin source changed since selection; browse it again")
        client = PluginReleases(config)
        if method == "GET":
            return 200, {**client.resolve(tag)[0], "source": source, "source_revision": config["revision"]}
        result = client.install(store, body.get("tag"), body.get("name"), body.get("sha256", ""))
        result.update(source=source, source_revision=config["revision"])
        try:
            synchronized = not publish or publish(result["asset"])
        except (OSError, AssetError):
            synchronized = False
        if not synchronized:
            return 502, {**result, "error": "plugin_runtime_sync_failed", "local_installed": True,
                         "detail": "Verified plugin saved locally; retry installation to synchronize the runtime cache."}
        return 200, result
    except (AssetError, ValueError, TypeError, KeyError) as exc:
        # Validation errors are safe; never return upstream bodies, URLs, or tokens.
        return 400, {"error": str(exc) if isinstance(exc, AssetError) else "invalid plugin release data"}
    except OSError:
        return 500, {"error": "plugin storage write failed"}
