"""Fixed reviewed profile and package/toolchain validation; no dynamic loading."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from typing import Any

from .protocol import ProtocolError, closed, decode_json

CORDIS_VERSION = "4.0.0-rc.10"
CORDIS_INTEGRITY = "sha512-xG90nPNQxR272cC4lR/m5LHevegIJvdddQBlKdEAdGz3n+zgH5lsgkg8o9fc2P3T/f+pO5D7FN1HZvkNBiABnw=="
NPM_VERSION = "11.8.0"
PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "cordis"
BUILD_FILES = (
    'package.json',
    'package-lock.json',
    'profile.json',
    'tsconfig.json',
    'scripts/toolchain.mjs',
    'scripts/build-manifest.mjs',
    'src/bootstrap.ts',
    'src/composition.ts',
    'src/protocol.ts',
    'src/resources.ts',
    'src/contracts/client.ts',
    'src/contracts/methods.ts',
    'src/contracts/schema.ts',
    'src/contracts/state.ts',
    'src/contracts/authority.ts',
    'src/contracts/goals.ts',
    'src/contracts/tasks.ts',
    'src/contracts/capabilities.ts',
    'src/contracts/inference.ts',
    'src/contracts/memory.ts',
    'src/contracts/artifacts.ts',
    'src/contracts/audit.ts',
    'src/contracts/research.ts',
    'src/contracts/conversation.ts',
    'src/contracts/scheduler.ts',
    'src/contracts/connections.ts',
    'src/contracts/agent_loop.ts',
    'src/contracts/source_extraction.ts',
    'src/plugins/index.ts',
    'src/plugins/proxy.ts',
    'src/plugins/state/index.ts',
    'src/plugins/authority/index.ts',
    'src/plugins/goals/index.ts',
    'src/plugins/tasks/index.ts',
    'src/plugins/capabilities/index.ts',
    'src/plugins/inference/index.ts',
    'src/plugins/memory/index.ts',
    'src/plugins/artifacts/index.ts',
    'src/plugins/audit/index.ts',
    'src/plugins/research/index.ts',
    'src/plugins/conversation/index.ts',
    'src/plugins/scheduler/index.ts',
    'src/plugins/connections/index.ts',
    'src/plugins/agent_loop/index.ts',
    'src/plugins/source_extraction/index.ts',
    'src/plugins/source_extraction/html.ts',
    'src/plugins/source_extraction/html-entities.ts',
    'src/plugins/source_extraction/rss-atom.ts',
    'src/plugins/research/guardian.ts',
    'src/providers/goal-plan.ts',
    'dist/src/bootstrap.js',
    'dist/src/composition.js',
    'dist/src/protocol.js',
    'dist/src/resources.js',
    'dist/src/contracts/client.js',
    'dist/src/contracts/methods.js',
    'dist/src/contracts/schema.js',
    'dist/src/contracts/state.js',
    'dist/src/contracts/authority.js',
    'dist/src/contracts/goals.js',
    'dist/src/contracts/tasks.js',
    'dist/src/contracts/capabilities.js',
    'dist/src/contracts/inference.js',
    'dist/src/contracts/memory.js',
    'dist/src/contracts/artifacts.js',
    'dist/src/contracts/audit.js',
    'dist/src/contracts/research.js',
    'dist/src/contracts/conversation.js',
    'dist/src/contracts/scheduler.js',
    'dist/src/contracts/connections.js',
    'dist/src/contracts/agent_loop.js',
    'dist/src/contracts/source_extraction.js',
    'dist/src/plugins/index.js',
    'dist/src/plugins/proxy.js',
    'dist/src/plugins/state/index.js',
    'dist/src/plugins/authority/index.js',
    'dist/src/plugins/goals/index.js',
    'dist/src/plugins/tasks/index.js',
    'dist/src/plugins/capabilities/index.js',
    'dist/src/plugins/inference/index.js',
    'dist/src/plugins/memory/index.js',
    'dist/src/plugins/artifacts/index.js',
    'dist/src/plugins/audit/index.js',
    'dist/src/plugins/research/index.js',
    'dist/src/plugins/conversation/index.js',
    'dist/src/plugins/scheduler/index.js',
    'dist/src/plugins/connections/index.js',
    'dist/src/plugins/agent_loop/index.js',
    'dist/src/plugins/source_extraction/index.js',
    'dist/src/plugins/source_extraction/html.js',
    'dist/src/plugins/source_extraction/html-entities.js',
    'dist/src/plugins/source_extraction/rss-atom.js',
    'dist/src/plugins/research/guardian.js',
    'dist/src/providers/goal-plan.js',
)
PACKAGE_FILES = (*BUILD_FILES, "dist/build-manifest.json")
# Child gets no PATH/HOME/config or credentials. Absolute argv needs no lookup.
CHILD_ENV = {"LANG": "C.UTF-8", "TZ": "UTC"}
KNOWN_PLUGIN = "seraph.host-lifecycle@1.0.0"
KNOWN_PLUGINS = (KNOWN_PLUGIN, 'seraph.state.v1', 'seraph.authority.v1', 'seraph.goals.v1', 'seraph.tasks.v1', 'seraph.capabilities.v1', 'seraph.inference.v1', 'seraph.memory.v1', 'seraph.artifacts.v1', 'seraph.audit.v1', 'seraph.research.v1', 'seraph.conversation.v1', 'seraph.scheduler.v1', 'seraph.connections.v1', 'seraph.agent-loop.v1', 'seraph.source-extraction.v1', 'seraph.goal-plan.v1', 'seraph.guardian-research.v1')


# Python always hosts state, authority and audit (#1024); their facades and the
# host plugin can never be removed or made optional by profile configuration.
MANDATORY_PLUGINS = (KNOWN_PLUGIN, 'seraph.state.v1', 'seraph.authority.v1', 'seraph.audit.v1')
# Declared, fixed provider dependencies (mirror of runtime/cordis/src/composition.ts
# and runtime_plugins.worker.DEPENDENCIES); a profile can neither add nor drop one.
PLUGIN_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    'seraph.authority.v1': ('seraph.state.v1',),
    'seraph.audit.v1': ('seraph.state.v1',),
    'seraph.tasks.v1': ('seraph.state.v1', 'seraph.authority.v1'),
    'seraph.goals.v1': ('seraph.state.v1', 'seraph.authority.v1'),
    'seraph.artifacts.v1': ('seraph.state.v1', 'seraph.authority.v1'),
    'seraph.memory.v1': ('seraph.state.v1', 'seraph.authority.v1', 'seraph.audit.v1'),
}
# Closed per-plugin configuration fields; only source extraction is configurable.
PLUGIN_CONFIG_FIELDS: dict[str, frozenset[str]] = {identifier: frozenset() for identifier in KNOWN_PLUGINS}
PLUGIN_CONFIG_FIELDS["seraph.source-extraction.v1"] = frozenset({"formats"})
# Mirror of runtime/cordis/src/plugins/source_extraction/index.ts.
SOURCE_MEDIA_TYPES = ("text/plain", "text/html", "application/rss+xml", "application/atom+xml",
                      "application/xml", "text/xml")
SOURCE_FORMAT_PROVIDERS = frozenset({"plain-text", "html-text", "rss-atom"})
# Provider implementations beyond each domain's own index/contract files.
EXTRA_PLUGIN_FILES = {
    "seraph.source-extraction.v1": ("src/plugins/source_extraction/html.ts", "src/plugins/source_extraction/html-entities.ts",
                                    "src/plugins/source_extraction/rss-atom.ts"),
    "seraph.goal-plan.v1": ("src/providers/goal-plan.ts", "src/contracts/research.ts"),
    "seraph.guardian-research.v1": ("src/plugins/research/guardian.ts", "src/contracts/research.ts"),
}


class CompositionBlocked(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReviewedComposition:
    root: Path
    node: Path
    node_version: str
    profile: dict[str, Any]
    composition_digest: str
    package_digest: str
    # Per-plugin implementation/config identity derived from the verified
    # build manifest; the child must report exactly these digests.
    plugin_digests: dict[str, str] | None = None

    @property
    def entrypoint(self) -> Path:
        return self.root / "dist/src/bootstrap.js"


def validate_profile(value: Any) -> dict[str, Any]:
    profile = closed(value, {"protocol", "profile_id", "plugins"})
    if type(profile["protocol"]) is not int or profile["protocol"] != 1 or profile["profile_id"] != "seraph-cordis-bootstrap-v1":
        raise ProtocolError("invalid reviewed profile")
    plugins = profile["plugins"]
    if type(plugins) is not list or not 1 <= len(plugins) <= len(KNOWN_PLUGINS):
        raise ProtocolError("required host plugin missing")
    cursor = 0
    for spec in plugins:
        closed(spec, {"id", "required", "dependencies", "config"})
        # Literal registry order: no duplicates or unknown IDs. Providers
        # outside the mandatory set may be optional; their methods then
        # return typed blocks.
        try:
            position = KNOWN_PLUGINS.index(spec["id"], cursor)
        except ValueError as exc:
            raise ProtocolError("unknown or invalid plugin") from exc
        if (type(spec["required"]) is not bool or type(spec["dependencies"]) is not list
                or spec["dependencies"] != list(PLUGIN_DEPENDENCIES.get(spec["id"], ()))):
            raise ProtocolError("unknown or invalid plugin")
        cursor = position + 1
        closed(spec["config"], set(PLUGIN_CONFIG_FIELDS[spec["id"]]))
        if spec["id"] == "seraph.source-extraction.v1":
            formats = spec["config"]["formats"]
            if (type(formats) is not dict or len(formats) > len(SOURCE_MEDIA_TYPES)
                    or any(key not in SOURCE_MEDIA_TYPES or value not in SOURCE_FORMAT_PROVIDERS
                           for key, value in formats.items())):
                raise ProtocolError("invalid source formats")
    if plugins[0]["id"] != KNOWN_PLUGIN or plugins[0]["required"] is not True:
        raise ProtocolError("required host plugin missing")
    required = {spec["id"] for spec in plugins if spec["required"] is True}
    if any(identifier not in required for identifier in MANDATORY_PLUGINS):
        raise ProtocolError("required provider missing")
    return profile


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plugin_files(identifier: str) -> tuple[str, ...]:
    """Mirror of runtime/cordis/src/composition.ts ``pluginFiles``."""
    if identifier == KNOWN_PLUGIN:
        return ("src/composition.ts", "dist/src/composition.js")
    match = re.fullmatch(r"seraph\.([a-z-]+)\.v1", identifier)
    if match is None or identifier not in KNOWN_PLUGINS:
        raise ProtocolError("unknown plugin")
    common = ("src/plugins/proxy.ts", "dist/src/plugins/proxy.js", "src/contracts/methods.ts",
              "dist/src/contracts/methods.js", "src/contracts/schema.ts", "dist/src/contracts/schema.js")
    extra = tuple(item for path in EXTRA_PLUGIN_FILES.get(identifier, ())
                  for item in (path, "dist/" + path[:-3] + ".js"))
    if identifier in {"seraph.goal-plan.v1", "seraph.guardian-research.v1"}:
        return (*extra, *common)
    name = match[1].replace("-", "_", 1)
    return (f"src/plugins/{name}/index.ts", f"dist/src/plugins/{name}/index.js", f"src/contracts/{name}.ts",
            f"dist/src/contracts/{name}.js", *common, *extra)


def plugin_config_digest(identifier: str, profile: dict[str, Any]) -> str:
    """Digest of one plugin's reviewed config, as the Cordis provider reports it."""
    spec = next(item for item in profile["plugins"] if item["id"] == identifier)
    return hashlib.sha256(_canonical(spec["config"]).encode()).hexdigest()


def plugin_digest(spec: dict[str, Any], files: dict[str, str]) -> str:
    hashes = {path: files[path] for path in plugin_files(spec["id"])}
    return hashlib.sha256(_canonical({"id": spec["id"], "required": spec["required"], "config": spec["config"],
                                      "files": hashes}).encode()).hexdigest()


def _trusted_file(path: Path, *, executable: bool = False) -> None:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & stat.S_IWOTH or metadata.st_uid not in {os.getuid(), 0} or (executable and not os.access(path, os.X_OK)):
            raise CompositionBlocked("unreviewed_runtime_path")
    except OSError as exc:
        raise CompositionBlocked("runtime_package_missing") from exc


def reviewed_node(node_path: Path | None = None) -> tuple[Path, str]:
    if node_path is None:
        found = shutil.which("node")
        if found is None:
            raise CompositionBlocked("node_missing")
        node_path = Path(found)
    try:
        node = node_path.resolve(strict=True)
    except OSError as exc:
        raise CompositionBlocked("node_missing") from exc
    _trusted_file(node, executable=True)
    try:
        result = subprocess.run([str(node), "--version"], env=CHILD_ENV, close_fds=True,
                                capture_output=True, timeout=1, check=True)
        version = result.stdout.decode("ascii").strip()
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
        if match is None or not ((int(match[1]) == 22 and int(match[2]) >= 12) or int(match[1]) == 24):
            raise CompositionBlocked("node_unsupported")
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise CompositionBlocked("node_unavailable") from exc
    return node, version


def reviewed_composition(*, node_path: Path | None = None, root: Path = PACKAGE_ROOT) -> ReviewedComposition:
    node, version = reviewed_node(node_path)
    try:
        if root != root.resolve(strict=True) or not root.is_dir() or root.stat().st_mode & stat.S_IWOTH:
            raise CompositionBlocked("unreviewed_runtime_path")
        for name in PACKAGE_FILES:
            path = root / name
            if path.resolve(strict=True) != path:
                raise CompositionBlocked("unreviewed_runtime_path")
            _trusted_file(path)
        if not (root / "node_modules/cordis/package.json").is_file():
            # Locked dependencies were never installed (npm ci from the lockfile).
            raise CompositionBlocked("build_dependencies_missing")
        profile = validate_profile(decode_json((root / "profile.json").read_bytes()))
        manifest = decode_json((root / "package.json").read_bytes())
        lock = decode_json((root / "package-lock.json").read_bytes())
        installed_path = root / "node_modules/cordis/package.json"
        if installed_path.resolve(strict=True) != installed_path:
            raise CompositionBlocked("unreviewed_runtime_path")
        _trusted_file(installed_path)
        installed = decode_json(installed_path.read_bytes())
        if manifest.get("packageManager") != f"npm@{NPM_VERSION}" or manifest.get("dependencies", {}).get("cordis") != CORDIS_VERSION or lock.get("lockfileVersion") != 3:
            raise CompositionBlocked("package_pin_mismatch")
        package = lock.get("packages", {}).get("node_modules/cordis", {})
        if package.get("version") != CORDIS_VERSION or package.get("integrity") != CORDIS_INTEGRITY or installed.get("version") != CORDIS_VERSION:
            raise CompositionBlocked("package_pin_mismatch")
        build = closed(decode_json((root / "dist/build-manifest.json").read_bytes()), {"format", "npm_version", "files"})
        files = closed(build["files"], set(BUILD_FILES))
        if type(build["format"]) is not int or build["format"] != 1 or build["npm_version"] != NPM_VERSION:
            raise CompositionBlocked("build_receipt_invalid")
        digest = hashlib.sha256()
        for name in PACKAGE_FILES:
            contents = (root / name).read_bytes()
            if name in files and files[name] != hashlib.sha256(contents).hexdigest():
                raise CompositionBlocked("runtime_build_stale")
            digest.update(name.encode("ascii") + b"\0" + contents + b"\0")
        composition_digest = hashlib.sha256(json.dumps(profile, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        digests = {spec["id"]: plugin_digest(spec, files) for spec in profile["plugins"]}
        return ReviewedComposition(root, node, version, profile, composition_digest, digest.hexdigest(), digests)
    except CompositionBlocked:
        raise
    except (OSError, ProtocolError, AttributeError, TypeError, KeyError, ValueError) as exc:
        raise CompositionBlocked("runtime_package_invalid_or_missing") from exc
