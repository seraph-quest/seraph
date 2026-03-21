"""Starter-pack manager bridging manifest-backed and legacy bundled sources."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from typing import Any

from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.starter_packs.loader import StarterPack, load_legacy_starter_packs, scan_starter_pack_paths


def _legacy_extension_id(kind: str, source_path: str) -> str:
    fingerprint = hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:10]
    slug = os.path.basename(source_path).replace(".", "-").lower() or "root"
    return f"legacy.{kind}.{slug}-{fingerprint}"


class StarterPackManager:
    def __init__(self) -> None:
        self._packs: list[StarterPack] = []
        self._load_errors: list[dict[str, str]] = []
        self._shared_manifest_errors: list[dict[str, str]] = []
        self._legacy_path: str = ""
        self._manifest_roots: list[str] = []
        self._registry: ExtensionRegistry | None = None

    def init(self, legacy_path: str, *, manifest_roots: list[str] | None = None) -> None:
        self._legacy_path = legacy_path
        self._manifest_roots = list(manifest_roots or [os.path.join(os.path.dirname(legacy_path), "extensions")])
        self._registry = ExtensionRegistry(
            manifest_roots=self._manifest_roots,
            skill_dirs=[],
            workflow_dirs=[],
            mcp_runtime=None,
        )
        self._reload_from_registry()

    def _snapshot(self) -> ExtensionRegistrySnapshot:
        if self._registry is None:
            self._registry = ExtensionRegistry(
                manifest_roots=self._manifest_roots,
                skill_dirs=[],
                workflow_dirs=[],
                mcp_runtime=None,
            )
        return self._registry.snapshot()

    def _reload_from_registry(self) -> None:
        snapshot = self._snapshot()
        contribution_paths: list[str] = []
        contribution_index: dict[str, tuple[str, str]] = {}
        for contribution in snapshot.list_contributions("starter_packs"):
            resolved_path = contribution.metadata.get("resolved_path")
            path = str(resolved_path) if isinstance(resolved_path, str) and resolved_path else contribution.reference
            contribution_paths.append(path)
            contribution_index[os.path.abspath(path)] = (contribution.source, contribution.extension_id)

        manifest_packs, manifest_errors = scan_starter_pack_paths(contribution_paths)
        for pack in manifest_packs:
            source, extension_id = contribution_index.get(os.path.abspath(pack.file_path), ("legacy", None))
            pack.source = source
            pack.extension_id = extension_id

        legacy_packs, legacy_errors = load_legacy_starter_packs(self._legacy_path)
        legacy_extension_id = _legacy_extension_id("starter_packs", self._legacy_path)
        for pack in legacy_packs:
            pack.source = "legacy"
            pack.extension_id = legacy_extension_id

        deduped: list[StarterPack] = []
        load_errors: list[dict[str, str]] = []
        shared_manifest_errors: list[dict[str, str]] = []
        by_name: dict[str, StarterPack] = {}
        for pack in sorted([*manifest_packs, *legacy_packs], key=lambda item: (0 if item.source == "manifest" else 1, item.file_path, item.name)):
            existing = by_name.get(pack.name)
            if existing is not None:
                load_errors.append({
                    "file_path": pack.file_path,
                    "message": f"Duplicate starter pack '{pack.name}' from {pack.file_path}; keeping {existing.file_path}",
                    "phase": "duplicate-starter-pack-name",
                })
                continue
            by_name[pack.name] = pack
            deduped.append(pack)

        self._packs = deduped
        for error in snapshot.load_errors:
            payload = {
                "file_path": error.source,
                "message": error.message,
                "phase": error.phase,
            }
            if self._error_affects_starter_packs(error.source, error.phase, error.details):
                load_errors.append(payload)
                continue
            if error.phase in {"manifest", "compatibility", "layout"}:
                shared_manifest_errors.append(payload)

        self._load_errors = [
            *load_errors,
            *(
                {
                    "file_path": str(error.get("file_path") or ""),
                    "message": str(error.get("message") or "starter pack parse error"),
                    "phase": "manifest-starter-packs",
                }
                for error in manifest_errors
            ),
            *(
                {
                    "file_path": str(error.get("file_path") or ""),
                    "message": str(error.get("message") or "starter pack parse error"),
                    "phase": "legacy-starter-packs",
                }
                for error in legacy_errors
            ),
        ]
        self._shared_manifest_errors = shared_manifest_errors

    def _error_affects_starter_packs(
        self,
        source: str,
        phase: str,
        details: list[dict[str, Any]] | None = None,
    ) -> bool:
        if phase == "manifest":
            if details:
                for detail in details:
                    loc = detail.get("loc")
                    if (
                        isinstance(loc, list)
                        and len(loc) >= 2
                        and str(loc[0]) == "contributes"
                        and str(loc[1]) == "starter_packs"
                    ):
                        return True
                return False
        if phase in {"compatibility", "layout"}:
            for detail in details or []:
                contributed_types = detail.get("contributed_types")
                if isinstance(contributed_types, list) and "starter_packs" in contributed_types:
                    return True
        if phase not in {"manifest", "compatibility", "layout"}:
            return False
        package_root = source
        if os.path.basename(source) in {"manifest.yaml", "manifest.yml"}:
            package_root = os.path.dirname(source)
        return os.path.isdir(os.path.join(package_root, "starter-packs"))

    def list_packs(self) -> list[dict[str, Any]]:
        return [asdict(pack) for pack in self._packs]

    def is_initialized(self) -> bool:
        return self._registry is not None

    def get_pack(self, name: str) -> StarterPack | None:
        return next((pack for pack in self._packs if pack.name == name), None)

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "starter_packs": self.list_packs(),
            "load_errors": list(self._load_errors),
            "shared_manifest_errors": list(self._shared_manifest_errors),
            "loaded_count": len(self._packs),
            "error_count": len(self._load_errors),
            "shared_error_count": len(self._shared_manifest_errors),
        }


starter_pack_manager = StarterPackManager()
