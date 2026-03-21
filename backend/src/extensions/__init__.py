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

__all__ = [
    "ExtensionCompatibility",
    "ExtensionContributionPaths",
    "ExtensionManifest",
    "ExtensionManifestError",
    "ExtensionPermissions",
    "ExtensionPublisher",
    "load_extension_manifest",
    "parse_extension_manifest",
]
