"""Complete retained native Memory inventory on the original SQLite writer."""
from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, _connection_state

MEMORY_JOB_KIND = "runtime_service_memory_v1"
_INDEX = "ix_workflow_run_states_job_kind"


async def native_memory_universe(db):
    """Return bounded canonical identities, without status/owner/seal filters.

    This is an inventory read, not admission, ownership or a historical grant.
    Original activation/admission owners must call before their own CAS/effects.
    """
    await _connection_state(db)
    return await (await db.connection()).run_sync(native_memory_universe_on_connection)


def native_memory_universe_on_connection(connection):
    """Same exact census for synchronous snapshot owners; no body/Source grant."""
    def query(statement, parameters=()):
        if hasattr(connection, "exec_driver_sql"):
            return connection.exec_driver_sql(statement, parameters)
        return connection.execute(statement, parameters)
    rowid_table = list(query(
        "SELECT wr FROM pragma_table_list WHERE schema='main' AND name='workflow_run_states' LIMIT 2"))
    if len(rowid_table) != 1 or rowid_table[0][0] != 0:
        raise HeaderBoundsError("memory_universe_rowid_unavailable")
    indices = list(query(
        "SELECT CASE WHEN typeof(name)='text' THEN CASE WHEN octet_length(name)<=128 THEN name END END,"
        "\"unique\",partial FROM pragma_index_list('workflow_run_states') LIMIT 129"))
    if len(indices) > MAX_ROWS:
        raise HeaderBoundsError("memory_universe_index_unavailable")
    matching = [row for row in indices if row[0] == _INDEX]
    if len(matching) != 1 or tuple(matching[0][1:]) != (0, 0):
        raise HeaderBoundsError("memory_universe_index_unavailable")
    columns = list(query(
        "SELECT seqno,cid,CASE WHEN typeof(name)='text' THEN CASE WHEN octet_length(name)<=128 THEN name END END,"
        "CASE WHEN typeof(coll)='text' THEN CASE WHEN octet_length(coll)<=16 THEN coll END END,key "
        "FROM pragma_index_xinfo(:name) LIMIT 3", {"name": _INDEX}))
    if (len(columns) != 2 or columns[0][0] != 0 or columns[0][2:] != ("job_kind", "BINARY", 1)
            or tuple(columns[1]) != (1, -1, None, "BINARY", 0)):
        raise HeaderBoundsError("memory_universe_index_unavailable")
    rowids = [row[0] for row in query(
        "SELECT _rowid_ FROM workflow_run_states INDEXED BY ix_workflow_run_states_job_kind "
        "WHERE job_kind=:kind LIMIT 129", {"kind": MEMORY_JOB_KIND})]
    if len(rowids) > MAX_ROWS:
        raise HeaderBoundsError("memory_universe_bound")
    identities = []
    for rowid in sorted(rowids):
        if type(rowid) is not int:
            raise HeaderBoundsError("memory_universe_rowid_unavailable")
        rows = list(query(
            "SELECT typeof(run_identity),octet_length(run_identity),"
            "CASE WHEN typeof(run_identity)='text' THEN CASE WHEN octet_length(run_identity)<=512 "
            "THEN run_identity END END FROM workflow_run_states WHERE _rowid_=:rowid LIMIT 2",
            {"rowid": rowid}))
        if len(rows) != 1 or rows[0][0] != "text" or type(rows[0][2]) is not str or not rows[0][2]:
            raise HeaderBoundsError("memory_universe_identity_unavailable")
        identities.append(rows[0][2])
    if len(set(identities)) != len(identities):
        raise HeaderBoundsError("memory_universe_identity_unavailable")
    return tuple(sorted(identities))
