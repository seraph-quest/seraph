#!/usr/bin/env python3
"""Host-local exact pairing reset. Run using backend Python with services stopped."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--extension-id',required=True)
    parser.add_argument('--reference',required=True)
    parser.add_argument('--expected-revision',required=True,type=int)
    parser.add_argument('--acknowledge-owner-reset',required=True,action='store_true')
    args=parser.parse_args()
    workspace=Path(args.workspace).resolve(strict=True)
    from config.settings import settings
    settings.workspace_dir=str(workspace)
    import sqlite3
    from src.workspace import canonical_workspace_database_path
    path=canonical_workspace_database_path(str(workspace))
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0]!=900:
            raise SystemExit("Compatible migrated workspace required; run the reviewed managed migration first")
    from src.extensions.pairing_reset import reset_pairing
    receipt=await reset_pairing(extension_id=args.extension_id,reference=args.reference,expected_revision=args.expected_revision)
    print(json.dumps(receipt))

if __name__=='__main__':
    asyncio.run(main())
