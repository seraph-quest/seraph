"""Passive bounded local metadata projection under current Python owners."""
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
import json
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from threading import Lock
import hashlib

from sqlalchemy import text

from config.settings import settings
from src.auth.service import AuthFailure
from src.db.engine import get_session
from pydantic import TypeAdapter
from src.operator.home_contracts import HomeContinuation, Item
from src.operator.home_cursor import CursorCodec, HomeCursorError, KINDS, NULL_DUE, Position, micros, timestamp


SECTIONS = ("active_goals", "programme_status", "task_next_actions", "prepared_outputs", "approvals", "blocked_items")
ITEM_ADAPTER = TypeAdapter(Item)


@dataclass(frozen=True)
class PolicyHandle:
    generation: int
    epoch: int
    digest: str
    blocked_reason: str | None


def _policy_facts(configured):
    from src.model_fabric.effective_policy import inference_policy_from_configuration
    try:
        _,digest = inference_policy_from_configuration(configured)
        return configured.egress_revision,digest,None
    except PermissionError:
        digest = hashlib.sha256(json.dumps({"blocked":configured.status,"revoked":configured.egress_revoked,
            "epoch":configured.egress_revision},sort_keys=True,separators=(",",":")).encode()).hexdigest()
        return configured.egress_revision,digest,"provider_policy_unavailable"


def _us(column):
    # Integer seconds plus six literal fractional digits: no floating dates or
    # SQLite %f millisecond truncation. Canonical SQLite timestamps are UTC.
    return (f"(CAST(strftime('%s',substr({column},1,19)||CASE WHEN substr({column},-6,1) IN ('+','-') "
        f"THEN substr({column},-6) ELSE '' END) AS INTEGER)*1000000 + "
        f"CASE WHEN substr({column},20,1)='.' THEN CAST(substr(substr({column},21)||'000000',1,6) AS INTEGER) ELSE 0 END)")


def _scope(alias, kind, identity_column, root_column, principal_column):
    current = f"({alias}.{root_column}=:root AND {alias}.{principal_column}=:principal)"
    recovered = f"""(:recovery_ok AND EXISTS (
      SELECT 1 FROM operator_recovery_journals j
      JOIN json_each(CASE WHEN json_valid(j.selections_json) THEN j.selections_json ELSE '[]' END) s
      JOIN operator_sessions r ON r.id={alias}.{root_column}
      WHERE j.identity_id=:identity AND j.state='confirmed'
        AND r.operator_identity_id=:identity AND r.is_bearer_tombstone=0
        AND ({alias}.{principal_column}=r.principal_id OR
          (r.legacy_owner_principal_id IS NOT NULL AND {alias}.{principal_column}=r.legacy_owner_principal_id))
        AND json_extract(CASE WHEN s.type='object' THEN s.value ELSE '{{}}' END,'$.kind')='{kind}'
        AND json_extract(CASE WHEN s.type='object' THEN s.value ELSE '{{}}' END,'$.record_id')={alias}.{identity_column}
        AND json_extract(CASE WHEN s.type='object' THEN s.value ELSE '{{}}' END,'$.source_session_id')={alias}.{root_column}
        AND json_extract(CASE WHEN s.type='object' THEN s.value ELSE '{{}}' END,'$.source_principal_id')={alias}.{principal_column}))"""
    return f"({current} OR {recovered})"


GOAL_SCOPE = _scope("g", "goal", "id", "owner_session_id", "owner_principal_id")
TASK_SCOPE = _scope("t", "task", "task_id", "owner_session_id", "owner_principal_id")
GUS, TUS, AUS = _us("g.updated_at"), _us("t.updated_at"), _us("a.created_at")


BASE = f"""WITH
readable_goals AS (SELECT g.id,g.revision,g.status,g.sort_order,g.due_date,g.updated_at,
 g.goal_programmes_json,g.owner_principal_id,g.owner_session_id,
 CASE WHEN g.owner_session_id=:root AND g.owner_principal_id=:principal THEN 'current' ELSE 'recovered_read_only' END access
 FROM goals g WHERE {GOAL_SCOPE}),
tasks AS (SELECT t.task_id,t.task_revision,t.goal_id,t.goal_revision,t.status,t.priority,t.priority_explicit,
 t.scheduled_at,t.updated_at,t.block_kind,t.owner_principal_id,t.owner_session_id,
 CASE WHEN t.owner_session_id=:root AND t.owner_principal_id=:principal THEN 'current' ELSE 'recovered_read_only' END access,
 a.attempt_id,a.ended_at,a.outcome
 FROM work_board_tasks t LEFT JOIN work_board_attempts a ON a.attempt_id=(SELECT aa.attempt_id
 FROM work_board_attempts aa WHERE aa.task_id=t.task_id ORDER BY aa.created_at DESC,aa.attempt_id DESC LIMIT 1)
 WHERE {TASK_SCOPE} AND EXISTS (SELECT 1 FROM goals gg WHERE gg.id=t.goal_id
 AND gg.owner_principal_id=t.owner_principal_id AND gg.owner_session_id=t.owner_session_id)),
approvals AS (SELECT a.id,a.expires_at,a.created_at FROM approval_requests a
 JOIN sessions s ON s.id=a.session_id
 WHERE a.owner_principal_id=:principal AND a.operator_session_id=:root
 AND s.owner_principal_id=:principal AND a.status='pending'),
programme_sources AS (SELECT g.id goal_id,g.revision goal_revision,g.updated_at,g.access,
 g.status goal_status,g.owner_principal_id,g.owner_session_id,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.id') id,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.grant_revision') grant_revision,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.state') stored_state,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.reason_code') reason,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.expires_at') expires_at,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.goal_revision') admitted_goal_revision,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.issuer_root_id') issuer_root_id,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.issuer_principal_id') issuer_principal_id,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.route_epoch') route_epoch,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.route_digest') route_digest,
 json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.budget.max_inference_microusd') budget,
 issuer.id proved_issuer,issuer.operator_identity_id issuer_identity,issuer.is_bearer_tombstone issuer_tombstone,
 issuer.principal_id proved_principal
 FROM readable_goals g JOIN json_each(CASE WHEN json_valid(g.goal_programmes_json)
 THEN g.goal_programmes_json ELSE '{{"generations":[]}}' END,'$.generations') p
 LEFT JOIN operator_sessions issuer ON issuer.id=json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.issuer_root_id')
 WHERE :programme_ok AND json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.schema_version')='GoalProgramme.v1'
 AND json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.goal_id')=g.id
 AND json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END,'$.owner_identity_id')=:identity),
programmes AS (SELECT *,CASE
 WHEN stored_state!='active' THEN stored_state
 WHEN proved_issuer IS NULL OR issuer_identity IS NOT :identity OR issuer_tombstone!=0
   OR proved_principal IS NOT issuer_principal_id THEN 'blocked'
 WHEN goal_revision!=admitted_goal_revision OR owner_principal_id!=issuer_principal_id
   OR owner_session_id!=issuer_root_id OR goal_status!='active' THEN 'paused'
 WHEN {_us('expires_at')}<=:now THEN 'review_due'
 WHEN :policy_blocked IS NOT NULL THEN 'blocked'
 WHEN route_epoch!=:policy_epoch OR route_digest!=:policy_digest THEN 'paused'
 WHEN budget=0 THEN 'blocked' ELSE 'active' END state FROM programme_sources)
"""


def _row(section, kind, band, value, due, source, key, identity, revision="NULL", goal="NULL", goal_revision="NULL",
         programme="NULL", grant="NULL", state="NULL", reason="NULL", priority="NULL", scheduled="NULL",
         expires="NULL", attempt="NULL", access="access"):
    # Arguments are fixed source-owned SQL expressions, never caller strings.
    return f"""SELECT '{section}' section,{kind} kind,{band} band,{value} value,
      COALESCE({due},{NULL_DUE}) due_us,{source} source_us,{key} key_hex,{identity} id,
      {revision} revision,{goal} goal_id,{goal_revision} goal_revision,{programme} programme_id,
      {grant} grant_revision,{state} state,{reason} reason,{priority} priority,{scheduled} scheduled_at,
      {expires} expires_at,{attempt} attempt_id,{access} access"""


TASK_US = _us("updated_at")
PROG_KEY = "printf('%04X',length(CAST(goal_id AS BLOB)))||hex(goal_id)||hex(id)||printf('%016X',grant_revision)"
GOALS = _row("active_goals", 0, 4, "sort_order", _us("due_date"), TASK_US, "hex(id)", "id", "revision") + " FROM readable_goals WHERE status='active'"
PROGS = _row("programme_status", 1, 5, "0", f"CASE WHEN state='active' AND :digest_us<{_us('expires_at')} THEN :digest_us ELSE NULL END", TASK_US, PROG_KEY, "id", goal="goal_id",
    goal_revision="goal_revision", programme="id", grant="grant_revision", state="state", reason="reason", expires="expires_at") + " FROM programmes"
NEXT = _row("task_next_actions", 2, f"CASE WHEN priority_explicit=1 THEN 0 WHEN scheduled_at IS NOT NULL AND {_us('scheduled_at')}<=:as_of AND status IN ('todo','ready') THEN 1 ELSE 7 END",
    "priority", _us("scheduled_at"), TASK_US, "hex(task_id)", "task_id", "task_revision", "goal_id", "goal_revision",
    state="status", priority="priority", scheduled="scheduled_at") + " FROM tasks WHERE status IN ('triage','todo','ready','running')"
OUTPUTS = _row("prepared_outputs", 3, 6, "0", "NULL", f"COALESCE({_us('ended_at')},{TASK_US})", "hex(task_id)", "task_id",
    "task_revision", "goal_id", "goal_revision", state="CASE WHEN outcome='verified' THEN 'prepared' ELSE 'unknown' END", attempt="attempt_id") + " FROM tasks WHERE status IN ('review','done') AND attempt_id IS NOT NULL"
APPROVALS = _row("approvals", 4, 2, "0", _us("expires_at"), _us("created_at"), "hex(id)", "id",
    expires="expires_at", access="'current'") + f" FROM approvals WHERE expires_at IS NULL OR {_us('expires_at')}>:now"
BLOCKED_TASKS = _row("blocked_items", 5, 3, "0", "NULL", TASK_US, "hex(task_id)", "task_id",
    "task_revision", "goal_id", "goal_revision", reason="block_kind") + " FROM tasks WHERE status='blocked' OR (status IN ('review','done') AND attempt_id IS NULL)"
BLOCKED_APPROVALS = _row("blocked_items", 6, 3, "0", _us("expires_at"), _us("created_at"), "hex(id)", "id",
    reason="'approval_expired'", access="'current'") + f" FROM approvals WHERE expires_at IS NOT NULL AND {_us('expires_at')}<=:now"
BLOCKED_PROGS = _row("blocked_items", 7, 3, "0", "NULL", TASK_US, PROG_KEY, "id", goal="goal_id",
    goal_revision="goal_revision", programme="id", grant="grant_revision", reason="'programme_'||state") + " FROM programmes WHERE state!='active'"
STREAMS = (GOALS, PROGS, NEXT, OUTPUTS, APPROVALS, BLOCKED_TASKS + " UNION ALL " + BLOCKED_APPROVALS + " UNION ALL " + BLOCKED_PROGS)
PROGRAMME_FIELDS = ("schema_version", "id", "goal_id", "goal_revision", "public_brief", "brief_digest",
    "grant_revision", "expires_at", "confirmed_at", "capability_ids", "budget", "cadence",
    "notification_limits", "state", "reason_code", "artifact_prefix", "owner_identity_id",
    "issuer_root_id", "issuer_principal_id", "route_epoch", "route_digest", "review_digest")
PROGRAMME_FIELD_SQL = ",".join("'"+name+"'" for name in PROGRAMME_FIELDS)
PROGRAMME_DIAGNOSTIC = f"""COALESCE(max(CASE WHEN goal_programmes_json IS NULL OR goal_programmes_json='' THEN 0
 WHEN json_valid(goal_programmes_json)=0 THEN 1
 WHEN json_type(goal_programmes_json) IS NOT 'object'
 OR json_type(goal_programmes_json,'$.revision') IS NOT 'integer'
 OR json_extract(goal_programmes_json,'$.revision')<0
 OR (SELECT count(*) FROM json_each(goal_programmes_json))!=3
 OR EXISTS (SELECT 1 FROM json_each(goal_programmes_json) k WHERE k.key NOT IN ('revision','preview','generations'))
 OR json_type(goal_programmes_json,'$.generations') IS NOT 'array'
 OR json_array_length(goal_programmes_json,'$.generations')>128 THEN 1
 WHEN EXISTS (SELECT 1 FROM json_each(goal_programmes_json,'$.generations') p WHERE CASE
 WHEN p.type!='object' THEN 1
 WHEN (SELECT count(*) FROM json_each(p.value))!={len(PROGRAMME_FIELDS)}
 OR EXISTS (SELECT 1 FROM json_each(p.value) k WHERE k.key NOT IN ({PROGRAMME_FIELD_SQL}))
 OR json_extract(p.value,'$.schema_version') IS NOT 'GoalProgramme.v1'
 OR json_type(p.value,'$.id') IS NOT 'text' OR length(json_extract(p.value,'$.id'))!=32
 OR json_extract(p.value,'$.id') GLOB '*[^0-9a-f]*'
 OR json_extract(p.value,'$.goal_id') IS NOT g.id
 OR json_type(p.value,'$.goal_revision') IS NOT 'integer' OR json_extract(p.value,'$.goal_revision')<1
 OR typeof(json_extract(p.value,'$.goal_revision'))!='integer'
 OR json_type(p.value,'$.grant_revision') IS NOT 'integer' OR json_extract(p.value,'$.grant_revision')<1
 OR typeof(json_extract(p.value,'$.grant_revision'))!='integer'
 OR json_extract(p.value,'$.state') NOT IN ('active','blocked','paused','revoked','review_due')
 OR json_type(p.value,'$.state') IS NOT 'text'
 OR {_us("json_extract(p.value,'$.expires_at')")} IS NULL
 OR {_us("json_extract(p.value,'$.confirmed_at')")} IS NULL
 OR json_type(p.value,'$.route_epoch') IS NOT 'integer' OR json_extract(p.value,'$.route_epoch')<1
 OR typeof(json_extract(p.value,'$.route_epoch'))!='integer'
 OR json_type(p.value,'$.route_digest') IS NOT 'text'
 OR length(json_extract(p.value,'$.route_digest'))!=64 OR json_extract(p.value,'$.route_digest') GLOB '*[^0-9a-f]*'
 OR json_type(p.value,'$.budget.max_inference_microusd') IS NOT 'integer'
 OR typeof(json_extract(p.value,'$.budget.max_inference_microusd'))!='integer'
 OR json_extract(p.value,'$.budget.max_inference_microusd')<0
 OR json_type(p.value,'$.owner_identity_id') IS NOT 'text'
 OR (:identity IS NOT NULL AND json_extract(p.value,'$.owner_identity_id') IS NOT :identity)
 OR json_type(p.value,'$.issuer_root_id') IS NOT 'text'
 OR json_type(p.value,'$.issuer_principal_id') IS NOT 'text' THEN 1 ELSE 0 END LIMIT 1) THEN 1
 WHEN EXISTS (SELECT 1 FROM json_each(goal_programmes_json,'$.generations') p
 GROUP BY json_extract(p.value,'$.id') HAVING count(*)>1 LIMIT 1) THEN 1 ELSE 0 END),0) malformed"""
ORDER = "band ASC,CASE WHEN band IN (0,1,7) THEN value END DESC,CASE WHEN band NOT IN (0,1,7) THEN value END ASC,due_us ASC,source_us ASC,kind ASC,key_hex COLLATE BINARY ASC"
AFTER = """(band>:last_band OR (band=:last_band AND (
 ((band IN (0,1,7) AND value<:last_value) OR (band NOT IN (0,1,7) AND value>:last_value))
 OR (value=:last_value AND (due_us>:last_due OR (due_us=:last_due AND
 (source_us>:last_source OR (source_us=:last_source AND
(kind>:last_kind OR (kind=:last_kind AND key_hex>:last_key COLLATE BINARY))))))))))"""


class HomeProjection:
    def __init__(self):
        self.started = False
        self.timezone = None
        self.timezone_name = None
        self._state_lock = Lock()
        self._policy_generation = 0
        self._policy = None
        self._codec = None

    def start(self):
        # Handles only. Configuration stays memory-owned; no lazy file/key creation.
        from src.model_fabric.configuration import _policy_publication_lock,read_model_fabric_configuration
        from src.extensions.capability_execution import _server_secret_key,CapabilityJournalError
        with _policy_publication_lock:
            name = settings.user_timezone
            try:
                zone = ZoneInfo(name)
            except (ValueError,KeyError):
                zone = None
            try:
                codec = CursorCodec(_server_secret_key())
            except CapabilityJournalError:
                codec = None
            facts = _policy_facts(read_model_fabric_configuration())
            with self._state_lock:
                self._policy_generation += 1
                self._policy = PolicyHandle(self._policy_generation,*facts)
                self._codec = codec
                self.timezone_name,self.timezone = name,zone
                self.started = True

    def stop(self):
        with self._state_lock:
            self.started = False
            self._policy_generation += 1
            self._policy = self._codec = None
            self.timezone = self.timezone_name = None

    def begin_policy_publication(self):
        with self._state_lock:
            if not self.started:
                return None
            self._policy_generation += 1
            self._policy = None
            return self._policy_generation

    def finish_policy_publication(self,generation,configured):
        if generation is None:
            return
        facts = _policy_facts(configured)
        with self._state_lock:
            if self.started and self._policy_generation==generation:
                self._policy = PolicyHandle(generation,*facts)

    def policy_context(self):
        with self._state_lock:
            policy = self._policy
            context = json.dumps({"timezone":settings.user_timezone,"generation":self._policy_generation,
                "epoch":policy.epoch if policy else None,"digest":policy.digest if policy else None,
                "blocked":policy.blocked_reason if policy else "provider_policy_unavailable"},
                sort_keys=True,separators=(",",":"))
            return policy,context

    def read_context(self):
        with self._state_lock:
            if not self.started or self._codec is None:
                raise HomeCursorError("continuation_unavailable")
            policy = self._policy
            context = json.dumps({"timezone":settings.user_timezone,"generation":self._policy_generation,
                "epoch":policy.epoch if policy else None,"digest":policy.digest if policy else None,
                "blocked":policy.blocked_reason if policy else "provider_policy_unavailable"},
                sort_keys=True,separators=(",",":"))
            return self._codec,policy,self.timezone,self.timezone_name,context

    def codec(self):
        with self._state_lock:
            if not self.started or self._codec is None:
                raise HomeCursorError("continuation_unavailable")
            return self._codec

    async def current(self, db, operator, now):
        row = (await db.execute(text("""SELECT r.id,r.operator_identity_id identity_id FROM operator_sessions r
        LEFT JOIN operator_identities i ON i.id=r.operator_identity_id
        WHERE r.id=:root AND r.principal_id=:principal AND r.token_hash=:token
        AND r.is_bearer_tombstone=0 AND r.revoked_at IS NULL AND r.replaced_by_id IS NULL
        AND r.idle_expires_at>:now AND r.absolute_expires_at>:now
        AND (r.operator_identity_id IS NULL OR (i.id IS NOT NULL AND i.revoked_at IS NULL))
        AND NOT EXISTS (SELECT 1 FROM operator_sessions p WHERE p.replaced_by_id=r.id AND p.is_bearer_tombstone=0)
        AND NOT EXISTS (SELECT 1 FROM operator_sessions p WHERE p.replaced_by_id=r.id AND p.is_bearer_tombstone=1 AND p.revoked_at IS NULL)
        """), {"root": operator.session_id, "principal": operator.principal.principal_id,
            "token": operator._token_hash, "now": now.replace(tzinfo=None)})).mappings().one_or_none()
        if row is None or row["identity_id"] != operator.operator_identity_id:
            raise AuthFailure("ownership_proof_required")
        return row["identity_id"]

    async def page(self, operator, *, limit=20, cursor=None):
        codec,policy,zone,zone_name,context = self.read_context()
        now_dt = datetime.now(timezone.utc)
        now = micros(now_dt)
        last = None
        if cursor:
            as_of, expires, last = codec.decode(cursor, now=now, limit=limit,
                principal=operator.principal.principal_id, root=operator.session_id,
                identity=operator.operator_identity_id,context=context)
        else:
            as_of = now
            expires = min(now + 300_000_000, micros(operator.idle_expires_at), micros(operator.absolute_expires_at))
        try:
            if zone is None or settings.user_timezone != zone_name:
                raise ValueError("programme_timezone_invalid")
            local = timestamp(as_of).astimezone(zone)
            next_digest = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if local >= next_digest:
                next_digest += timedelta(days=1)
            digest_us = micros(next_digest)
            timezone_ok = True
        except ValueError:
            digest_us, timezone_ok = NULL_DUE, False
        parameters = {"root": operator.session_id, "principal": operator.principal.principal_id,
            "identity": operator.operator_identity_id, "as_of": as_of, "now": now,
            "digest_us": digest_us, "recovery_ok": operator.operator_identity_id is not None,
            "programme_ok": timezone_ok and operator.operator_identity_id is not None and policy is not None,
            "policy_epoch":policy.epoch if policy else None,"policy_digest":policy.digest if policy else None,
            "policy_blocked":policy.blocked_reason if policy else "provider_policy_unavailable"}
        async with get_session() as db:
            await db.execute(text("BEGIN"))
            await self.current(db, operator, now_dt)
            recovery = (await db.execute(text("""SELECT
             count(*)>100 overflow,
             COALESCE(max(CASE WHEN json_valid(selections_json)=0 THEN 1
               WHEN json_type(selections_json) IS NOT 'array' OR json_array_length(selections_json)>50 THEN 1
               WHEN EXISTS (SELECT 1 FROM json_each(selections_json) e WHERE CASE WHEN e.type!='object' THEN 1
                WHEN json_type(e.value,'$.kind') IS NOT 'text' OR json_type(e.value,'$.record_id') IS NOT 'text'
                OR json_type(e.value,'$.source_session_id') IS NOT 'text' OR json_type(e.value,'$.source_principal_id') IS NOT 'text'
                THEN 1 ELSE 0 END LIMIT 1) THEN 1 ELSE 0 END),0) malformed
             FROM operator_recovery_journals WHERE identity_id=:identity"""), parameters)).mappings().one()
            parameters["recovery_ok"] = parameters["recovery_ok"] and not recovery["overflow"] and not recovery["malformed"]
            diagnostic = (await db.execute(text(BASE + " SELECT " + PROGRAMME_DIAGNOSTIC + " FROM readable_goals g"), parameters)).mappings().one()
            parameters["programme_ok"] = parameters["programme_ok"] and not diagnostic["malformed"]
            sources = (await db.execute(text(BASE + f"""SELECT
              EXISTS(SELECT 1 FROM readable_goals WHERE id IS NULL OR length(CAST(id AS BLOB)) NOT BETWEEN 1 AND 512
                OR typeof(revision)!='integer' OR revision<1 OR typeof(sort_order)!='integer'
                OR {_us('updated_at')} IS NULL OR (due_date IS NOT NULL AND {_us('due_date')} IS NULL)
                OR status NOT IN ('active','completed','paused','abandoned') LIMIT 1) goals_bad,
              EXISTS(SELECT 1 FROM tasks WHERE task_id IS NULL OR length(CAST(task_id AS BLOB)) NOT BETWEEN 1 AND 512
                OR task_id GLOB '*[^A-Za-z0-9_.:/-]*' OR typeof(task_revision)!='integer' OR task_revision<1
                OR typeof(goal_revision)!='integer' OR goal_revision<1
                OR typeof(priority)!='integer' OR priority NOT BETWEEN 0 AND 100
                OR priority_explicit NOT IN (0,1) OR {_us('updated_at')} IS NULL
                OR (scheduled_at IS NOT NULL AND {_us('scheduled_at')} IS NULL)
                OR status NOT IN ('triage','todo','ready','running','blocked','review','done','archived') LIMIT 1) tasks_bad,
              EXISTS(SELECT 1 FROM approvals WHERE id IS NULL OR length(CAST(id AS BLOB)) NOT BETWEEN 1 AND 512
                OR {_us('created_at')} IS NULL OR (expires_at IS NOT NULL AND {_us('expires_at')} IS NULL) LIMIT 1) approvals_bad
              """),parameters)).mappings().one()
            candidates = []
            invalid = set()
            if sources["goals_bad"]:
                invalid.update(("active_goals","programme_status","blocked_items"))
            if sources["tasks_bad"]:
                invalid.update(("task_next_actions","prepared_outputs","blocked_items"))
            if sources["approvals_bad"]:
                invalid.update(("approvals","blocked_items"))
            for index, stream in enumerate(STREAMS):
                where = f"source_us<=:as_of AND {'1' if last is None else AFTER}"
                if last:
                    parameters.update(last_band=last.band, last_value=last.value, last_due=last.due_us,
                        last_source=last.source_us, last_kind=last.kind, last_key=last.key.hex().upper())
                rows = list((await db.execute(text(BASE + f"SELECT * FROM ({stream}) WHERE {where} ORDER BY {ORDER} LIMIT 21"), parameters)).mappings())
                for row in rows:
                    try:
                        candidate = self.candidate(row)
                    except (ValueError, TypeError, OverflowError):
                        invalid.add(SECTIONS[index])
                        continue
                    candidates.append(candidate)
            if last:
                # Same original source, ownership and complete key; no callback or file read.
                stream = STREAMS[{0:0,1:1,2:2,3:3,4:4,5:5,6:5,7:5}[last.kind]]
                anchor = (await db.execute(text(BASE + f"SELECT 1 FROM ({stream}) WHERE band=:last_band AND value=:last_value AND due_us=:last_due AND source_us=:last_source AND kind=:last_kind AND key_hex=:last_key LIMIT 1"), parameters)).scalar_one_or_none()
                if anchor is None:
                    raise HomeCursorError("continuation_stale")
            candidates.sort(key=cmp_to_key(lambda a,b: a[0].compare(b[0])))
            selected = candidates[:limit]
            await self.enrich(db, selected, identity=operator.operator_identity_id)
            await self.current(db, operator, datetime.now(timezone.utc))
            if context != self.read_context()[-1]:
                raise HomeCursorError("continuation_stale")
        body = {name: {"items": [], "state": "empty", "source_as_of": timestamp(as_of).isoformat()} for name in SECTIONS}
        for _position, section, item in selected:
            body[section]["items"].append(item)
            body[section]["state"] = "ready"
        if operator.operator_identity_id is not None and not parameters["recovery_ok"]:
            invalid.update(SECTIONS)
        if not parameters["programme_ok"]:
            invalid.add("programme_status")
            if operator.operator_identity_id is not None:
                invalid.add("blocked_items")
        for name in invalid:
            body[name]["state"] = "degraded" if body[name]["items"] else "blocked"
        body["as_of"] = timestamp(as_of).isoformat()
        output = HomeContinuation.model_validate_json(json.dumps(body))
        next_cursor = None
        if len(candidates) > limit and selected:
            next_cursor = codec.encode(as_of=as_of, expires=expires, limit=limit,
                principal=operator.principal.principal_id, root=operator.session_id,
                identity=operator.operator_identity_id, context=context, position=selected[-1][0])
        return output, next_cursor

    def candidate(self, row):
        from src.operator.home_cursor import source_id, programme_key
        kind = row["kind"]
        key = programme_key(row["goal_id"], row["programme_id"], row["grant_revision"]) if kind in {1,7} else source_id(row["id"], task=kind in {2,3,5})
        if key.hex().upper() != row["key_hex"] or type(row["value"]) is not int:
            raise ValueError()
        position = Position(row["band"], row["value"], row["due_us"], row["source_us"], kind, key)
        source = timestamp(position.source_us).isoformat()
        item = {"kind": KINDS[kind], "ownership_access": row["access"], "source_at": source}
        if kind == 0:
            item.update(goal_id=row["id"], goal_revision=row["revision"], status="active", sort_order=row["value"],
                due_at=None if position.due_us == NULL_DUE else timestamp(position.due_us).isoformat(),
                target={"kind":"goal","goal_id":row["id"],"goal_revision":row["revision"]})
        elif kind in {1,7}:
            target = {"kind":"programme","goal_id":row["goal_id"],"programme_id":row["programme_id"],"goal_revision":row["goal_revision"]}
            item.update(goal_id=row["goal_id"], goal_revision=row["goal_revision"], programme_id=row["programme_id"],
                grant_revision=row["grant_revision"], target=target)
            if kind == 1:
                item.update(state=row["state"],reason_code=None if row["state"]=='active' else 'programme_'+row["state"],expires_at=self.utc(row["expires_at"]),
                    next_digest_at=None if position.due_us==NULL_DUE else timestamp(position.due_us).isoformat())
            else:
                item["reason_code"] = self.reason(row["reason"])
        elif kind in {2,3,5}:
            source_id(row["goal_id"])
            target = {"kind":"task","task_id":row["id"],"task_revision":row["revision"]}
            item.update(task_id=row["id"],task_revision=row["revision"])
            if kind != 3:
                item.update(goal_id=row["goal_id"],goal_revision=row["goal_revision"])
            if kind == 2:
                if not 0 <= row["priority"] <= 100:
                    raise ValueError()
                item.update(status=row["state"],priority=row["priority"],scheduled_at=self.utc(row["scheduled_at"]),
                    action="review_plan" if row["state"]=="triage" else "inspect_task",method=None,target=target)
            elif kind == 3:
                source_id(row["attempt_id"],task=True)
                item.update(attempt_id=row["attempt_id"],output_state=row["state"],method=None,
                    target={"kind":"output",**{k:v for k,v in target.items() if k!='kind'},"attempt_id":row["attempt_id"]})
            else:
                item.update(reason_code=self.reason(row["reason"]),target=target)
        else:
            item.update(approval_id=row["id"],target={"kind":"approval","approval_id":row["id"]})
            if kind == 4:
                item.update(status="pending",expires_at=self.utc(row["expires_at"]))
            else:
                item["reason_code"]="approval_expired"
        ITEM_ADAPTER.validate_json(json.dumps(item))
        return position, row["section"], item

    @staticmethod
    def utc(value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()

    @staticmethod
    def reason(value):
        from typing import get_args
        from src.operator.home_contracts import Reason
        return value if value in get_args(Reason) else "recovery_unknown"

    async def enrich(self, db, selected, *, identity):
        from src.work_board.historical_method import project_historical_method
        ids = list({item["task_id"] for _position, _section, item in selected if "method" in item})
        if not ids:
            return
        bindings = {f"task{i}": identity for i,identity in enumerate(ids)}
        placeholders = ",".join(":" + name for name in bindings)
        rows = (await db.execute(text(f"""SELECT t.task_id,t.owner_principal_id,t.owner_session_id,
          t.goal_id,t.goal_revision,t.input_artifact_id,t.typed_input_digest,t.capability_id,
          CASE WHEN length(CAST(t.admitted_method_json AS BLOB))<=65536 THEN t.admitted_method_json ELSE NULL END admitted_method_json,
          a.attempt_id,a.task_id attempt_task_id,
          CASE WHEN length(CAST(a.admitted_method_json AS BLOB))<=65536 THEN a.admitted_method_json ELSE NULL END attempt_method
          FROM work_board_tasks t LEFT JOIN work_board_attempts a ON a.attempt_id=(
            SELECT aa.attempt_id FROM work_board_attempts aa WHERE aa.task_id=t.task_id
            ORDER BY aa.created_at DESC,aa.attempt_id DESC LIMIT 1)
          WHERE t.task_id IN ({placeholders})"""), bindings)).mappings().all()
        by_id = {row["task_id"]:row for row in rows}
        for _position, _section, item in selected:
            if "method" not in item:
                continue
            row = by_id.get(item["task_id"])
            if row is None or row["capability_id"] != "agent.task.v1":
                item["method"] = None
                continue
            if identity is None:
                item["method"] = {"status":"unknown", "method_id":None, "version":None,
                    "digest":None, "admitted_at":None, "lifecycle":"unavailable",
                    "reason_code":"method_canonical_unavailable", "target":None}
                continue
            attempt = {"attempt_id":row["attempt_id"],"task_id":row["attempt_task_id"]} if row["attempt_id"] else None
            raw = row["attempt_method"] if attempt else row["admitted_method_json"]
            value = project_historical_method(raw,task=row,attempt=attempt)
            if value["status"] == "admitted":
                # Canonical visibility is enriched below; this is historical
                # source evidence, not a claim of current execution eligibility.
                value["target"] = None
                value["lifecycle"] = "unavailable"
                value["reason_code"] = "method_canonical_unavailable"
            item["method"] = value
        pins = [item for _position,_section,item in selected if item.get("method")
            and item["method"]["status"]=="admitted"]
        if not pins:
            return
        values, parameters = [], {"identity":identity}
        for index,item in enumerate(pins):
            values.append(f"(:task{index},:proposal{index},:version{index},:digest{index})")
            parameters.update({f"task{index}":item["task_id"],f"proposal{index}":item["method"]["method_id"],
                f"version{index}":item["method"]["version"],f"digest{index}":item["method"]["digest"]})
        metadata = "CASE WHEN json_valid(m.metadata_json) THEN m.metadata_json ELSE '{}' END"
        scope = "$.work_board_provenance.memory_scope"
        def extracted(path):
            return f"json_extract({metadata},'{path}')"
        def typed(path):
            return f"json_type({metadata},'{path}')"
        control_paths = ("$.archived_reason","$.delete_export_state","$.last_action",
            "$.operator_control.delete_export_state","$.operator_control.last_action")
        malformed = " OR ".join(f"({typed(path)} IS NOT NULL AND {typed(path)}!='text')" for path in control_paths)
        malformed += (f" OR ({typed('$.operator_control')} IS NOT NULL AND {typed('$.operator_control')}!='object')"
            f" OR json_valid(m.metadata_json)!=1 OR {typed('$')} IS NOT 'object'"
            f" OR {typed('$.work_board_provenance')} IS NOT 'object' OR {typed(scope)} IS NOT 'object'")
        suppressed = " OR ".join((
            f"lower(trim(COALESCE({extracted('$.archived_reason')},'')))='operator_delete_export'",
            *(f"lower(trim(COALESCE({extracted(path)},'')))='canonical_memory_redacted'"
                for path in ("$.delete_export_state","$.operator_control.delete_export_state")),
            *(f"lower(trim(COALESCE({extracted(path)},''))) IN ('propagate_delete_export','operator_delete_export')"
                for path in ("$.last_action","$.operator_control.last_action"))))
        visible = (await db.execute(text(f"""WITH pins(task_id,proposal_id,version,digest) AS (VALUES {','.join(values)})
          SELECT s.task_id,p.proposal_id,m.id memory_id,p.status proposal_state,m.status memory_state,
          {extracted('$.work_board_provenance.lifecycle_state')} lifecycle,
          ({malformed}) malformed,({suppressed}) suppressed,
          EXISTS(SELECT 1 FROM memory_tombstones z WHERE z.memory_id=m.id) tombstoned,
          (p.schema_version='task_method_proposal.v1' AND p.accepted_memory_id=s.version
           AND p.accepted_memory_content_digest=s.digest AND p.memory_kind='pattern' AND m.kind='pattern'
           AND p.owner_principal_id=t.owner_principal_id AND p.owner_session_id=t.owner_session_id
           AND p.goal_id=t.goal_id AND p.goal_revision=t.goal_revision
           AND m.source_session_id=t.owner_session_id
           AND g.owner_principal_id=t.owner_principal_id AND g.owner_session_id=t.owner_session_id
           AND g.revision=t.goal_revision
           AND {extracted(scope+'.owner.identity_id')}=:identity
           AND {extracted(scope+'.owner.issuer_root_id')}=t.owner_session_id
           AND {extracted(scope+'.owner.issuer_principal_id')}=t.owner_principal_id
           AND {extracted(scope+'.goal_id')}=t.goal_id AND {extracted(scope+'.goal_revision')}=t.goal_revision
           AND {extracted(scope+'.family')}='general'
           AND {extracted(scope+'.proposal_id')}=s.proposal_id
           AND {extracted(scope+'.candidate_version')}=s.version
           AND {extracted(scope+'.candidate_digest')}=s.digest) canonical,
          EXISTS(SELECT 1 FROM task_method_active a WHERE a.owner_identity_id=:identity
            AND a.goal_id=t.goal_id AND a.goal_revision=t.goal_revision AND a.family='general' AND a.baseline=0
            AND json_extract(CASE WHEN json_valid(a.binding_json) THEN a.binding_json ELSE '{{}}' END,'$.proposal_id')=s.proposal_id
            AND json_extract(CASE WHEN json_valid(a.binding_json) THEN a.binding_json ELSE '{{}}' END,'$.version')=s.version
            AND json_extract(CASE WHEN json_valid(a.binding_json) THEN a.binding_json ELSE '{{}}' END,'$.digest')=s.digest) pointer_matches
          FROM pins s JOIN work_board_tasks t ON t.task_id=s.task_id
          LEFT JOIN goals g ON g.id=t.goal_id
          LEFT JOIN memory_proposals p ON p.proposal_id=s.proposal_id
          LEFT JOIN memories m ON m.id=s.version"""),parameters)).mappings().all()
        visibility = {row["task_id"]:row for row in visible}
        for item in pins:
            row = visibility.get(item["task_id"])
            method = item["method"]
            if row is None or row["malformed"] or not row["canonical"]:
                continue
            if row["tombstoned"] or row["suppressed"]:
                method.update(lifecycle="suppressed_metadata",reason_code="method_suppressed",target=None)
                continue
            if row["lifecycle"]=='rolled_back' and row["proposal_state"]=='rolled_back' and row["memory_state"]=='archived':
                method.update(lifecycle="rolled_back_metadata",reason_code="method_rolled_back")
            elif row["lifecycle"]=='active' and row["proposal_state"]=='accepted' and row["memory_state"]=='active' and row["pointer_matches"]:
                method.update(lifecycle="active_metadata",reason_code=None)
            else:
                continue
            method["target"] = {"kind":"method","proposal_id":method["method_id"],
                "version":method["version"],"digest":method["digest"]}


home_projection = HomeProjection()
