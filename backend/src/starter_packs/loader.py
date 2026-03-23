"""Starter-pack loader for manifest-backed packs and legacy bundled defaults."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _record_starter_pack_error(
    errors: list[dict[str, str]] | None,
    *,
    path: str,
    message: str,
) -> None:
    logger.warning(message)
    if errors is not None:
        errors.append({"file_path": path, "message": message})


@dataclass
class StarterPack:
    name: str
    label: str
    description: str
    skills: list[str]
    workflows: list[str]
    install_items: list[str]
    sample_prompt: str = ""
    file_path: str = ""
    source: str = "legacy"
    extension_id: str | None = None


def _normalize_string_list(value: Any, *, field_name: str, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"Starter pack file {path} has invalid '{field_name}' field")
    return [item.strip() for item in value]


def parse_starter_pack_payload(
    payload: Any,
    *,
    path: str,
    errors: list[dict[str, str]] | None = None,
) -> StarterPack | None:
    if not isinstance(payload, dict):
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} root must be a mapping")
        return None
    name = payload.get("name")
    description = payload.get("description")
    if not isinstance(name, str) or not name.strip():
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} missing required 'name' field")
        return None
    if not isinstance(description, str) or not description.strip():
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} missing required 'description' field")
        return None
    label = payload.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} has invalid 'label' field")
        return None
    sample_prompt = payload.get("sample_prompt", "")
    if sample_prompt is not None and not isinstance(sample_prompt, str):
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} has invalid 'sample_prompt' field")
        return None
    try:
        skills = _normalize_string_list(payload.get("skills"), field_name="skills", path=path)
        workflows = _normalize_string_list(payload.get("workflows"), field_name="workflows", path=path)
        install_items = _normalize_string_list(payload.get("install_items"), field_name="install_items", path=path)
    except ValueError as exc:
        _record_starter_pack_error(errors, path=path, message=str(exc))
        return None
    return StarterPack(
        name=name.strip(),
        label=(label or name).strip(),
        description=description.strip(),
        skills=skills,
        workflows=workflows,
        install_items=install_items,
        sample_prompt="" if sample_prompt is None else sample_prompt.strip(),
        file_path=path,
    )


def _load_json(path: str, *, errors: list[dict[str, str]] | None = None) -> Any | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _record_starter_pack_error(errors, path=path, message=f"Failed to read starter pack file {path}: {exc}")
        return None


def scan_starter_pack_paths(pack_paths: list[str]) -> tuple[list[StarterPack], list[dict[str, str]]]:
    packs: list[StarterPack] = []
    errors: list[dict[str, str]] = []
    for path in sorted(pack_paths):
        payload = _load_json(path, errors=errors)
        if payload is None:
            continue
        pack = parse_starter_pack_payload(payload, path=path, errors=errors)
        if pack is not None:
            packs.append(pack)
    return packs, errors


def load_legacy_starter_packs(path: str) -> tuple[list[StarterPack], list[dict[str, str]]]:
    if not os.path.isfile(path):
        return [], []
    errors: list[dict[str, str]] = []
    payload = _load_json(path, errors=errors)
    if payload is None:
        return [], errors
    packs_payload = payload.get("packs") if isinstance(payload, dict) else None
    if not isinstance(packs_payload, list):
        _record_starter_pack_error(errors, path=path, message=f"Starter pack file {path} must define a 'packs' list")
        return [], errors
    packs: list[StarterPack] = []
    for index, item in enumerate(packs_payload):
        pack_path = f"{path}#packs[{index}]"
        pack = parse_starter_pack_payload(item, path=pack_path, errors=errors)
        if pack is not None:
            pack.file_path = path
            packs.append(pack)
    return packs, errors
