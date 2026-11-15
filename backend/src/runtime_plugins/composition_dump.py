"""Read-only, secret-free composition dump (ADR-036 §6, #1198).

The dump shows the composition as currently known: the declared bundles with
their runtime domains, each domain's owner row (owner, epoch, state, owner
digest), the reviewed Cordis provider digest and, for the live dump, the
plugin state the running child reports; which domains and trust services are
pinned; the validated named profile (ADR-036 §6, #1204) with the bundles it
selects; and the effective patch rows its layers set.

It is built only from an allow-list of non-secret fields: reason codes,
digests, epochs and fixed names. It never reads settings, environment values,
the vault or any credential. ``live_composition_dump`` serves
``/api/runtime/status`` and ``/api/runtime/composition/dump``;
``offline_composition_dump`` serves ``./manage.sh -e dev composition dump``,
which opens the workspace database read-only and never starts the child.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

from .profiles import BUNDLES, PATCH_ROWS, ComposedProfile, CompositionProfileInvalid, booted_profile, compose_profile, load_operator_patch
from .trust import PINNED_DOMAINS, TRUST_BUNDLE, TRUST_SERVICES, is_pinned_domain

DUMP_SCHEMA = "seraph-composition-dump.v1"
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ROW_FIELDS = ("runtime_domain", "owner_kind", "epoch", "composition_digest", "state")


def _code(value: Any) -> str | None:
    """A reason or state code, or ``None``; never free text."""
    if value is None:
        return None
    return value if type(value) is str and _CODE.fullmatch(value) else "redacted"


def _digest(value: Any) -> str | None:
    return value if type(value) is str and _SHA.fullmatch(value) else None


def _profile_payload(profile: ComposedProfile | CompositionProfileInvalid | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    if isinstance(profile, CompositionProfileInvalid):
        # Only a catalogued row id is echoed; free text from the operator's patch file never is.
        row_id = profile.row_id if profile.row_id in PATCH_ROWS else (None if profile.row_id is None else "redacted")
        return {"state": "invalid", "reason": _code(profile.reason), "row_id": row_id}
    return profile.payload()


def build_composition_dump(rows: dict[str, Any] | None, *, inventory_state: str, inventory_reason: str | None,
                           cordis: dict[str, Any], plugins: dict[str, dict[str, Any]] | None,
                           source: str,
                           profile: ComposedProfile | CompositionProfileInvalid | None = None) -> dict[str, Any]:
    """Assemble the dump from owner rows and Cordis facts; only allow-listed fields pass."""
    provider_digests = cordis.get("plugin_digests") or {}
    selected = set(profile.profile.bundles) if isinstance(profile, ComposedProfile) else None
    bundles = []
    for item in BUNDLES:
        bundle = item.name
        domains = []
        for domain in item.domains:
            row = (rows or {}).get(domain)
            plugin = None if plugins is None else plugins.get(domain)
            domains.append({
                "runtime_domain": domain,
                "pinned": is_pinned_domain(domain),
                "owner_kind": _code(getattr(row, "owner_kind", None)),
                "epoch": row.epoch if row is not None and type(row.epoch) is int else None,
                "state": _code(row.state) if row is not None else ("missing" if rows else None),
                "owner_digest": _digest(getattr(row, "composition_digest", None)),
                "provider_digest": _digest(provider_digests.get(domain)),
                "cordis_plugin": None if plugin is None else {
                    "state": _code(plugin.get("state")), "reason": _code(plugin.get("reason")),
                    "digest": _digest(plugin.get("digest"))},
            })
        status = "declared" if selected is None else ("selected" if bundle in selected else "not_selected")
        bundles.append({"bundle": bundle, "kind": item.kind, "pinned": bundle == TRUST_BUNDLE, "status": status,
                        "domains": domains})
    return {
        "schema": DUMP_SCHEMA,
        "source": source,
        "kernel": {"cordis_version": _code(cordis.get("cordis_version")),
                   "profile_id": _code(cordis.get("profile_id")),
                   "state": _code(cordis.get("state")), "reason": _code(cordis.get("reason")),
                   "package_digest": _digest(cordis.get("package_digest")),
                   "composition_digest": _digest(cordis.get("composition_digest"))},
        "inventory": {"state": inventory_state, "reason": _code(inventory_reason)},
        "bundles": bundles,
        "pinned_domains": sorted(PINNED_DOMAINS),
        "trust_services": [service.payload() for service in TRUST_SERVICES],
        # ADR-036 §6 (#1204): the validated profile and the rows its patch layers set.
        "profile": _profile_payload(profile),
        "effective_patch": profile.effective_patch if isinstance(profile, ComposedProfile) else [],
    }


def _checked_rows(raw_rows) -> tuple[dict[str, Any] | None, str, str | None]:
    from .ownership import DOMAINS, CompositionBindingError, _checked_row
    rows: dict[str, Any] = {}
    try:
        for row in raw_rows:
            if row.runtime_domain not in DOMAINS or row.runtime_domain in rows:
                raise CompositionBindingError("composition_inventory_invalid")
            _checked_row(row)
            rows[row.runtime_domain] = row
    except CompositionBindingError as exc:
        return None, "invalid", exc.reason_code
    return rows, ("ok" if rows else "unseeded"), None


def _cordis_facts(host) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    from .composition import CORDIS_VERSION
    snapshot = host.snapshot()
    reviewed = getattr(host, "reviewed", None)
    facts = {"cordis_version": CORDIS_VERSION, "profile_id": snapshot.get("profile_id"),
             "state": snapshot.get("state"), "reason": snapshot.get("reason"),
             "package_digest": snapshot.get("package_digest"),
             "composition_digest": snapshot.get("composition_digest"),
             "plugin_digests": dict(getattr(reviewed, "plugin_digests", None) or {})}
    plugins = {item.get("id"): item for item in snapshot.get("plugins") or () if isinstance(item, dict)}
    return facts, plugins


async def live_composition_dump(host) -> dict[str, Any]:
    """The booted tree: current owner rows and this process's Cordis host."""
    from sqlalchemy import select
    from src.db.engine import get_session
    from src.db.models import RuntimeCompositionState
    facts, plugins = _cordis_facts(host)
    try:
        async with get_session() as db:
            raw = list((await db.execute(select(RuntimeCompositionState)
                                         .execution_options(populate_existing=True))).scalars())
        rows, state, reason = _checked_rows(raw)
    except Exception:  # noqa: BLE001 - the dump reports the gap, never fails status
        rows, state, reason = None, "unavailable", "composition_inventory_unavailable"
    return build_composition_dump(rows, inventory_state=state, inventory_reason=reason, cordis=facts,
                                  plugins=plugins, source="live", profile=booted_profile())


def offline_profile(workspace: Path, *, requested: str | None,
                    deployment_environment: str) -> ComposedProfile | CompositionProfileInvalid:
    """The profile this workspace would boot with, validated exactly as boot validates it."""
    try:
        # Offline the dump never reads the no-login switch; production refuses it at startup.
        return compose_profile(requested, deployment_environment=deployment_environment, local_no_login=False,
                               operator_patch=load_operator_patch(workspace))
    except CompositionProfileInvalid as exc:
        return exc


def offline_composition_dump(workspace: Path, *, node_path: Path | None = None, profile: str | None = None,
                             deployment_environment: str = "development") -> dict[str, Any]:
    """The same dump from a stopped or running workspace, opened read-only; no child is started."""
    from .composition import CORDIS_VERSION, CompositionBlocked, reviewed_composition
    database = workspace / "seraph.db"
    rows: dict[str, Any] | None = None
    if not database.is_file():
        state, reason = "unavailable", "workspace_database_missing"
    else:
        try:
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
            try:
                raw = [SimpleNamespace(**dict(zip((*_ROW_FIELDS, "recovery_receipt_ref"), item)))
                       for item in connection.execute(
                           "SELECT runtime_domain, owner_kind, epoch, composition_digest, state, "
                           "recovery_receipt_ref FROM runtime_composition_states")]
            finally:
                connection.close()
            rows, state, reason = _checked_rows(raw)
        except sqlite3.Error:
            state, reason = "unavailable", "composition_inventory_unavailable"
    facts: dict[str, Any] = {"cordis_version": CORDIS_VERSION}
    try:
        reviewed = reviewed_composition(node_path=node_path)
        facts.update(profile_id=reviewed.profile["profile_id"], state="available", reason=None,
                     package_digest=reviewed.package_digest, composition_digest=reviewed.composition_digest,
                     plugin_digests=dict(reviewed.plugin_digests or {}))
    except CompositionBlocked as exc:
        facts.update(state="blocked", reason=exc.reason)
    return build_composition_dump(rows, inventory_state=state, inventory_reason=reason, cordis=facts,
                                  plugins=None, source="offline",
                                  profile=offline_profile(workspace, requested=profile,
                                                          deployment_environment=deployment_environment))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the read-only, secret-free composition dump.")
    parser.add_argument("command", choices=("dump",))
    parser.add_argument("--workspace", type=Path, required=True, help="workspace directory holding seraph.db")
    parser.add_argument("--node", type=Path, help="explicit reviewed Node executable for the package check")
    parser.add_argument("--profile", default="", help="COMPOSITION_PROFILE as the launcher reads it (empty derives it)")
    parser.add_argument("--deployment-environment", default="development",
                        help="DEPLOYMENT_ENVIRONMENT as the launcher reads it")
    args = parser.parse_args(argv)
    if not args.workspace.is_absolute() or (args.node is not None and not args.node.is_absolute()):
        parser.error("--workspace and --node must be absolute paths")
    dump = offline_composition_dump(args.workspace, node_path=args.node, profile=args.profile,
                                    deployment_environment=args.deployment_environment)
    print(json.dumps(dump, indent=2, sort_keys=True))
    return 0 if dump["inventory"]["state"] == "ok" and dump["profile"]["state"] == "valid" else 1


if __name__ == "__main__":
    sys.exit(main())
