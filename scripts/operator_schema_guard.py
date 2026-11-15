#!/usr/bin/env python3
"""Read-only managed launch check for the forward-only principal schema."""
import argparse
from pathlib import Path
import sqlite3

SUPPORTED_SCHEMA = 900


def _connect_read_only(database: Path) -> sqlite3.Connection:
    """Open the stopped workspace database without writing to it.

    A WAL database that was closed cleanly has no ``-wal``/``-shm`` files, and
    a ``mode=ro`` connection cannot recreate them (SQLITE_CANTOPEN). With no
    ``-wal`` file the main file holds every committed page, so reading it as
    ``immutable`` is exact. A present ``-wal`` file may hold unflushed pages:
    then the original error stands (fail closed).
    """
    uri = database.resolve().as_uri()
    try:
        db = sqlite3.connect(uri + '?mode=ro', uri=True)
        db.execute('PRAGMA user_version').fetchone()
        return db
    except sqlite3.OperationalError:
        if Path(str(database) + '-wal').exists():
            raise
        return sqlite3.connect(uri + '?mode=ro&immutable=1', uri=True)


def check(database: Path, runtime_schema: int = SUPPORTED_SCHEMA) -> None:
    if not database.exists():
        return
    db = _connect_read_only(database)
    try:
        minimum = db.execute('PRAGMA user_version').fetchone()[0]
        principal_columns = {row[1] for row in db.execute('PRAGMA table_info(operator_sessions)')}
        required = max(minimum, SUPPORTED_SCHEMA if 'principal_id' in principal_columns else 0)
        if required > runtime_schema:
            raise RuntimeError('workspace requires the principal-compatible runtime; stop services and restore a pre-migration backup before downgrade')
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--runtime-schema', type=int, default=SUPPORTED_SCHEMA)
    args = parser.parse_args()
    try:
        check(args.database, args.runtime_schema)
    except (RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f'Operator schema startup blocked: {exc}\n')
