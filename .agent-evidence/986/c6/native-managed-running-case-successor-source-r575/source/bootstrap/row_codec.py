'Independent SQLite readback through the pinned original row serializers.'
import hashlib
import json
from pathlib import Path
import sqlite3


def read(context, database, identities):
    # Called only in the reviewed trusted controller role after pre-request
    # socket policy. An already imported different epoch is a hard failure.
    from src.db import models
    path = Path(models.__file__).resolve(strict=True)
    expected = Path(context['source_root']) / 'backend/src/db/models.py'
    if path != expected or hashlib.sha256(path.read_bytes()).hexdigest() != context['source_file_pins'][str(path)]:
        raise RuntimeError('original_model_serializer_epoch_changed')
    classes = [models.WorkflowRunState, models.WorkBoardTask, models.WorkBoardAttempt,
        models.WorkBoardInputArtifact, models.Goal, models.OperatorSession,
        models.RepoRepairProposal, models.ApprovalRequest, models.InferenceCostReservation,
        models.WorkBoardEvent]
    by_table = {model.__tablename__: model for model in classes}
    connection = sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    result = []
    seen = set()
    try:
        connection.execute('BEGIN')
        for identity in identities:
            model = by_table[identity['table']]
            columns = [column.key for column in model.__table__.primary_key.columns]
            if columns != identity['columns'] or len(columns) != len(identity['key']):
                raise AssertionError('original_primary_key_constructor_changed')
            unique = (identity['table'], tuple(identity['key']))
            if unique in seen:
                raise AssertionError('duplicate_original_readback_identity')
            seen.add(unique)
            query = 'SELECT * FROM "' + identity['table'] + '" WHERE ' + ' AND '.join('"' + name + '"=?' for name in columns)
            rows = connection.execute(query, identity['key']).fetchall()
            if len(rows) != 1:
                raise AssertionError('original_exact_row_missing')
            value = model.model_validate(dict(rows[0])).model_dump(mode='json')
            result.append({'table': identity['table'], 'raw': json.dumps(value, sort_keys=True, separators=(',', ':'))})
        return result
    finally:
        connection.close()
