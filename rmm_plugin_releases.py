"""Fetch verified, versioned plugin releases at the operator storage owner."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from rmm_asset_store import AssetError, AssetStore, _max_bytes, validate_asset_name

RELEASE_TAG = re.compile(r"plugins-[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


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

    def __init__(self):
        self.repository = os.environ.get("RMM_PLUGIN_REPOSITORY", "CyrilleFranchet/Minimal-RMM-plugins").strip()
        if not REPOSITORY.fullmatch(self.repository) or any(p in {".", ".."} for p in self.repository.split("/")):
            raise ReleaseError("RMM_PLUGIN_REPOSITORY must be owner/repository")
        self.token = os.environ.get("RMM_PLUGIN_GITHUB_TOKEN", "").strip()
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

    def resolve(self, tag: str = "latest") -> tuple[dict, dict]:
        """Resolve a published release and its strict, commit-pinned manifest."""
        if not isinstance(tag, str) or (tag != "latest" and not RELEASE_TAG.fullmatch(tag)):
            raise ReleaseError("tag must be latest or plugins- followed by a full commit SHA")
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
        if (not isinstance(manifest, dict) or manifest.get("schema") != "minimal-rmm-plugin-bundle-v1"
                or manifest.get("source_commit", manifest.get("version")) != resolved[8:]):
            raise ReleaseError("manifest does not match the release commit")
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
        return {"repository": self.repository, "tag": resolved, "version": resolved[8:], "plugins": plugins}, indexed

    def install(self, store: AssetStore, tag: str, name: str, expected: str = "") -> dict:
        """Install one verified file, reusing matching bytes already in the store."""
        if not isinstance(tag, str) or not RELEASE_TAG.fullmatch(tag):
            raise ReleaseError("installation requires an exact plugins-<commit SHA> tag; browse latest first")
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
        return {"asset": row, "release": {"repository": self.repository, "tag": tag}, "cached": cached}


def plugin_release_api(store: AssetStore, method: str, *, tag="latest", body=None, publish=None):
    """Share release browsing/install behavior across server and workspace APIs."""
    try:
        client = PluginReleases()
        if method == "GET":
            return 200, client.resolve(tag)[0]
        if not isinstance(body, dict) or set(body) - {"tag", "name", "sha256"}:
            raise ReleaseError("expected tag, name, and optional sha256")
        result = client.install(store, body.get("tag"), body.get("name"), body.get("sha256", ""))
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
