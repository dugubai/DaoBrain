"""Consistent SQLite backup/restore to a NEW file only; never replace source.

Run inside the accepted backend/container or offline on an authorized copy.
Restore invalidates old sessions. This is single-host SQLite, not a cluster DB.
"""
import argparse
import os
from pathlib import Path
import sqlite3


def transfer(source, destination, restore=False):
    src, dst = Path(source), Path(destination)
    if not src.is_absolute() or not dst.is_absolute() or not src.is_file() or src.is_symlink():
        raise ValueError("Absolute regular source required")
    if not dst.parent.is_dir() or dst.exists() or dst.is_symlink():
        raise ValueError("Destination must be a new file in an existing directory")
    # Stage first: no final restored path can expose still-valid old sessions.
    # Retain the staging alias instead of deleting any file. Atomic link refuses
    # an existing destination; both names refer to the already verified copy.
    from uuid import uuid4
    staging = dst.parent / (".business-staging-" + uuid4().hex + ".sqlite3")
    fd = os.open(str(staging), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    incoming = outgoing = None
    try:
        incoming = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        outgoing = sqlite3.connect(str(staging))
        if incoming.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source integrity check failed")
        if incoming.execute("SELECT version FROM schema_version").fetchall() != [(1,)]:
            raise ValueError("Unsupported schema")
        incoming.backup(outgoing)
        # SQLite 3.46 read-only connections can omit CHECK failures. Verify the
        # writable staged copy before restoration can change any copied rows.
        if outgoing.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Destination integrity check failed")
        if restore:
            outgoing.execute("UPDATE sessions SET expires=0")
            outgoing.commit()
        if outgoing.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Destination integrity check failed")
    finally:
        try:
            if outgoing is not None:
                outgoing.close()
        finally:
            if incoming is not None:
                incoming.close()
    os.link(str(staging), str(dst))
    return {"verified": True, "restored_copy": restore, "source_preserved": True,
            "sessions_invalidated": restore, "staging_alias_retained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    print(transfer(args.source, args.destination, args.restore))


if __name__ == "__main__":
    main()
