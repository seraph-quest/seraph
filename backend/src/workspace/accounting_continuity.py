"""Retain the latest inference ledger during managed root promotion."""

from pathlib import Path
import sqlite3
import json
import hashlib
from datetime import datetime, timezone
import os
import stat
import threading
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace

_PROGRAMME_SELECTION_SEAL = object()


def _programme_value_upper(value, *, budget=None):
    """Closed JSON width of an already paid value; no serialization/copy."""
    from collections.abc import Mapping
    from src.memory.header_bounds import HeaderBoundsError
    if budget is not None:
        # Numeric width work is itself a paid allocation. The original paid
        # <=1MiB body bounds lengths/counts (7 digits), JSON widths (8), and
        # bit_length*30103 (12); 64 covers these scalar/iterator locals.
        budget.debit(64, appearance=("programme-constructor", "width-scratch"))
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        # 30103/100000 > log10(2); unlike the SQL codec this admits big ints.
        return 1 + (value.bit_length() * 30103) // 100000 + (value < 0)
    if type(value) is float:
        return 64
    if type(value) is str:
        if budget is not None:
            budget.debit(4 * (4 + 4),
                appearance=("programme-constructor", "string-method-references"))
        size = len(value)
        if not size:
            return 2
        if value.isascii() and value.isprintable():
            return 2 + size + value.count('"') + value.count("\\")
        # No second encoding or code-point walk; includes lone surrogates.
        return 24 * size + 2
    if type(value) in (tuple, list):
        total = 2
        for item in value:
            if budget is not None:
                budget.debit(24, appearance=("programme-constructor", "width-add-scratch"))
            total += _programme_value_upper(item, budget=budget) + 1
        return total - 1 if value else total
    if isinstance(value, Mapping):
        total = 2
        for key in value:
            if budget is not None:
                budget.debit(32, appearance=("programme-constructor", "width-add-scratch"))
            if budget is not None:
                # Two receiver/method pairs16, three bools15 (including
                # exact str type), len/add14 and key/result references8.
                budget.debit(53, appearance=("programme-constructor", "key-width-scratch"))
            key_upper = (len(key) + 2 if type(key) is str and key.isascii()
                and key.isidentifier() else _programme_value_upper(key, budget=budget))
            total += key_upper + 1 + _programme_value_upper(value[key], budget=budget) + 1
        return total - 1 if value else total
    if type(value) is datetime:
        # Full datetime.isoformat with microseconds and an offset with seconds
        # and microseconds fits 42 ASCII characters, plus its JSON quotes.
        return 44
    from src.goals.contracts import (GoalProgramme, GoalProgrammeAuthorityBinding,
        GoalProgrammeBudget, GoalProgrammeNotifications)
    if type(value) in (GoalProgramme, GoalProgrammeAuthorityBinding,
            GoalProgrammeBudget, GoalProgrammeNotifications):
        fields = type(value).model_fields
        total = 2
        for name in fields:
            if budget is not None:
                budget.debit(36, appearance=("programme-constructor", "model-width-scratch"))
            total += len(name) + 3 + _programme_value_upper(getattr(value, name), budget=budget) + 1
        return total - 1
    raise HeaderBoundsError("programme_numeric_shape_unavailable")


def _programme_paid_map_upper(table, encoded, budget):
    """Map bound from the already-paid original raw encoder, without a walk."""
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS
    budget.debit(96, appearance=("programme-constructor", "paid-map-width-scratch"))
    descriptor = COMPOSITION_DESCRIPTORS[table]
    # Original outer = [tag, table, key, fields]; pair lists add two brackets
    # per field compared with a map. Subtract only the key's minimum width2.
    # A real's hex string may be narrower than its numeric map representation;
    # 64 per real preserves the original real scalar bound.
    return (len(encoded) - 30 - (len(table) + 2) - 5 - 2
        - 2 * len(descriptor.columns) + 64 * descriptor.kinds.count("real"))


def _programme_utf8_upper(value, *, budget=None):
    from src.memory.header_bounds import HeaderBoundsError
    if type(value) is not str:
        raise HeaderBoundsError("programme_numeric_shape_unavailable")
    if budget is not None:
        # Receiver/method8 + boolean5 + len7 + non-ASCII multiply8.
        budget.debit(28, appearance=("programme-constructor", "utf8-width-scratch"))
    return len(value) if value.isascii() else 4 * len(value)



def _programme_parse_upper(raw, *, budget):
    """Prepaid byte lexical expansion counter, never JSON validation."""
    budget.debit(2 + 4 * 5 + 20 + 14 + 5,
        appearance=("programme-constructor", "json-width-scratch"))
    encoded_upper = _programme_utf8_upper(raw, budget=budget)
    budget.debit(encoded_upper, appearance=("programme-constructor", "json-scan-input"))
    # Every yielded byte scalar is <=255 (at most three decimal digits),
    # prepaid even if this interpreter happens to cache small integers.
    budget.debit(3 * encoded_upper, appearance=("programme-constructor", "json-scan-byte-scalars"))
    encoded = raw.encode("utf-8", errors="surrogatepass")
    quoted = escaped = numeric = floating = False
    extra = 0
    for char in encoded:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if numeric and char not in b"0123456789.eE+-":
            if floating:
                budget.debit(20, appearance=("programme-constructor", "json-counter-update"))
                extra += 64
            numeric = floating = False
        # Python's original json.loads also accepts these nonfinite tokens.
        # Counting any outside-string N/I is conservative for malformed words;
        # only the original parser validates the token.
        if char in b"NI":
            budget.debit(20, appearance=("programme-constructor", "json-counter-update"))
            extra += 64
        if char == 34:
            quoted = True
        elif char in b"0123456789-" and not numeric:
            numeric = True
        elif numeric and char in b".eE":
            floating = True
    if numeric and floating:
        budget.debit(20, appearance=("programme-constructor", "json-counter-update"))
        extra += 64
    return _programme_value_upper(raw, budget=budget) + extra


def _programme_model_upper(value, model, *, budget=None):
    """Declared defaults/nested/date output before the original constructor."""
    from src.goals.contracts import (GoalProgramme, GoalProgrammeRequest,
        GoalProgrammeBudget, GoalProgrammeNotifications)
    from src.memory.header_bounds import HeaderBoundsError
    if model not in (GoalProgramme, GoalProgrammeRequest, GoalProgrammeBudget, GoalProgrammeNotifications):
        raise HeaderBoundsError("programme_numeric_shape_unavailable")
    if type(value) is not dict:
        return _programme_value_upper(value, budget=budget)
    total = 2
    for name in model.model_fields:
        if budget is not None:
            budget.debit(36, appearance=("programme-constructor", "model-width-scratch"))
        definition = model.model_fields[name]
        total += len(name) + 3
        if name in value:
            item = value[name]
        elif name == "notification_limits" and model is GoalProgrammeRequest:
            total += 2 + _programme_value_upper("per_day") + 1 + 1
            continue
        elif definition.is_required():
            # Invalid original input has no successful output; reserve its
            # actual input width and let the original model reject it.
            total += 4
            continue
        else:
            item = definition.default
        if name in {"expires_at", "confirmed_at"}:
            total += 44
        elif name == "budget":
            total += _programme_model_upper(item, GoalProgrammeBudget, budget=budget)
        elif name == "notification_limits":
            total += _programme_model_upper(item, GoalProgrammeNotifications, budget=budget)
        else:
            total += _programme_value_upper(item, budget=budget)
    return total + len(model.model_fields) - 1


from weakref import WeakKeyDictionary
_PROGRAMME_CONSTRUCTOR_PAYERS = WeakKeyDictionary()


class _ProgrammeConstructorPayer:
    """Private numeric lifetime; never a programme/read/effect permission."""
    def __init__(self, connection, certificate):
        import sys
        from src.memory.composition_headers import (_checked_current_snapshot_scope,
            _snapshot_current_task)
        from src.memory.header_bounds import HeaderBoundsError
        if sys._getframe(1).f_code is not _programme_constructor_payer.__code__:
            raise HeaderBoundsError("programme_constructor_owner_unavailable")
        self.connection, self.certificate = connection, certificate
        self.scope = _checked_current_snapshot_scope(certificate.budget)
        self.task, self.thread = _snapshot_current_task(), threading.get_ident()
        self.transaction, self.driver = certificate.transaction, certificate.driver
        self.widths = {}
        self.budget = certificate.budget
        _PROGRAMME_CONSTRUCTOR_PAYERS[self] = (certificate, self.scope, self.widths, self.budget)

    def _checked(self):
        from src.memory.composition_headers import (_checked_current_snapshot_scope,
            _snapshot_current_task, CompositionHeaderCertificate, _SEAL)
        from src.memory.header_bounds import HeaderBoundsError
        if type(self) is not _ProgrammeConstructorPayer:
            raise HeaderBoundsError("programme_constructor_owner_unavailable")
        issued = _PROGRAMME_CONSTRUCTOR_PAYERS.get(self)
        if type(issued) is not tuple:
            raise HeaderBoundsError("programme_constructor_owner_unavailable")
        cert = self.certificate
        if (issued[0] is not cert or issued[1] is not self.scope
                or issued[2] is not self.widths or issued[3] is not self.budget
                or cert.budget is not self.budget):
            raise HeaderBoundsError("programme_constructor_owner_changed")
        if (type(cert) is not CompositionHeaderCertificate or cert.seal is not _SEAL
                or cert.issued_id != id(cert) or cert.connection is not self.connection
                or cert.transaction is not self.transaction or cert.driver is not self.driver
                or _snapshot_current_task() is not self.task or threading.get_ident() != self.thread
                or _checked_current_snapshot_scope(cert.budget) is not self.scope
                or self.driver.total_changes != cert.changes):
            raise HeaderBoundsError("programme_constructor_owner_changed")
        if cert.raw_owner is not None:
            owner = cert.raw_owner
            if (type(owner) is not _RawRollbackConnection or owner._db is not self.connection
                    or not owner._live or owner._poisoned or owner._token is not self.transaction
                    or not owner._db.in_transaction):
                raise HeaderBoundsError("programme_constructor_owner_changed")
        elif (self.scope is None or self.scope.connection is not self.connection
                or self.connection.get_transaction() is not self.transaction
                or self.connection.connection.driver_connection is not self.driver
                or not self.driver.in_transaction):
            raise HeaderBoundsError("programme_constructor_owner_changed")
        return cert.budget

    def remember(self, value, upper):
        import sys
        from src.guardian.goal_programmes import _paid_programme_generation
        from src.memory.header_bounds import HeaderBoundsError
        if sys._getframe(1).f_code not in (
                _paid_programme_generation.__code__, _programme_matching_generation.__code__,
                _ProgrammeConstructorPayer.generations.__code__):
            raise HeaderBoundsError("programme_constructor_owner_unavailable")
        budget = self._checked()
        budget.debit(20 + 1 + 2 + 4 + 1 + 8 + 1,
            appearance=("programme-constructor", "retained-model-width"))
        self.widths[id(value)] = (value, upper)

    def generations(self, values):
        import sys
        from src.guardian.goal_programmes import _load
        from src.goals.contracts import GoalProgramme
        from src.memory.header_bounds import HeaderBoundsError
        if sys._getframe(1).f_code is not _load.__code__:
            raise HeaderBoundsError("programme_constructor_owner_unavailable")
        budget = self._checked()
        budget.debit(32, appearance=("programme-constructor", "generation-width-state"))
        upper = 2
        for item in values:
            budget.debit(24, appearance=("programme-constructor", "generation-width-add"))
            width = _programme_model_upper(item, GoalProgramme, budget=budget)
            self.remember(item, width)
            upper += width + 1
        if values:
            upper -= 1
        budget.debit(upper, appearance=("programme-constructor", "generation-list"))

    def binding(self, programme, capability):
        budget = self._checked()
        from src.goals.contracts import GoalProgramme
        from src.memory.header_bounds import HeaderBoundsError
        if type(programme) is not GoalProgramme or type(capability) is not str:
            raise HeaderBoundsError("programme_numeric_shape_unavailable")
        budget.debit(2 + 13 * (2 + 4 + 1 + 4) + 12,
            appearance=("programme-constructor", "binding-width-roster"))
        # Exact original from_programme's thirteen fields; field-name JSON
        # widths are fixed by Source, values come from the paid typed model.
        upper = 2
        for name, item in (("goal_id", programme.goal_id),
                ("goal_revision", programme.goal_revision), ("programme_id", programme.id),
                ("grant_revision", programme.grant_revision),
                ("owner_identity_id", programme.owner_identity_id),
                ("issuer_root_id", programme.issuer_root_id),
                ("issuer_principal_id", programme.issuer_principal_id),
                ("brief_digest", programme.brief_digest), ("capability_id", capability),
                ("route_epoch", programme.route_epoch), ("route_digest", programme.route_digest),
                ("expires_at", programme.expires_at),
                ("cost_ceiling_microusd", programme.budget.max_inference_microusd)):
            budget.debit(28, appearance=("programme-constructor", "binding-width-add-scratch"))
            upper += len(name) + 3 + _programme_value_upper(item, budget=budget) + 1
        upper -= 1
        budget.debit(upper, appearance=("programme-constructor", "binding-kwargs"))
        budget.debit(upper, appearance=("programme-constructor", "binding-model"))
        return upper

    def pay(self, stage, value, *, model=None):
        budget = self._checked()
        if stage == "matching-list":
            from src.memory.header_bounds import HeaderBoundsError
            budget.debit(32, appearance=("programme-constructor", "matching-width-scratch"))
            upper = 2
            for item in value:
                budget.debit(44, appearance=("programme-constructor", "matching-width-entry"))
                retained = self.widths.get(id(item))
                if retained is None or retained[0] is not item:
                    raise HeaderBoundsError("programme_constructor_model_width_unavailable")
                upper += retained[1] + 1
            if value:
                upper -= 1
        elif stage == "json":
            upper = _programme_parse_upper(value, budget=budget)
        elif stage == "envelope-keys":
            budget.debit(32, appearance=("programme-constructor", "envelope-width-scratch"))
            upper = 2
            if type(value) is dict:
                for key in value:
                    budget.debit(24, appearance=("programme-constructor", "envelope-width-add-scratch"))
                    upper += _programme_value_upper(key, budget=budget) + 1
                if value:
                    upper -= 1
        else:
            budget.debit(20, appearance=("programme-constructor", "retained-width-lookup"))
            retained = self.widths.get(id(value))
            if retained is not None and retained[0] is value:
                upper = retained[1]
            else:
                upper = _programme_value_upper(value, budget=budget) if model is None else _programme_model_upper(value, model, budget=budget)
        budget.debit(upper, appearance=("programme-constructor", stage))
        if model is not None:
            from src.goals.contracts import (GoalProgramme, GoalProgrammeRequest,
                GoalProgrammeBudget, GoalProgrammeNotifications)
            budget.debit(2 + 2 * (2 + 4 + 1 + 4) + 1,
                appearance=("programme-constructor", "nested-model-roster"))
            for name, nested in (("budget", GoalProgrammeBudget), ("notification_limits", GoalProgrammeNotifications)):
                if name in value:
                    budget.debit(_programme_model_upper(value[name], nested, budget=budget),
                        appearance=("programme-constructor", stage, name))
            if model is GoalProgrammeRequest and "notification_limits" not in value:
                budget.debit(13, appearance=("programme-constructor", stage, "notification-default-model"))
            if "public_brief" in value:
                if model is GoalProgrammeRequest:
                    budget.debit(_programme_value_upper(value["public_brief"], budget=budget),
                        appearance=("programme-constructor", stage, "brief-strip"))
                    budget.debit(7, appearance=("programme-constructor", stage, "brief-length"))
                budget.debit(_programme_utf8_upper(value["public_brief"], budget=budget),
                    appearance=("programme-constructor", stage, "brief-utf8"))
                if model is GoalProgramme:
                    budget.debit(4, appearance=("programme-constructor", stage, "brief-hash-owner"))
                    budget.debit(66, appearance=("programme-constructor", stage, "brief-digest"))
            for name in ("expires_at", "confirmed_at"):
                if name in value:
                    budget.debit(44, appearance=("programme-constructor", stage, name))
            if "expires_at" in value and "confirmed_at" in value:
                budget.debit(4 + 64, appearance=("programme-constructor", stage, "duration"))
        return upper


def _programme_constructor_payer(connection, certificate):
    import sys
    from src.memory.header_bounds import HeaderBoundsError
    if sys._getframe(1).f_code not in (
            _derive_programme_rows.__code__, _derive_programme_admission_rows.__code__):
        raise HeaderBoundsError("programme_constructor_owner_unavailable")
    # The enclosing original validator and paid row read already ran. No
    # second validation query is introduced by this capacity-only lifetime.
    # Seven fixed attribute names (quoted widths total 68), six opaque
    # references and a signed thread-id scalar; delimiters/colons included.
    certificate.budget.debit(2 + 68 + 7 + 6 + 6 * 4 + 20 + 16 + 34,
        appearance=("programme-constructor", "payer-storage"))
    payer = _ProgrammeConstructorPayer(connection, certificate)
    payer._checked()
    return payer


def _programme_matching_generation(value, payer):
    import sys
    from src.goals.contracts import GoalProgramme
    from src.memory.header_bounds import HeaderBoundsError
    if sys._getframe(1).f_code not in (
            _derive_programme_rows.__code__, _derive_programme_admission_rows.__code__):
        raise HeaderBoundsError("programme_constructor_owner_unavailable")
    upper = payer.pay("generation-model", value, model=GoalProgramme)
    programme = GoalProgramme.model_validate(value)
    payer.remember(programme, upper)
    return programme


@dataclass(frozen=True, eq=False)
class _ProgrammeSelection:
    owner: object
    common33: object
    goals: object
    issuers: object
    runs: object
    identity_ids: tuple
    issued_id: int = 0
    seal: object = field(default=_PROGRAMME_SELECTION_SEAL, repr=False)


def _derive_programme_rows(connection, common33, *, fail):
    from src.memory.composition_headers import _validate, snapshot_reads
    from src.workspace.accounting_witness import _programme_reference_row_on_connection
    from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE, discovery_authority
    from src.guardian.goal_programmes import _load
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    from src.workflows.job_runtime import _digest
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    _validate(connection, common33)
    def pay(table, key, *appearances):
        upper = common33.rows[(table, key)][1]
        for appearance in appearances:
            common33.budget.debit(upper, appearance=("programme-selector", appearance, table, key))
    goals, issuers, runs, identities = {}, {}, {}, set()
    def full(table, key):
        with snapshot_reads(common33):
            return _programme_reference_row_on_connection(connection, table, key)[1]
    for table, key in common33.rows:
        if table != "workflow_run_states":
            continue
        run = full(table, key)
        if run["job_kind"] != DISCOVERY_KIND or run["composition_binding_json"] is None:
            continue
        pay("workflow_run_states", key, "authority-json", "authority-model")
        authority = discovery_authority(run["declared_authority_json"])
        binding = authority.programme_binding
        pay("workflow_run_states", key, "composition-json", "composition-model", "composition-dependencies")
        composition = RuntimeCompositionBinding.from_json(run["composition_binding_json"])
        pay("workflow_run_states", key, "authority-digest-json", "authority-digest-canonical",
            "authority-digest-encoded", "input-digest-json", "input-digest-canonical", "input-digest-encoded")
        if (run["owner_kind"] != "service" or run["service_id"] != DISCOVERY_SERVICE
                or run["owner_principal_id"] != DISCOVERY_SERVICE
                or any(run[name] is not None for name in ("session_id", "conversation_id", "operator_session_id"))
                or run["run_identity"] != authority.original_job_id
                or run["goal_id"] != binding.goal_id or run["goal_revision"] != binding.goal_revision
                or composition.origin_method != "research.executeAccepted"
                or composition.native_branch != "public_research"
                or composition.host_package_digest is None
                or not {"seraph.goals.v1", "seraph.inference.v1"}.issubset(
                    item.runtime_domain for item in composition.dependency_vector)
                or run["authority_digest"] != _digest(json.loads(run["declared_authority_json"]))
                or run["input_digest"] != _digest(json.loads(run["arguments_json"]))):
            fail("programme_original_lineage_changed")
        with snapshot_reads(common33):
            _, goal, goal_bytes = _programme_reference_row_on_connection(connection, "goals", binding.goal_id)
        payer = _programme_constructor_payer(connection, common33)
        goal_upper = _programme_paid_map_upper("goals", goal_bytes, common33.budget)
        common33.budget.debit(goal_upper, appearance=("programme-constructor", "programme-namespace"))
        stored = _load(SimpleNamespace(**goal), _programme_payer=payer)
        payer.pay("matching-list", stored["generations"])
        matching = [_programme_matching_generation(value, payer) for value in stored["generations"]
            if value["id"] == binding.programme_id and value["grant_revision"] == binding.grant_revision]
        if matching:
            payer.binding(matching[0], authority.capability_id)
        if len(matching) != 1 or GoalProgrammeAuthorityBinding.from_programme(matching[0], authority.capability_id) != binding:
            fail("programme_original_generation_changed")
        issuer = full("operator_sessions", binding.issuer_root_id)
        if (issuer["operator_identity_id"] != binding.owner_identity_id
                or issuer["principal_id"] != binding.issuer_principal_id):
            fail("programme_original_issuer_changed")
        pay("goals", binding.goal_id, "goal-map-entry")
        pay("operator_sessions", binding.issuer_root_id, "issuer-map-entry", "identity-set-entry")
        pay("workflow_run_states", key, "run-map-entry")
        goals[binding.goal_id], issuers[binding.issuer_root_id] = goal, issuer
        runs[key] = run
        identities.add(binding.owner_identity_id)
    pending = list(goals)
    while pending:
        key = pending.pop()
        parent = goals[key]["parent_id"]
        if parent is not None and parent not in goals:
            pay("goals", parent, "ancestor-map-entry")
            goals[parent] = full("goals", parent)
            pending.append(parent)
    for key in goals:
        seen, current = set(), key
        while current is not None:
            if current in seen:
                fail("programme_goal_parent_cycle")
            seen.add(current)
            current = goals[current]["parent_id"]
    identity_upper = 2 + sum(6 * len(value.encode("utf-8")) + 3 for value in identities)
    for appearance in ("identity-order", "identity-return"):
        common33.budget.debit(identity_upper, appearance=("programme-selector", appearance))
    return goals, issuers, runs, tuple(sorted(identities))


def _derive_programme_admission_rows(connection, common33, *, goal_id, programme_id,
                                    grant_revision, fail):
    """Canonical first-admission capacity facts; no Identity body or grant."""
    from src.memory.composition_headers import _validate, snapshot_reads
    from src.workspace.accounting_witness import _programme_reference_row_on_connection
    from src.guardian.goal_programmes import _load
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    from src.work_board.research_parent import DISCOVERY_CAPABILITY
    _validate(connection, common33)
    if (any(type(value) is not str or not 0 < len(value.encode("utf-8")) <= 512
            for value in (goal_id, programme_id))
            or type(grant_revision) is not int or grant_revision < 1):
        fail("programme_original_generation_changed")
    with snapshot_reads(common33):
        _, goal, goal_bytes = _programme_reference_row_on_connection(connection, "goals", goal_id)
    payer = _programme_constructor_payer(connection, common33)
    goal_upper = _programme_paid_map_upper("goals", goal_bytes, common33.budget)
    common33.budget.debit(goal_upper, appearance=("programme-constructor", "programme-namespace"))
    stored = _load(SimpleNamespace(**goal), _programme_payer=payer)
    payer.pay("matching-list", stored["generations"])
    matching = [_programme_matching_generation(value, payer) for value in stored["generations"] if value["id"] == programme_id]
    if len(matching) != 1:
        fail("programme_original_generation_changed")
    programme = matching[0]
    if (programme.grant_revision != grant_revision
            or programme.goal_id != goal_id or programme.goal_revision != goal["revision"]
            or DISCOVERY_CAPABILITY not in programme.capability_ids):
        fail("programme_original_generation_changed")
    payer.binding(programme, DISCOVERY_CAPABILITY)
    binding = GoalProgrammeAuthorityBinding.from_programme(programme, DISCOVERY_CAPABILITY)
    with snapshot_reads(common33):
        _, issuer, issuer_bytes = _programme_reference_row_on_connection(connection, "operator_sessions", binding.issuer_root_id)
    if (issuer["operator_identity_id"] != binding.owner_identity_id
            or issuer["principal_id"] != binding.issuer_principal_id):
        fail("programme_original_issuer_changed")
    # Pay canonical retained facts and typed return appearances in the same frame
    # before issuing/storing the private selection or delivering its outputs.
    # Both typed model returns and their returned maps are separate from the
    # canonical raw tuple delivery, and paid before constructing those copies.
    payer.pay("programme-output-model", programme)
    payer.pay("binding-output-model", binding)
    common33.budget.debit(96, appearance=("programme-constructor", "return-width-scratch"))
    common33.budget.debit(len(goal_bytes) + len(issuer_bytes)
        + goal_upper
        + _programme_paid_map_upper("operator_sessions", issuer_bytes, common33.budget)
        + _programme_value_upper(goal_id, budget=common33.budget)
        + _programme_value_upper(binding.issuer_root_id, budget=common33.budget) + 19,
        appearance=("programme-admission-output", goal_id, programme_id, grant_revision))
    return programme, binding, {goal_id: MappingProxyType(goal)}, {
        binding.issuer_root_id: MappingProxyType(issuer)}


class _RawRollbackConnection:
    """Private original rollback handle, not a plugin SQL or authority API.

    Path/inode observations check cooperative owner consistency; they do not
    prove which inode SQLite's VFS opened or isolate a hostile same-UID actor.
    This bounded first seam admits read certification only. Original retention
    writes/publication still require the complete programme closure protocol.
    """
    def __init__(self, path, budget, *, readonly):
        from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
        from src.workspace.production import _assert_no_symlink_components
        if type(budget) is not HeaderReadBudget:
            raise HeaderBoundsError("header_request_bound")
        self._path = Path(path).absolute()
        _assert_no_symlink_components(self._path, label="rollback database")
        self._path = self._path.resolve(strict=True)
        observed = self._path.stat()
        if not stat.S_ISREG(observed.st_mode):
            raise HeaderBoundsError("rollback_database_unavailable")
        self._observed_inode = (observed.st_dev, observed.st_ino)
        self._budget = budget
        self._namespace = object()  # Exact handle namespace, never a VFS claim.
        self._thread = threading.get_ident()
        self._pid = os.getpid()
        self._live = True
        self._poisoned = False
        self._token = None
        self._expected = None
        self._trace_count = 0
        self._transition_trace = 0
        self._probe_denials = 0
        self._selection = None
        self._pair = None
        self._db = sqlite3.connect(self._path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True, isolation_level=None, cached_statements=0)
        self._db.row_factory = sqlite3.Row
        self._db.set_authorizer(self._authorize)
        self._db.set_trace_callback(self._trace)
        try:
            self._validate_handle()
        except BaseException:
            self.close()
            raise

    def _fail(self, code):
        from src.memory.header_bounds import HeaderBoundsError
        self._poisoned = True
        self._token = None
        raise HeaderBoundsError(code)

    def _authorize(self, action, first, second, database, trigger):
        if action == sqlite3.SQLITE_FUNCTION and second == "hex":
            self._probe_denials += 1
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_SAVEPOINT):
            self._poisoned = True
            self._token = None
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION:
            expected = self._expected.split()[0] if self._expected else None
            if first != expected:
                self._poisoned = True
                self._token = None
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if (first not in {
                "database_list", "schema_version", "encoding", "table_xinfo",
                "table_list", "index_list", "index_xinfo", "table_info", "index_info", "foreign_key_list"}
                or (second is not None and first not in {
                    "table_xinfo", "table_list", "index_list", "index_xinfo", "table_info", "index_info", "foreign_key_list"})):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and second in {
                "typeof", "octet_length", "sqlite_version", "total_changes"}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and second in {"count", "sum", "coalesce"}:
            from src.memory.composition_headers import _raw_metadata_census_function_allowed
            if _raw_metadata_census_function_allowed(self, second):
                return sqlite3.SQLITE_OK
        # Closed read-only policy: temp/virtual schema DDL and every other
        # unneeded action deny too, rather than relying on a mutation blacklist.
        return sqlite3.SQLITE_DENY

    def _trace(self, statement):
        self._trace_count += 1
        command = statement.strip().upper()
        if command.split(" ", 1)[0] in ("BEGIN", "COMMIT", "ROLLBACK", "END", "SAVEPOINT", "RELEASE"):
            if command != self._expected:
                self._poisoned = True
                self._token = None
            else:
                self._transition_trace += 1

    def _validate_handle(self):
        if (not self._live or self._poisoned or self._thread != threading.get_ident()
                or self._pid != os.getpid() or type(self._db) is not sqlite3.Connection
                or self._db.isolation_level is not None):
            self._fail("rollback_raw_owner_unavailable")
        # Harmless denied function probes detect replaced authorizers without
        # executing an unauthorized transaction. Statement caching is disabled.
        denied = self._probe_denials
        try:
            self._db.execute("SELECT hex(NULL)").fetchall()
        except sqlite3.DatabaseError:
            pass
        if self._probe_denials != denied + 1:
            self._fail("rollback_authorizer_unavailable")
        traced = self._trace_count
        self._db.execute("SELECT 1").fetchone()
        if self._trace_count != traced + 1:
            self._fail("rollback_trace_unavailable")
        from src.workspace.production import _assert_no_symlink_components
        try:
            _assert_no_symlink_components(self._path, label="rollback database")
            observed = self._path.stat()
        except (OSError, ValueError):
            self._fail("rollback_database_path_changed")
        if (not stat.S_ISREG(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != self._observed_inode):
            self._fail("rollback_database_path_changed")
        databases = self._db.execute("PRAGMA database_list").fetchall()
        main = [row for row in databases if row[1] == "main"]
        if (len(main) != 1 or Path(main[0][2]) != self._path
                or any(row[1] not in ("main", "temp") for row in databases)):
            self._fail("rollback_database_binding_changed")
        if bool(self._token) != self._db.in_transaction:
            self._fail("rollback_transaction_state_changed")

    def _validate_budget(self, budget):
        if budget is not self._budget:
            self._fail("rollback_budget_changed")

    def _state(self, connection):
        if connection is not self._db:
            self._fail("rollback_connection_changed")
        self._validate_handle()
        if self._token is None:
            from src.memory.header_bounds import HeaderBoundsError
            raise HeaderBoundsError("header_transaction_required")
        return self._token, self._db, self._db.total_changes

    def _transition(self, statement):
        self._validate_handle()
        opening = statement in ("BEGIN", "BEGIN IMMEDIATE")
        if opening == self._db.in_transaction:
            self._fail("rollback_transaction_transition_invalid")
        self._expected = statement
        traced = self._transition_trace
        self._token = None
        try:
            self._db.execute(statement)
            if (self._poisoned or self._transition_trace != traced + 1
                    or self._db.in_transaction != opening):
                self._fail("rollback_transaction_receipt_unavailable")
            self._token = object() if opening else None
        except BaseException:
            self._poisoned = True
            self._token = None
            raise
        finally:
            self._expected = None

    def begin(self, *, immediate=False):
        self._transition("BEGIN IMMEDIATE" if immediate else "BEGIN")

    def rollback(self):
        self._transition("ROLLBACK")

    def commit(self):
        self._transition("COMMIT")

    def certify(self):
        from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
        return _certify_current_memory_snapshot_on_connection(self._db, self._budget, raw_owner=self)

    def _validate_programme_selection(self, selection):
        from src.memory.composition_headers import _validate
        if (type(selection) is not _ProgrammeSelection or selection.seal is not _PROGRAMME_SELECTION_SEAL
                or selection.issued_id != id(selection) or selection.owner._selection is not selection
                or self._pair is None or selection.owner._pair is not self._pair
                or selection.owner is not self._pair.source):
            self._fail("programme_original_selection_unavailable")
        _validate(selection.common33.connection, selection.common33)
        self._validate_budget(selection.common33.budget)

    def _select_programmes(self, common33):
        """Original owner derives finite lineage after actual common33 bodies."""
        from src.memory.composition_headers import _validate
        _validate(self._db, common33)
        if common33.raw_owner is not self or self._pair is None or self is not self._pair.source:
            self._fail("programme_original_selection_unavailable")
        goals, issuers, runs, identities = _derive_programme_rows(self._db, common33, fail=self._fail)
        selection = _ProgrammeSelection(self, common33, MappingProxyType(goals), MappingProxyType(issuers),
            MappingProxyType(runs), tuple(sorted(identities)))
        object.__setattr__(selection, "issued_id", id(selection))
        self._selection = selection
        return selection

    def close(self):
        if self._live:
            self._live = False
            self._token = None
            self._db.close()


class _RawRollbackPair:
    """Exactly two retained handles and one unchanged numeric frame."""
    def __init__(self, source_path, destination_path, budget):
        self.source = _RawRollbackConnection(source_path, budget, readonly=True)
        try:
            self.destination = _RawRollbackConnection(destination_path, budget, readonly=False)
            if self.source._observed_inode == self.destination._observed_inode:
                self.source._fail("rollback_database_alias")
            self.source._pair = self.destination._pair = self
            self._preflight = None
            self._writer = None
            self._copies_reserved = False
        except BaseException:
            if hasattr(self, "destination"):
                self.destination.close()
            self.source.close()
            raise

    def close(self):
        self.source.close()
        self.destination.close()

    def _compare_programmes(self, source_common, destination_common, selection):
        from src.memory.composition_headers import (snapshot_reads,
            preflight_programme_identity_component, read_programme_identity)
        from src.workspace.accounting_witness import _programme_reference_row_on_connection
        self.destination._validate_programme_selection(selection)
        from src.work_board.research_parent import DISCOVERY_KIND
        for table, key in destination_common.rows:
            if table != "workflow_run_states":
                continue
            with snapshot_reads(destination_common):
                row = _programme_reference_row_on_connection(self.destination._db, table, key)[1]
            if row["job_kind"] == DISCOVERY_KIND and row["composition_binding_json"] is not None:
                source_run = selection.runs.get(key)
                if source_run is None or any(row[name] != source_run[name] for name in _JOB_BINDING_FIELDS):
                    self.destination._fail("programme_destination_lineage_conflict")
        for key, source_row in selection.goals.items():
            if ("goals", key) in destination_common.rows:
                with snapshot_reads(destination_common):
                    destination_row = _programme_reference_row_on_connection(self.destination._db, "goals", key)[1]
                if tuple(destination_row.values()) != tuple(source_row.values()):
                    self.destination._fail("programme_goal_raw_conflict")
        source_identity = destination_identity = None
        source_rows, destination_rows = {}, {}
        if selection.identity_ids:
            source_identity = preflight_programme_identity_component(source_common, original_selection=selection)
            destination_identity = preflight_programme_identity_component(destination_common, original_selection=selection)
            for identity in selection.identity_ids:
                source_row = read_programme_identity(source_identity, identity)
                destination_row = read_programme_identity(destination_identity, identity)
                if destination_row is not None and source_row != destination_row:
                    self.destination._fail("programme_identity_raw_conflict")
                source_rows[identity], destination_rows[identity] = source_row, destination_row
        return source_identity, destination_identity, MappingProxyType(source_rows), MappingProxyType(destination_rows)

    def preflight_programme_conflicts(self):
        self.source.begin()
        self.destination.begin()
        source, destination = self.source.certify(), self.destination.certify()
        selection = self.source._select_programmes(source)
        compared = self._compare_programmes(source, destination, selection)
        self._preflight = (source, destination, selection, compared)
        return self._preflight

    def begin_current_writer(self):
        from src.memory.composition_headers import _validate
        if self._preflight is None:
            self.destination._fail("programme_preflight_required")
        source, _destination, old_selection, _compared = self._preflight
        _validate(self.source._db, source)
        self.destination.rollback()
        self.destination.begin(immediate=True)
        destination = self.destination.certify()
        # Rederive the original source proof under its retained actual snapshot;
        # never promote a copied source selection to current writer permission.
        selection = self.source._select_programmes(source)
        if (dict(selection.goals) != dict(old_selection.goals) or dict(selection.issuers) != dict(old_selection.issuers)
                or dict(selection.runs) != dict(old_selection.runs) or selection.identity_ids != old_selection.identity_ids):
            self.destination._fail("programme_original_selection_changed")
        compared = self._compare_programmes(source, destination, selection)
        self._writer = (source, destination, selection, compared)
        return self._writer

    def reserve_programme_copies(self):
        """Reserve exact absent raw Goal/Identity destinations before effects."""
        from src.memory.header_bounds import GOAL
        from src.memory.composition_headers import _validate
        if self._writer is None or self._copies_reserved:
            self.destination._fail("programme_copy_reservation_unavailable")
        source, destination, selection, compared = self._writer
        _validate(self.source._db, source)
        _validate(self.destination._db, destination)
        source_identity, _destination_identity, _source_rows, destination_rows = compared
        budget = self.destination._budget
        for key in selection.goals:
            if ("goals", key) not in destination.rows:
                budget.reserve_future_row(GOAL, key, source.rows[("goals", key)][1],
                    database_identity=self.destination._namespace)
        for identity, row in destination_rows.items():
            if row is None:
                budget._reserve_programme_identity(self.destination, identity,
                    source_identity.rows[("operator_identities", identity)][1])
        self._copies_reserved = True


# The same immutable invocation/authority binding checked by durable admission,
# plus the legacy row/root/conversation identity. Mutable execution receipts
# come from the verified canonical source, never a stale target generation.
_JOB_BINDING_FIELDS = (
    "id", "run_identity", "root_run_identity", "parent_run_identity", "workflow_name", "tool_name",
    "session_id", "conversation_id", "operator_session_id", "branch_kind", "branch_depth",
    "run_fingerprint", "arguments_json", "record_schema_version", "parent_job_id", "parent_fencing_token",
    "job_kind", "owner_kind", "owner_principal_id", "service_id", "goal_id", "goal_revision",
    "plan_revision", "candidate_id", "source_task_id", "composition_binding_json", "capability_version", "input_digest", "authority_digest",
    "budget_digest", "idempotency_scope", "idempotency_key", "idempotency_binding", "priority",
    "dependencies_json", "resource_claims_json", "declared_authority_json", "deadline_at", "max_attempts",
)
_JOB_RECONCILIATION_KEY = "inference_accounting_restore_reconciliation"


def _retained_job_values(source, target, account):
    from src.workspace.production import ProductionWorkspaceReconciliationError
    values = dict(source)
    prior = dict(target) if target is not None else None
    if any(field not in values for field in _JOB_BINDING_FIELDS):
        raise ProductionWorkspaceReconciliationError("accounting canonical job binding schema unavailable")
    if prior is not None and any(prior.get(field) != values[field] for field in _JOB_BINDING_FIELDS):
        raise ProductionWorkspaceReconciliationError("accounting canonical job binding mismatch")
    for row in (values, prior):
        if row is not None and any(type(row.get(field)) is not int or row[field] < 0 for field in ("revision", "fencing_token")):
            raise ProductionWorkspaceReconciliationError("accounting canonical job fence invalid")
    try:
        metadata = json.loads(values["metadata_json"]) if values["metadata_json"] is not None else {}
        prior_metadata = json.loads(prior["metadata_json"]) if prior is not None and prior["metadata_json"] is not None else {}
    except (TypeError, ValueError) as exc:
        raise ProductionWorkspaceReconciliationError("accounting canonical job metadata unavailable") from exc
    if not isinstance(metadata, dict) or not isinstance(prior_metadata, dict):
        raise ProductionWorkspaceReconciliationError("accounting canonical job metadata unavailable")
    source_digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    binding = {"deployment_id": account["deployment_id"], "accounting_revision": account["revision"],
        "source_job_digest": source_digest}
    values.update(status="blocked", failure_reason="workspace_restore_requires_reconciliation",
        lease_owner=None, lease_expires_at=None)
    marker = prior_metadata.get(_JOB_RECONCILIATION_KEY)
    if prior is not None and isinstance(marker, dict):
        expected_marker = {**binding, "revision": prior["revision"], "fencing_token": prior["fencing_token"]}
        expected_metadata = {**metadata, _JOB_RECONCILIATION_KEY: expected_marker}
        mutable = {"revision", "fencing_token", "metadata_json", "updated_at"}
        if (marker == expected_marker and prior_metadata == expected_metadata
            and all(prior.get(field) == value for field, value in values.items() if field not in mutable)
            and prior["revision"] > source["revision"] and prior["fencing_token"] > source["fencing_token"]):
            # Exact retry after retention but before descriptor promotion.
            # The previous fresh fence remains authoritative; no churn.
            return prior
    values["revision"] = max(values["revision"], prior["revision"] if prior else -1) + 1
    values["fencing_token"] = max(values["fencing_token"], prior["fencing_token"] if prior else -1) + 1
    values["metadata_json"] = json.dumps({**metadata, _JOB_RECONCILIATION_KEY: {**binding,
        "revision": values["revision"], "fencing_token": values["fencing_token"]}}, sort_keys=True, separators=(",", ":"))
    values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return values


def transition_programme_envelope(*, workspace, budget, _recover_pending=False):
    """Explicit stopped original owner transition; zero real programme seeds."""
    from src.workspace import canonical_workspace_registry
    from src.workspace.lifecycle import _require_production_fence
    from src.memory.header_bounds import HeaderReadBudget
    from src.memory.composition_headers import snapshot_reads, charge_table
    from src.workspace.production import (ProductionWorkspace, ProductionWorkspaceReconciliationError,
        read_lifecycle_receipt, read_accounting_checkpoint, _write_private_checkpoint,
        _write_lifecycle_receipt_locked, MAX_ACCOUNTING_CHECKPOINT_BYTES, MAX_LIFECYCLE_RECEIPT_BYTES)
    from src.workspace.accounting_witness import (maintenance_accounting_lock, composition_closure,
        native_composition_files, _programme_envelope, _programme_transition,
        _programme_json, _programme_reference_row_on_connection, ledger_record, ledger_digest, witness)
    from src.work_board.research_parent import DISCOVERY_KIND
    if type(workspace) is not ProductionWorkspace or type(budget) is not HeaderReadBudget:
        raise ProductionWorkspaceReconciliationError("programme_original_owner_unavailable")
    registry = canonical_workspace_registry(workspace.host_root)
    _require_production_fence(registry)
    with maintenance_accounting_lock(workspace, header_budget=budget):
        receipt = read_lifecycle_receipt(workspace, header_budget=budget)
        checkpoint = read_accounting_checkpoint(workspace, header_budget=budget)
        previous = receipt.get("runtime_composition") if receipt else None
        if type(previous) is not dict or previous.get("schema_version") != 1:
            raise ProductionWorkspaceReconciliationError("composition_explicit_transition_requires_v1")
        owner = _RawRollbackConnection(workspace.host_root / registry.config.database_path, budget, readonly=False)
        try:
            owner.begin(immediate=True)
            certificate = owner.certify()
            with snapshot_reads(certificate):
                for table, key in certificate.rows:
                    if table != "workflow_run_states":
                        continue
                    row = _programme_reference_row_on_connection(owner._db, table, key)[1]
                    if row["job_kind"] == DISCOVERY_KIND and row["composition_binding_json"] is not None:
                        raise ProductionWorkspaceReconciliationError("composition_transition_requires_empty_programmes")
                    if row["status"] == "running" or row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                        raise ProductionWorkspaceReconciliationError("composition_transition_requires_stopped_owner")
                native, _members = composition_closure(owner._db,
                    verify_files=lambda table, row: native_composition_files(table, row,
                        root=workspace.host_root, header_budget=budget))
                if native != previous or any(row["state"] == "draining" for row in native["inventory"]):
                    raise ProductionWorkspaceReconciliationError("composition_transition_source_changed")
                charge_table(owner._db, "inference_accounting_owners")
                accounts = list(owner._db.execute("SELECT * FROM inference_accounting_owners"))
                charge_table(owner._db, "inference_cost_reservations")
                rows = [ledger_record(row) for row in owner._db.execute("SELECT * FROM inference_cost_reservations")]
            if len(accounts) != 1 or checkpoint is None:
                raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
            account = ledger_record(accounts[0])
            actual_witness = witness(account)
            if (ledger_digest(account, rows) != account["ledger_digest"]
                    or actual_witness != receipt.get("inference_accounting")
                    or actual_witness != checkpoint.get("witness")
                    or checkpoint.get("secret_values_included") is not False
                    or (not _recover_pending and checkpoint.get("schema_version") == 2
                        and checkpoint.get("composition_target") != previous)):
                raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
            target = _programme_envelope(native, None, None)
            transition, reference = _programme_transition(previous, target)
            target["transition_ref"] = reference
            if _recover_pending and (checkpoint.get("schema_version") != 2
                    or checkpoint.get("composition_base") != previous
                    or checkpoint.get("composition_target") != target
                    or checkpoint.get("composition_programme_transition") != transition
                    or checkpoint.get("composition_delta") != []):
                raise ProductionWorkspaceReconciliationError("composition_transition_recovery_binding_changed")
            projected = {**checkpoint, "schema_version": 2, "composition_base": previous,
                "composition_target": target, "composition_delta": [],
                "composition_programme_transition": transition, "secret_values_included": False}
            final_receipt = {**receipt, "runtime_composition": target,
                "composition_programme_transition": transition, "secret_values_included": False}
            checkpoint_size, receipt_size = len(_programme_json(projected)), len(_programme_json(final_receipt))
            if checkpoint_size > MAX_ACCOUNTING_CHECKPOINT_BYTES or receipt_size > MAX_LIFECYCLE_RECEIPT_BYTES:
                raise ProductionWorkspaceReconciliationError("composition_publication_size_exceeded")
            budget.enroll({(owner._namespace, "prospective-file", "accounting-checkpoint.json"),
                (owner._namespace, "prospective-file", "receipt.json")})
            budget.debit(checkpoint_size, appearance=("programme-transition-checkpoint",))
            budget.debit(receipt_size, appearance=("programme-transition-receipt",))
            # All source/body/output bounds precede the first durable effect.
            # Checkpoint first makes interruption an explicit pending gap.
            if not _recover_pending:
                _write_private_checkpoint(workspace.lifecycle_directory / "accounting-checkpoint.json", projected)
            _write_lifecycle_receipt_locked(workspace, final_receipt,
                _programme_transition=transition, _prior_receipt=receipt)
            owner.rollback()
            return {"status": "transitioned", "runtime_composition": target,
                "composition_programme_transition": transition, "no_learning": True,
                "secret_values_included": False}
        finally:
            owner.close()


def reconcile_programme_envelope_transition(*, workspace, budget):
    """Explicit stopped recovery of only the exact interrupted empty upgrade."""
    return transition_programme_envelope(workspace=workspace, budget=budget, _recover_pending=True)


def verify_accounting_generation(database, expected):
    from src.workspace.accounting_witness import ledger_record, ledger_digest, witness
    from src.workspace.production import ProductionWorkspaceReconciliationError
    if not database.is_file() or database.is_symlink():
        raise ProductionWorkspaceReconciliationError("accounting continuity database unavailable")
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        owners = db.execute("SELECT * FROM inference_accounting_owners").fetchall()
        if len(owners) != 1:
            raise ProductionWorkspaceReconciliationError("accounting continuity owner unavailable")
        account = ledger_record(owners[0])
        rows = [ledger_record(row) for row in db.execute("SELECT * FROM inference_cost_reservations")]
        if witness(account) != expected or ledger_digest(account, rows) != account["ledger_digest"]:
            raise ProductionWorkspaceReconciliationError("accounting continuity latest ledger unavailable")
        return account, rows


def acknowledge_accounting_period(*, root, period, expected_revision, actor):
    from src.workspace.accounting_witness import maintenance_accounting_lock, utc_period, period_state, ledger_digest, witness
    from src.workspace.production import ProductionWorkspaceReconciliationError, read_lifecycle_receipt, write_lifecycle_receipt, write_accounting_checkpoint
    if period != utc_period() or type(expected_revision) is not int:
        raise ProductionWorkspaceReconciliationError("accounting period review binding invalid")
    with maintenance_accounting_lock(root) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        account, rows = verify_accounting_generation(root / "seraph.db", receipt.get("inference_accounting"))
        if account["revision"] != expected_revision:
            raise ProductionWorkspaceReconciliationError("accounting period revision changed")
        state = period_state(account, rows, period)
        history = json.loads(account["settings_history_json"])
        history.append({"kind": "period_observed", "period_id": max(period, state["period_high_water"]), "recorded_at": datetime.now(timezone.utc).isoformat()})
        history.append({"kind": "period_review", "period_id": period, "accounting_revision": expected_revision,
            "actor": actor, "recorded_at": datetime.now(timezone.utc).isoformat(), "memory_status": "no_learning"})
        base = witness(account)
        account["settings_history_json"] = json.dumps(history, sort_keys=True, separators=(",", ":"))
        account["revision"] += 1
        account["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        account["ledger_digest"] = ledger_digest(account, rows)
        with sqlite3.connect(root / "seraph.db") as db:
            db.execute("BEGIN IMMEDIATE")
            names = ",".join('"'+name+'"=?' for name in account)
            update = db.execute(f'UPDATE inference_accounting_owners SET {names} WHERE deployment_id=? AND revision=?',
                (*account.values(), account["deployment_id"], expected_revision))
            if update.rowcount != 1:
                raise ProductionWorkspaceReconciliationError("accounting period revision changed")
            write_accounting_checkpoint(workspace, {"schema_version": 1, "base": base, "account": account,
                "operations": [], "witness": witness(account), "secret_values_included": False})
            receipt["inference_accounting"] = witness(account)
            write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
        return {"status": "acknowledged", "period_id": period, "revision": account["revision"],
            "period_high_water": max(period, state["period_high_water"]), "job_authority_changed": False, "memory_status": "no_learning"}


def rebind_accounting_root(*, active, target):
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    from src.workspace.production import read_lifecycle_receipt, write_lifecycle_receipt, ProductionWorkspaceReconciliationError
    from src.workspace.accounting_witness import maintenance_accounting_lock, revoke_restored_policy, _write_configuration_file
    _require_production_fence(canonical_workspace_registry(active.host_root))
    _require_production_fence(canonical_workspace_registry(target.host_root))
    receipt = read_lifecycle_receipt(active) or {}
    binding = receipt.get("deployment_binding", {})
    if binding.get("root_path_digest") == target.identity_digest:
        verify_accounting_generation(target.host_root / "seraph.db", receipt.get("inference_accounting"))
        return {"status": "already_rebound", "deployment_id": receipt["inference_accounting"]["deployment_id"], "job_authority_changed": False}
    if binding.get("root_path_digest") != active.identity_digest or active.lifecycle_directory != target.lifecycle_directory:
        raise ProductionWorkspaceReconciliationError("accounting rebind source descriptor unavailable")
    retained = retain_inference_accounting(active=active.host_root, target=target.host_root, database_path="seraph.db")
    if retained["status"] != "retained_latest":
        raise ProductionWorkspaceReconciliationError("accounting rebind deployment ledger unavailable")
    target_config = target.host_root / "model-fabric-settings.json"
    if not target_config.exists():
        source = active.host_root / "model-fabric-settings.json"
        if source.is_file() and not source.is_symlink():
            _write_configuration_file(target_config, json.loads(source.read_text()))
    revoke_restored_policy(active=active, target=target.host_root)
    with maintenance_accounting_lock(active) as workspace:
        receipt = read_lifecycle_receipt(workspace)
        if receipt.get("runtime_composition") is not None:
            receipt["runtime_composition"] = verify_promoted_composition(target)
        receipt["deployment_binding"] = {"revision": receipt["deployment_binding"]["revision"] + 1,
            "root_path_digest": target.identity_digest, "host_bind_identity": target.bind_identity_digest}
        write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
    return {"status": "rebound", "deployment_id": receipt["inference_accounting"]["deployment_id"],
        "liabilities_retained": retained["operations"], "job_authority_changed": False, "memory_status": "no_learning"}


def verify_promoted_composition(workspace):
    from src.workspace.accounting_witness import composition_closure, native_composition_files
    from src.workspace.production import read_accounting_checkpoint, ProductionWorkspaceReconciliationError
    checkpoint = read_accounting_checkpoint(workspace)
    if checkpoint is None or checkpoint.get("schema_version") != 2:
        raise ProductionWorkspaceReconciliationError("composition_promotion_checkpoint_unavailable")
    with sqlite3.connect(workspace.host_root / "seraph.db") as connection:
        actual, _ = composition_closure(connection,
            verify_files=lambda table, row: native_composition_files(table, row, root=workspace.host_root))
    if actual is None or actual != checkpoint.get("composition_target"):
        raise ProductionWorkspaceReconciliationError("composition_promotion_canonical_generation_missing")
    return actual


def reconcile_accounting_checkpoint(*, root: Path, registry) -> dict[str, object]:
    """Explicit maintenance repair of one witnessed, interrupted DB commit.

    The retained delta contains no callback/source content and cannot grant
    execution authority. Older arbitrary DB generations still require the
    latest retained ledger; this bounded checkpoint covers one commit gap.
    """
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace.production import ProductionWorkspaceReconciliationError, read_lifecycle_receipt, read_accounting_checkpoint
    from src.workspace.accounting_witness import maintenance_accounting_lock as _continuity_lock, ledger_digest as _ledger_digest, witness as _witness, ledger_record
    _require_production_fence(registry)
    with _continuity_lock(root.resolve()) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        checkpoint = read_accounting_checkpoint(workspace)
        if checkpoint is not None and checkpoint.get("schema_version") == 2:
            actual = verify_promoted_composition(workspace)
            from src.workspace.production import write_lifecycle_receipt
            receipt["runtime_composition"] = actual
            write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
            if checkpoint.get("witness") is None:
                return {"status": "already_reconciled", "execution_authority_changed": False,
                        "memory_status": "no_learning"}
        if checkpoint is None or checkpoint.get("witness") != receipt.get("inference_accounting") or checkpoint.get("schema_version") not in {1, 2}:
            raise ProductionWorkspaceReconciliationError("accounting checkpoint authority unavailable")
        database = root / registry.config.database_path
        if not database.is_file() or database.is_symlink():
            raise ProductionWorkspaceReconciliationError("accounting checkpoint database unavailable")
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner_rows = connection.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if len(owner_rows) > 1:
                raise ProductionWorkspaceReconciliationError("accounting checkpoint owner ambiguous")
            old_owner = ledger_record(owner_rows[0]) if owner_rows else None
            old_rows = [ledger_record(row) for row in connection.execute("SELECT * FROM inference_cost_reservations")]
            if old_owner is not None and _witness(old_owner) == checkpoint["witness"] and _ledger_digest(old_owner, old_rows) == old_owner["ledger_digest"]:
                return {"status": "already_reconciled", "revision": old_owner["revision"], "execution_authority_changed": False}
            if (checkpoint.get("base") != (_witness(old_owner) if old_owner is not None else None)
                or (old_owner is not None and _ledger_digest(old_owner, old_rows) != old_owner["ledger_digest"])):
                raise ProductionWorkspaceReconciliationError("accounting checkpoint base revision unavailable")
            account = ledger_record(checkpoint["account"])
            changes = checkpoint.get("operations")
            if not isinstance(changes, list) or len(changes) > 128 or account["revision"] != (old_owner["revision"] + 1 if old_owner is not None else 1):
                raise ProductionWorkspaceReconciliationError("accounting checkpoint revision invalid")
            rows_by_id = {row["operation_id"]: row for row in old_rows}
            for item in changes:
                row = ledger_record(item)
                prior = rows_by_id.get(row["operation_id"])
                if row["deployment_id"] != account["deployment_id"] or (prior is not None and row["revision"] < prior["revision"]):
                    raise ProductionWorkspaceReconciliationError("accounting checkpoint liability regression")
                rows_by_id[row["operation_id"]] = row
            if _witness(account) != checkpoint["witness"] or _ledger_digest(account, list(rows_by_id.values())) != account["ledger_digest"]:
                raise ProductionWorkspaceReconciliationError("accounting checkpoint digest mismatch")
            for table, values in (("inference_accounting_owners", [account]), ("inference_cost_reservations", [rows_by_id[item["operation_id"]] for item in changes])):
                for value in values:
                    fields = dict(value)
                    allowed = {entry[1] for entry in connection.execute(f'PRAGMA table_info("{table}")')}
                    private = {"group_lookup_key"} if table == "inference_cost_reservations" else set()
                    if set(fields) != allowed - private:
                        raise ProductionWorkspaceReconciliationError("accounting checkpoint columns invalid")
                    if private & allowed:
                        # Maintenance remains dependency-free. Startup's
                        # serialized migration classifies this private NULL;
                        # indexed readers block until then. Financial bytes,
                        # revisions, witness and evidence are unchanged.
                        fields["group_lookup_key"] = None
                    names = ",".join('"' + name + '"' for name in fields)
                    connection.execute(f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({",".join("?" for _ in fields)})', tuple(fields.values()))
            connection.commit()
            return {"status": "reconciled", "revision": account["revision"], "execution_authority_changed": False,
                "liabilities_retained": len(changes), "memory_status": "no_learning"}
        finally:
            connection.close()


def retain_inference_accounting(*, active: Path, target: Path, database_path: str) -> dict[str, object]:
    # Only the existing maintenance-fenced lifecycle calls this helper. Its
    # external witness also fences every live ledger mutation.
    from src.workspace.accounting_witness import maintenance_accounting_lock as _continuity_lock, ledger_digest as _ledger_digest, witness as _witness, ledger_record
    from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, read_lifecycle_receipt

    source_path, target_path = active / database_path, target / database_path
    if not source_path.is_file() or source_path.is_symlink() or not target_path.is_file() or target_path.is_symlink():
        raise ProductionWorkspaceReconciliationError("accounting continuity databases unavailable")
    with sqlite3.connect(source_path) as inspection:
        exists = inspection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_accounting_owners'").fetchone()
        empty = not exists or not inspection.execute("SELECT 1 FROM inference_accounting_owners").fetchone()
        if empty:
            prior = read_lifecycle_receipt(ProductionWorkspace(host_root=active.resolve()))
            if prior and prior.get("inference_accounting"):
                raise ProductionWorkspaceReconciliationError("accounting continuity ledger missing")
            target_has = sqlite3.connect(target_path)
            try:
                target_exists = target_has.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_accounting_owners'").fetchone()
                if target_exists and target_has.execute("SELECT 1 FROM inference_accounting_owners").fetchone():
                    raise ProductionWorkspaceReconciliationError("accounting continuity restore authority unavailable")
            finally:
                target_has.close()
            return retain_runtime_composition(active=active, target=target, database_path=database_path)
    with _continuity_lock(active.resolve()) as workspace:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(target_path)
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            receipt = read_lifecycle_receipt(workspace) or {}
            if "inference_accounting_owners" not in tables:
                if receipt.get("inference_accounting"):
                    raise ProductionWorkspaceReconciliationError("accounting continuity ledger missing")
                return {"status": "not_initialized"}
            owner_rows = source.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if not owner_rows:
                if receipt.get("inference_accounting"):
                    raise ProductionWorkspaceReconciliationError("accounting continuity owner missing")
                return {"status": "not_initialized"}
            if len(owner_rows) != 1:
                raise ProductionWorkspaceReconciliationError("accounting continuity owner ambiguous")
            account = ledger_record(owner_rows[0])
            operations = source.execute("SELECT * FROM inference_cost_reservations").fetchall()
            rows = [ledger_record(row) for row in operations]
            if receipt.get("inference_accounting") != _witness(account) or _ledger_digest(account, rows) != account["ledger_digest"]:
                raise ProductionWorkspaceReconciliationError("accounting continuity latest ledger unavailable")
            destination.execute("BEGIN IMMEDIATE")
            target_tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("inference_accounting_owners", "inference_cost_reservations"):
                if table not in target_tables:
                    ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    destination.execute(ddl)
            prior = destination.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if prior and (len(prior) != 1 or prior[0]["deployment_id"] != account["deployment_id"] or prior[0]["revision"] > account["revision"]):
                raise ProductionWorkspaceReconciliationError("accounting continuity foreign or newer restore ledger")
            source_ids = {row["operation_id"] for row in rows}
            for row in destination.execute("SELECT operation_id, revision FROM inference_cost_reservations"):
                current = next((item for item in rows if item["operation_id"] == row["operation_id"]), None)
                if row["operation_id"] not in source_ids or current["revision"] < row["revision"]:
                    raise ProductionWorkspaceReconciliationError("accounting continuity unretained restore liability")
            jobs = []
            if rows:
                if "workflow_run_states" not in tables or "workflow_run_states" not in target_tables:
                    raise ProductionWorkspaceReconciliationError("accounting canonical job schema unavailable")
                for job_id in sorted({row["job_id"] for row in rows}):
                    source_job = source.execute("SELECT * FROM workflow_run_states WHERE run_identity=?", (job_id,)).fetchone()
                    if source_job is None:
                        raise ProductionWorkspaceReconciliationError("accounting canonical job missing")
                    target_job = destination.execute("SELECT * FROM workflow_run_states WHERE run_identity=?", (job_id,)).fetchone()
                    jobs.append((target_job is not None, _retained_job_values(source_job, target_job, account)))
            # This is the verified latest retained ledger, including complete
            # history and immutable operation ownership. Restored data cannot
            # overwrite it. Every linked job is refreshed from that source,
            # blocked and fenced, including rows already present in the target.
            for table, values in (("inference_accounting_owners", owner_rows), ("inference_cost_reservations", operations)):
                destination.execute(f'DELETE FROM "{table}"')
                if values:
                    allowed = {entry[1] for entry in destination.execute(f'PRAGMA table_info("{table}")')}
                    private = {"group_lookup_key"} if table == "inference_cost_reservations" else set()
                    records = []
                    for value in values:
                        fields = dict(value)
                        if set(fields) - private != allowed - private:
                            raise ProductionWorkspaceReconciliationError("accounting continuity columns invalid")
                        fields.pop("group_lookup_key", None)
                        if private & allowed:
                            fields["group_lookup_key"] = None
                        records.append(fields)
                    columns = list(records[0])
                    names = ",".join('"' + column + '"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    destination.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                        [tuple(record[column] for column in columns) for record in records])
            for present, values in jobs:
                if present:
                    assignments = ",".join('"' + column + '"=?' for column in values if column != "id")
                    destination.execute(f'UPDATE workflow_run_states SET {assignments} WHERE id=?',
                        (*[value for column, value in values.items() if column != "id"], values["id"]))
                else:
                    names = ",".join('"' + column + '"' for column in values)
                    destination.execute(f'INSERT INTO workflow_run_states ({names}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
            composition = _retain_composition_in_transaction(source, destination, workspace=workspace,
                source_root=active, target_root=target, account=account)
            destination.commit()
            return {"status": "retained_latest", "revision": account["revision"], "operations": len(rows)}
        finally:
            source.close()
            destination.close()


def _retain_composition_in_transaction(source, destination, *, workspace, source_root, target_root, account=None):
    """Copy the verified latest canonical rows/bytes, never reconstruct hashes.

    Caller already owns stopped maintenance, the one external lock and target
    writer. Missing native extensions/private bytes remain a hard block.
    """
    from src.workspace.accounting_witness import (composition_closure, native_composition_files,
        RETAINED_FIELDS, COMPOSITION_KEYS, composition_row_digest, _composition_row,
        validate_retained_table_schema)
    from src.workspace.production import (read_lifecycle_receipt, read_accounting_checkpoint,
        write_accounting_checkpoint, write_lifecycle_receipt, ProductionWorkspaceReconciliationError)
    source_witness, members = composition_closure(source,
        verify_files=lambda table, row: native_composition_files(table, row, root=source_root))
    receipt = read_lifecycle_receipt(workspace) or {}
    if source_witness is None:
        if receipt.get("runtime_composition") is not None:
            raise ProductionWorkspaceReconciliationError("composition_latest_inventory_missing")
        return {"status": "not_initialized"}
    if source_witness != receipt.get("runtime_composition"):
        raise ProductionWorkspaceReconciliationError("composition_latest_witness_mismatch")
    checkpoint = read_accounting_checkpoint(workspace)
    if checkpoint and checkpoint.get("schema_version") == 2 and checkpoint.get("composition_target") != source_witness:
        if checkpoint.get("composition_base") != source_witness:
            raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
        existing_target, _ = composition_closure(destination,
            verify_files=lambda table, row: native_composition_files(table, row, root=target_root))
        if existing_target == checkpoint.get("composition_target"):
            return {"status": "already_retained", "witness": existing_target}
        raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
    # A copied generation must retain actual private bytes before rows gain a
    # canonical address. A digest does not substitute for missing target files.
    for table, key in members:
        if table in {"work_board_input_artifacts", "workflow_run_states"}:
            row = _composition_row(source, table, key)
            list(native_composition_files(table, row, root=target_root))
    target_tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, fields in RETAINED_FIELDS.items():
        if table not in target_tables:
            ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            destination.execute(ddl)
            if table == "sessions":
                index = source.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_sessions_continuity_task_id'").fetchone()
                if index is not None:
                    destination.execute(index[0])
        validate_retained_table_schema(destination, table, error="composition_restore_schema_unavailable")
    # Check selected destination Sessions even when no destination inventory yet
    # exists; otherwise their unretained task link could survive a row update.
    for table, key in members:
        if table == "sessions" and destination.execute('SELECT 1 FROM sessions WHERE id=?', (key,)).fetchone():
            _composition_row(destination, table, key)
    destination_witness, destination_members = composition_closure(destination,
        verify_files=lambda table, row: native_composition_files(table, row, root=target_root))
    if destination_members - members:
        raise ProductionWorkspaceReconciliationError("composition_foreign_restore_closure")
    source_inventory = {row["runtime_domain"]: row for row in source_witness["inventory"]}
    if destination_witness is not None:
        for row in destination_witness["inventory"]:
            current = source_inventory[row["runtime_domain"]]
            if row["epoch"] > current["epoch"] or (row["epoch"] == current["epoch"] and
                    (row["owner_kind"], row["composition_digest"]) != (current["owner_kind"], current["composition_digest"])):
                raise ProductionWorkspaceReconciliationError("composition_restore_high_water_conflict")
    neutral = account or {"deployment_id": source_witness["inventory_digest"], "revision": 0}
    retained = []
    planned = []
    recovery_rows = []
    from src.runtime_plugins.ownership import checked_recovery_proof, restored_recovery_reference
    from uuid import uuid5, NAMESPACE_URL
    restored_at = datetime.now(timezone.utc).isoformat(sep=" ").replace("+00:00", "")
    for table, key in sorted(members):
        values = _composition_row(source, table, key)
        key_field = COMPOSITION_KEYS[table]
        existing = destination.execute(f'SELECT * FROM "{table}" WHERE "{key_field}"=?', (key,)).fetchone()
        prior = {field: existing[field] for field in RETAINED_FIELDS[table]} if existing is not None else None
        if table == "workflow_run_states":
            values = _retained_job_values(values, prior, neutral)
        elif table == "runtime_composition_states":
            if values["epoch"] >= 2**63 - 1:
                raise ProductionWorkspaceReconciliationError("composition_restore_epoch_exhausted")
            old_owner = {field: values[field] for field in RETAINED_FIELDS[table][:4]}
            new_owner = {**old_owner, "epoch": values["epoch"] + 1}
            proof = {"schema_version": 1, "runtime_domain": key, "prior": old_owner,
                "target": new_owner, "state": "blocked", "phase": "awaiting_boot",
                "prior_recovery_receipt_ref": values["recovery_receipt_ref"]}
            details = json.dumps(proof, sort_keys=True, separators=(",", ":"))
            checked_recovery_proof("runtime_composition_recovery", details)
            recovery_id = uuid5(NAMESPACE_URL, "seraph-stopped-composition-restore-v1:" +
                source_witness["closure_digest"] + ":" + json.dumps(new_owner, sort_keys=True, separators=(",", ":"))).hex
            recovery = {"id": recovery_id, "session_id": None, "actor": "managed_maintenance",
                "event_type": "runtime_composition_recovery", "tool_name": None,
                "risk_level": "low", "policy_mode": "full",
                "summary": "Stopped restoration invalidated prior owner authority.",
                "details_json": details, "created_at": restored_at}
            if source.execute('SELECT 1 FROM audit_events WHERE id=?', (recovery_id,)).fetchone() is not None:
                raise ProductionWorkspaceReconciliationError("composition_restore_recovery_collision")
            collision = destination.execute('SELECT * FROM audit_events WHERE id=?', (recovery_id,)).fetchone()
            if collision is not None:
                if {field: collision[field] for field in RETAINED_FIELDS["audit_events"]} != recovery:
                    raise ProductionWorkspaceReconciliationError("composition_restore_recovery_collision")
            else:
                recovery_rows.append(("audit_events", recovery_id, None, recovery))
            values = {**values, **new_owner, "state": "blocked",
                "recovery_receipt_ref": restored_recovery_reference(source_witness["closure_digest"], recovery_id)}
        elif table == "work_board_attempts":
            source_fence = values["fencing_token"]
            values = {**values, "lease_owner": None, "lease_expires_at": None,
                "fencing_token": max(values["fencing_token"], prior["fencing_token"] if prior else -1) + 1}
            if prior is not None and prior["lease_owner"] is None and prior["lease_expires_at"] is None:
                mutable = {"fencing_token"}
                if prior["fencing_token"] > source_fence and all(prior[field] == value for field, value in values.items() if field not in mutable):
                    values = prior
        elif table == "production_workflow_authority_states" and values["workflow_phase"] not in {"blocked", "cancelled", "failed"}:
            values.update(workflow_phase="blocked", safe_replay_decision="unsafe",
                          blocked_replay_reason="workspace_restore_requires_reconciliation")
        if prior != values:
            planned.append((table, key, prior, values))
    operations = checkpoint.get("operations", []) if checkpoint and checkpoint.get("schema_version") == 2 else []
    planned = recovery_rows + planned
    if len(operations) + len(planned) > 128:
        raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
    # Validate the complete actual delta before the first retained row changes.
    for table, key, prior, values in planned:
        retained.append({"table_id": table, "key": key,
            "before_digest": composition_row_digest(table, key, prior) if prior is not None else None,
            "after_digest": composition_row_digest(table, key, values)})
    for table, key, prior, values in planned:
        key_field = COMPOSITION_KEYS[table]
        if prior is None:
            names = ",".join('"' + field + '"' for field in values)
            destination.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
        else:
            names = ",".join('"' + field + '"=?' for field in values if field != key_field)
            destination.execute(f'UPDATE "{table}" SET {names} WHERE "{key_field}"=?',
                (*[value for field, value in values.items() if field != key_field], key))
    target_witness, _ = composition_closure(destination,
        verify_files=lambda table, row: native_composition_files(table, row, root=target_root))
    # Source deployment receipt stays its latest stopped generation. The
    # target witness travels in the already-existing shared checkpoint and
    # only becomes the promoted receipt during native root reconciliation.
    write_accounting_checkpoint(workspace, {**(checkpoint or {}), "schema_version": 2,
        "composition_base": source_witness, "composition_target": target_witness,
        "composition_delta": retained, "secret_values_included": False})
    return {"status": "retained_latest", "jobs": sum(table == "workflow_run_states" for table, _ in members),
            "witness": target_witness}


def retain_runtime_composition(*, active: Path, target: Path, database_path: str):
    from src.workspace.accounting_witness import maintenance_accounting_lock
    from src.workspace.production import ProductionWorkspace, read_lifecycle_receipt, ProductionWorkspaceReconciliationError
    with sqlite3.connect(active / database_path) as probe:
        exists = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'").fetchone()
        initialized = exists and probe.execute("SELECT 1 FROM runtime_composition_states LIMIT 1").fetchone()
    if not initialized:
        if (read_lifecycle_receipt(ProductionWorkspace(host_root=active.resolve())) or {}).get("runtime_composition"):
            raise ProductionWorkspaceReconciliationError("composition_latest_inventory_missing")
        return {"status": "not_initialized"}
    with maintenance_accounting_lock(active.resolve()) as workspace:
        with sqlite3.connect(active / database_path) as source, sqlite3.connect(target / database_path) as destination:
            source.row_factory = destination.row_factory = sqlite3.Row
            destination.execute("BEGIN IMMEDIATE")
            _retain_composition_in_transaction(source, destination, workspace=workspace,
                source_root=active, target_root=target)
            return {"status": "not_initialized"}
