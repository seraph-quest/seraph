"""Typed extension platform foundations for Seraph.

This package hosts the manifest, registry, and validation primitives for the
extension-platform migration described in the implementation roadmap.
"""

from .manifest import (
    ExtensionCompatibility,
    ExtensionContributionPaths,
    ExtensionManifest,
    ExtensionManifestError,
    ExtensionPermissions,
    ExtensionPublisher,
    load_extension_manifest,
    parse_extension_manifest,
)
from .doctor import (
    ExtensionDoctorIssue,
    ExtensionDoctorReport,
    ExtensionDoctorResult,
    doctor_extension,
    doctor_snapshot,
)
from .layout import (
    CONTRIBUTION_LAYOUTS,
    MANIFEST_FILENAMES,
    expected_layout_prefixes,
    is_package_manifest_path,
    iter_extension_manifest_paths,
    resolve_package_reference,
    validate_contribution_layout,
)
from .registry import (
    ExtensionContributionRecord,
    ExtensionLoadErrorRecord,
    ExtensionRecord,
    ExtensionRegistry,
    ExtensionRegistrySnapshot,
    extension_registry,
)

__all__ = [
    "ExtensionCompatibility",
    "ExtensionContributionRecord",
    "ExtensionContributionPaths",
    "ExtensionDoctorIssue",
    "ExtensionDoctorReport",
    "ExtensionDoctorResult",
    "ExtensionLoadErrorRecord",
    "ExtensionManifest",
    "ExtensionManifestError",
    "ExtensionPermissions",
    "ExtensionPublisher",
    "ExtensionRecord",
    "ExtensionRegistry",
    "ExtensionRegistrySnapshot",
    "CONTRIBUTION_LAYOUTS",
    "MANIFEST_FILENAMES",
    "doctor_extension",
    "doctor_snapshot",
    "extension_registry",
    "expected_layout_prefixes",
    "is_package_manifest_path",
    "iter_extension_manifest_paths",
    "load_extension_manifest",
    "parse_extension_manifest",
    "resolve_package_reference",
    "validate_contribution_layout",
]
