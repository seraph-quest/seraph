"""Runbook manager bridging manifest-backed and loose runbook sources."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from typing import Any

from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.runbooks.loader import Runbook, scan_runbook_paths


def _legacy_extension_id(kind: str, source_path: str) -> str:
    fingerprint = hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:10]
    slug = os.path.basename(source_path).replace(".", "-").lower() or "root"
    return f"legacy.{kind}.{slug}-{fingerprint}"


class RunbookManager:
    def __init__(self) -> None:
        self._runbooks: list[Runbook] = []
        self._load_errors: list[dict[str, str]] = []
        self._shared_manifest_errors: list[dict[str, str]] = []
        self._runbooks_dir: str = ""
        self._manifest_roots: list[str] = []
        self._registry: ExtensionRegistry | None = None

    def init(self, runbooks_dir: str, *, manifest_roots: list[str] | None = None) -> None:
        self._runbooks_dir = runbooks_dir
        self._manifest_roots = list(manifest_roots or [os.path.join(os.path.dirname(runbooks_dir), "extensions")])
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
        contribution_index: dict[str, tuple[str, str | None, int]] = {}
        for contribution in snapshot.list_contributions("runbooks"):
            resolved_path = contribution.metadata.get("resolved_path")
            path = str(resolved_path) if isinstance(resolved_path, str) and resolved_path else contribution.reference
            contribution_paths.append(path)
            contribution_index[os.path.abspath(path)] = (
                contribution.source,
                contribution.extension_id,
                int(contribution.metadata.get("manifest_root_index", len(self._manifest_roots))),
            )

        manifest_runbooks, manifest_errors = scan_runbook_paths(contribution_paths)
        manifest_priority_by_path: dict[str, int] = {}
        for runbook in manifest_runbooks:
            source, extension_id, manifest_root_index = contribution_index.get(
                os.path.abspath(runbook.file_path),
                ("legacy", None, len(self._manifest_roots)),
            )
            runbook.source = source
            runbook.extension_id = extension_id
            manifest_priority_by_path[os.path.abspath(runbook.file_path)] = manifest_root_index

        legacy_runbook_paths = []
        if os.path.isdir(self._runbooks_dir):
            legacy_runbook_paths = [
                os.path.join(self._runbooks_dir, filename)
                for filename in sorted(os.listdir(self._runbooks_dir))
                if filename.endswith((".yaml", ".yml"))
            ]
        legacy_runbooks, legacy_errors = scan_runbook_paths(legacy_runbook_paths)
        legacy_extension_id = _legacy_extension_id("runbooks", self._runbooks_dir or "runbooks")
        for runbook in legacy_runbooks:
            runbook.source = "legacy"
            runbook.extension_id = legacy_extension_id

        deduped: list[Runbook] = []
        load_errors: list[dict[str, str]] = []
        shared_manifest_errors: list[dict[str, str]] = []
        by_id: dict[str, Runbook] = {}
        for runbook in sorted(
            [*manifest_runbooks, *legacy_runbooks],
            key=lambda item: (
                0 if item.source == "manifest" else 1,
                manifest_priority_by_path.get(os.path.abspath(item.file_path), len(self._manifest_roots)),
                item.file_path,
                item.id,
            ),
        ):
            existing = by_id.get(runbook.id)
            if existing is not None:
                load_errors.append({
                    "file_path": runbook.file_path,
                    "message": f"Duplicate runbook '{runbook.id}' from {runbook.file_path}; keeping {existing.file_path}",
                    "phase": "duplicate-runbook-id",
                })
                continue
            by_id[runbook.id] = runbook
            deduped.append(runbook)

        self._runbooks = deduped
        for error in snapshot.load_errors:
            payload = {
                "file_path": error.source,
                "message": error.message,
                "phase": error.phase,
            }
            if self._error_affects_runbooks(error.source, error.phase, error.details):
                load_errors.append(payload)
                continue
            if error.phase in {"manifest", "compatibility", "layout"}:
                shared_manifest_errors.append(payload)

        self._load_errors = [
            *load_errors,
            *(
                {
                    "file_path": str(error.get("file_path") or ""),
                    "message": str(error.get("message") or "runbook parse error"),
                    "phase": "manifest-runbooks",
                }
                for error in manifest_errors
            ),
            *(
                {
                    "file_path": str(error.get("file_path") or ""),
                    "message": str(error.get("message") or "runbook parse error"),
                    "phase": "legacy-runbooks",
                }
                for error in legacy_errors
            ),
        ]
        self._shared_manifest_errors = shared_manifest_errors

    def _error_affects_runbooks(
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
                        and str(loc[1]) == "runbooks"
                    ):
                        return True
                return False
        if phase in {"compatibility", "layout"}:
            for detail in details or []:
                contributed_types = detail.get("contributed_types")
                if isinstance(contributed_types, list) and "runbooks" in contributed_types:
                    return True
        if phase not in {"manifest", "compatibility", "layout"}:
            return False
        package_root = source
        if os.path.basename(source) in {"manifest.yaml", "manifest.yml"}:
            package_root = os.path.dirname(source)
        return os.path.isdir(os.path.join(package_root, "runbooks"))

    def list_runbooks(self) -> list[dict[str, Any]]:
        return [asdict(runbook) for runbook in self._runbooks]

    def is_initialized(self) -> bool:
        return self._registry is not None

    def get_runbook(self, runbook_id: str) -> Runbook | None:
        return next((runbook for runbook in self._runbooks if runbook.id == runbook_id), None)

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "runbooks": self.list_runbooks(),
            "load_errors": list(self._load_errors),
            "shared_manifest_errors": list(self._shared_manifest_errors),
            "loaded_count": len(self._runbooks),
            "error_count": len(self._load_errors),
            "shared_error_count": len(self._shared_manifest_errors),
        }


runbook_manager = RunbookManager()
