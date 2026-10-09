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
 CASE WHEN typeof(g.title)='text' AND length(CAST(g.title AS BLOB)) BETWEEN 1 AND 512 THEN g.title ELSE NULL END title,
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
         expires="NULL", attempt="NULL", access="access", title="NULL", source_kind="NULL",
         snoozed="NULL", graph="NULL", availability="NULL", created="NULL"):
    # Arguments are fixed source-owned SQL expressions, never caller strings.
    return f"""SELECT '{section}' section,{kind} kind,{band} band,{value} value,
      COALESCE({due},{NULL_DUE}) due_us,{source} source_us,{key} key_hex,{identity} id,
      {revision} revision,{goal} goal_id,{goal_revision} goal_revision,{programme} programme_id,
      {grant} grant_revision,{state} state,{reason} reason,{priority} priority,{scheduled} scheduled_at,
      {expires} expires_at,{attempt} attempt_id,{access} access,{title} title,
      {source_kind} source_kind,{snoozed} snoozed_until,{graph} graph_header,
      {availability} source_availability,{created} created_us"""


TASK_US = _us("updated_at")
PROG_KEY = "printf('%04X',length(CAST(goal_id AS BLOB)))||hex(goal_id)||hex(id)||printf('%016X',grant_revision)"
GOALS = _row("active_goals", 0, 4, "sort_order", _us("due_date"), TASK_US, "hex(id)", "id", "revision",title="title") + " FROM readable_goals WHERE status='active'"
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
# Fixed metadata arrays are bounded before transfer; no full ORM/private body.
def _header(alias, columns):
    expressions=[alias+'.'+column for column in columns.split()]
    bad=[]
    for column, expression in zip(columns.split(),expressions):
        if column.endswith('revision') and column!='message_revision':
            valid=f"typeof({expression})='integer' AND {expression} BETWEEN 1 AND 9223372036854775807"
        else:
            maximum=128 if 'digest' in column else 64 if column in {'state','status'} else 512
            valid=f"typeof({expression})='text' AND length(CAST({expression} AS BLOB))<={maximum}"
        bad.append(f"({expression} IS NOT NULL AND NOT ({valid}))")
    return "json_array("+','.join(expressions)+")",' OR '.join(bad)


def _owner(alias):
    return f"({alias}.owner_principal_id=:principal AND {alias}.owner_session_id=:root)"


def _missing_or(alias, identifier, predicate):
    return f"({alias}.{identifier} IS NULL OR ({predicate}))"


DISPOSITION_HEADER="source_id watch_id source_digest goal_id goal_revision plan_revision state snoozed_until expires_at"
GOAL_HEADER="id owner_principal_id owner_session_id revision status updated_at"
SOURCE_HEADER=(('d',DISPOSITION_HEADER),
    ('p','id goal_id goal_revision source_watch_id plan_revision input_digest status updated_at'),
    ('w','id owner_principal_id owner_session_id goal_id goal_revision plan_revision state updated_at'),('ig',GOAL_HEADER))
MAIL_HEADER=(('d',DISPOSITION_HEADER),
    ('b','binding_id owner_principal_id owner_session_id goal_id goal_revision binding_revision state updated_at'),
    ('s','binding_id owner_principal_id owner_session_id goal_id goal_revision revision state consent_id connection_id connection_revision source_consent_revision updated_at'),
    ('c','consent_id owner_principal_id owner_session_id connection_id connection_revision goal_id goal_revision revision source_revision state expires_at updated_at'),
    ('x','connection_id owner_principal_id owner_session_id revision state updated_at'),
    ('m','message_binding_id owner_principal_id owner_session_id connection_id revision message_revision status updated_at'),('ig',GOAL_HEADER))
OPPORTUNITY_HEADER=(('d',DISPOSITION_HEADER),
    ('o','id owner_principal_id original_root_id goal_id goal_revision watch_id watch_revision revision status expires_at'),('ig',GOAL_HEADER))


def _inbox_stream(kind,joins,predicates,headers,present,source_created):
    arrays,bad=zip(*(_header(a,c) for a,c in headers))
    raw="json_array('home-inbox-graph.v1','"+kind+"',"+','.join(arrays)+")"
    graph=f"CASE WHEN ({' OR '.join(bad)}) THEN NULL ELSE CASE WHEN length(CAST({raw} AS BLOB))>16384 THEN NULL ELSE {raw} END END"
    title={'source_packet':'Watched source changed','mail_notice':'New message in watched mailbox',
        'guardian_opportunity':'Public evidence opportunity'}[kind]
    prefix="printf('%04X',length(CAST(d.id AS BLOB)))||hex(d.id)||printf('%016X',d.revision)"
    row=_row('task_next_actions',8,3,1,_us("CASE WHEN d.state='snoozed' AND d.snoozed_until IS NOT NULL THEN d.snoozed_until ELSE d.expires_at END"),
        _us('d.updated_at'),prefix,'d.id','d.revision','d.goal_id','d.goal_revision',state='d.state',
        expires='d.expires_at',access="'current'",title="'"+title+"'",source_kind="'"+kind+"'",snoozed='d.snoozed_until',
        graph=graph,availability=f"CASE WHEN {present} THEN 'present' ELSE 'unavailable' END",created=_us('d.created_at'))
    return row+f" FROM guardian_inbox_dispositions d {joins} WHERE {_owner('d')} AND d.source_kind='{kind}' AND d.state IN ('pending','snoozed') AND ({INBOX_SCALARS}) AND {_us('d.expires_at')}>:now AND ({source_created} IS NULL OR {_us(source_created)}<=:as_of) AND "+' AND '.join(predicates)


INBOX_SCALARS=f"""typeof(d.id)='text' AND length(CAST(d.id AS BLOB)) BETWEEN 1 AND 512
 AND typeof(d.goal_id)='text' AND length(CAST(d.goal_id AS BLOB)) BETWEEN 1 AND 512
 AND typeof(d.revision)='integer' AND d.revision BETWEEN 1 AND 9223372036854775807
 AND typeof(d.goal_revision)='integer' AND d.goal_revision BETWEEN 1 AND 9223372036854775807
 AND {_us('d.created_at')} IS NOT NULL AND {_us('d.updated_at')} IS NOT NULL AND {_us('d.expires_at')} IS NOT NULL
 AND (d.snoozed_until IS NULL OR {_us('d.snoozed_until')} IS NOT NULL)"""
SOURCE_INBOX=_inbox_stream('source_packet',"""LEFT JOIN guardian_decision_packets p ON p.id=d.source_id
 LEFT JOIN guardian_source_watches w ON w.id=p.source_watch_id LEFT JOIN goals ig ON ig.id=p.goal_id""",
 [_missing_or('p','id','p.goal_id=d.goal_id AND p.source_watch_id=d.watch_id'),
  _missing_or('w','id',_owner('w')+' AND w.id=d.watch_id'),_missing_or('ig','id',_owner('ig')+' AND ig.id=d.goal_id')],
 SOURCE_HEADER,'p.id IS NOT NULL AND w.id IS NOT NULL AND ig.id IS NOT NULL','p.created_at')
MAIL_INBOX=_inbox_stream('mail_notice',"""LEFT JOIN governed_schedule_bindings b ON b.binding_id=d.watch_id
 LEFT JOIN mail_watch_states s ON s.binding_id=d.watch_id LEFT JOIN goals ig ON ig.id=d.goal_id
 LEFT JOIN mail_read_consents c ON c.consent_id=s.consent_id LEFT JOIN google_service_connections x ON x.connection_id=s.connection_id
 LEFT JOIN mail_message_bindings m ON m.owner_principal_id=:principal AND m.owner_session_id=:root AND m.connection_id=x.connection_id
 AND substr(d.source_id,1,length('mail-notice:'||d.watch_id||':'))='mail-notice:'||d.watch_id||':'
 AND length(CAST(substr(d.source_id,length('mail-notice:'||d.watch_id||':')+1) AS BLOB)) BETWEEN 1 AND 128
 AND m.message_key=substr(d.source_id,length('mail-notice:'||d.watch_id||':')+1)""",
 [_missing_or('b','binding_id',_owner('b')+' AND b.goal_id=d.goal_id'),
  _missing_or('s','binding_id',_owner('s')+' AND s.goal_id=d.goal_id'),_missing_or('ig','id',_owner('ig')),
  _missing_or('c','consent_id',_owner('c')+' AND c.connection_id=s.connection_id'),
  _missing_or('x','connection_id',_owner('x'))],MAIL_HEADER,
 'b.binding_id IS NOT NULL AND s.binding_id IS NOT NULL AND ig.id IS NOT NULL AND c.consent_id IS NOT NULL AND x.connection_id IS NOT NULL AND m.message_binding_id IS NOT NULL','m.created_at')
OPPORTUNITY_INBOX=_inbox_stream('guardian_opportunity',"""JOIN guardian_opportunities o ON o.id=d.source_id
 LEFT JOIN goals ig ON ig.id=o.goal_id""",
 ["o.owner_principal_id=:principal AND o.original_root_id=:root AND o.status='proposed' AND o.goal_id=d.goal_id AND o.goal_revision=d.goal_revision AND o.watch_id=d.watch_id AND o.watch_revision=d.plan_revision",
  _missing_or('ig','id',_owner('ig'))],OPPORTUNITY_HEADER,'ig.id IS NOT NULL','o.created_at')
INBOX=SOURCE_INBOX+' UNION ALL '+MAIL_INBOX+' UNION ALL '+OPPORTUNITY_INBOX
STREAMS = (GOALS, PROGS, NEXT+' UNION ALL '+INBOX, OUTPUTS, APPROVALS, BLOCKED_TASKS + " UNION ALL " + BLOCKED_APPROVALS + " UNION ALL " + BLOCKED_PROGS)
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
            context = json.dumps({"projection_schema":"home-attention-goal.v2","timezone":settings.user_timezone,"generation":self._policy_generation,
                "epoch":policy.epoch if policy else None,"digest":policy.digest if policy else None,
                "blocked":policy.blocked_reason if policy else "provider_policy_unavailable"},
                sort_keys=True,separators=(",",":"))
            return policy,context

    def read_context(self):
        with self._state_lock:
            if not self.started or self._codec is None:
                raise HomeCursorError("continuation_unavailable")
            policy = self._policy
            context = json.dumps({"projection_schema":"home-attention-goal.v2","timezone":settings.user_timezone,"generation":self._policy_generation,
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
            inbox_bad=(await db.execute(text(f"SELECT EXISTS(SELECT 1 FROM guardian_inbox_dispositions d WHERE {_owner('d')} AND d.source_kind IN ('source_packet','mail_notice','guardian_opportunity') AND d.state IN ('pending','snoozed') AND NOT ({INBOX_SCALARS}) LIMIT 1)"),parameters)).scalar_one()
            if inbox_bad: invalid.add('task_next_actions')
            if sources["goals_bad"]:
                invalid.update(("active_goals","programme_status","blocked_items"))
            if sources["tasks_bad"]:
                invalid.update(("task_next_actions","prepared_outputs","blocked_items"))
            if sources["approvals_bad"]:
                invalid.update(("approvals","blocked_items"))
            for index, stream in enumerate(STREAMS):
                where = f"(CASE WHEN kind=8 THEN created_us ELSE source_us END)<=:as_of AND {'1' if last is None else AFTER}"
                if last:
                    parameters.update(last_band=last.band, last_value=last.value, last_due=last.due_us,
                        last_source=last.source_us, last_kind=last.kind, last_key=(last.key[:-32] if last.kind==8 else last.key).hex().upper())
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
                stream = STREAMS[{0:0,1:1,2:2,3:3,4:4,5:5,6:5,7:5,8:2}[last.kind]]
                anchor_rows=(await db.execute(text(BASE + f"SELECT * FROM ({stream}) WHERE band=:last_band AND value=:last_value AND due_us=:last_due AND source_us=:last_source AND kind=:last_kind AND key_hex=:last_key LIMIT 1"), parameters)).mappings().all()
                anchor=bool(anchor_rows)
                if last.kind==8 and anchor:
                    try: anchor=self.candidate(anchor_rows[0])[0]==last
                    except (ValueError,TypeError,OverflowError): anchor=False
                if not anchor:
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
            body["programme_status"]["state"] = "degraded" if body["programme_status"]["items"] else "blocked"
            if operator.operator_identity_id is not None:
                body["blocked_items"]["state"] = "degraded" if body["blocked_items"]["items"] else "blocked"
        if diagnostic["malformed"]:
            invalid.update(("programme_status", "blocked_items"))
        for name in invalid:
            # Projection diagnostics mean unknown source truth even with no
            # safe rows; independently confirmed prerequisites stay blocked.
            body[name]["state"] = "degraded"
        body["as_of"] = timestamp(as_of).isoformat()
        output = HomeContinuation.model_validate_json(json.dumps(body))
        next_cursor = None
        if len(candidates) > limit and selected:
            next_cursor = codec.encode(as_of=as_of, expires=expires, limit=limit,
                principal=operator.principal.principal_id, root=operator.session_id,
                identity=operator.operator_identity_id, context=context, position=selected[-1][0])
        return output, next_cursor

    def candidate(self, row):
        from src.operator.home_cursor import source_id, programme_key, inbox_key
        kind = row["kind"]
        if kind==8:
            graph=row['graph_header']
            if graph is None or len(graph.encode())>16384: raise ValueError('unsupported Inbox header')
            key=inbox_key(row['id'],row['revision'],hashlib.sha256(graph.encode()).digest())
        else:
            key = programme_key(row["goal_id"], row["programme_id"], row["grant_revision"]) if kind in {1,7} else source_id(row["id"], task=kind in {2,3,5})
        if (key[:-32] if kind==8 else key).hex().upper() != row["key_hex"] or type(row["value"]) is not int:
            raise ValueError()
        position = Position(row["band"], row["value"], row["due_us"], row["source_us"], kind, key)
        source = timestamp(position.source_us).isoformat()
        item = {"kind": KINDS[kind], "ownership_access": row["access"], "source_at": source}
        if kind == 0:
            title=row['title']
            if title is not None and (len(title)>256 or not title.strip() or any(ord(c)<32 or ord(c)==127 for c in title)): title=None
            item.update(goal_id=row["id"], goal_revision=row["revision"], status="active", sort_order=row["value"],title=title,
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
        elif kind==8:
            item.update(inbox_id=row['id'],inbox_revision=row['revision'],source_kind=row['source_kind'],state=row['state'],
                title=row['title'],source_availability=row['source_availability'],goal_id=row['goal_id'],goal_revision=row['goal_revision'],
                snoozed_until=self.utc(row['snoozed_until']),expires_at=self.utc(row['expires_at']),
                target={'kind':'inbox','inbox_id':row['id'],'inbox_revision':row['revision']})
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
            if item["ownership_access"] == "recovered_read_only":
                # Task-only recovery grants historical metadata, not the
                # original Goal scope required by the method inspector.
                continue
            method["target"] = {"kind":"method","proposal_id":method["method_id"],
                "version":method["version"],"digest":method["digest"]}


home_projection = HomeProjection()
