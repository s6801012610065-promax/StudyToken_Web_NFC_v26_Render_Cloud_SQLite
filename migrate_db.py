#!/usr/bin/env python3
"""Move data between the old SQLite file and PostgreSQL (Render).

  python migrate_db.py import --sqlite data/study_token.db --database-url "postgresql://..."   # SQLite -> PostgreSQL
  python migrate_db.py export --sqlite backup.db           --database-url "postgresql://..."   # PostgreSQL -> SQLite (backup)
  add --dry-run to only report what would happen.

Safe by design: the source is never modified (SQLite is opened on a temporary COPY), rows that already exist
in the destination are skipped (never overwritten), ids are preserved, everything runs in ONE transaction
(all or nothing), and row counts are verified afterwards. Re-running is harmless.
"""
from __future__ import annotations
import argparse, os, shutil, sqlite3, sys, tempfile
import database

ORDER = ['subjects', 'courses', 'sessions', 'timeline_sections']
KEY = {'subjects': 'code', 'courses': 'id', 'sessions': 'id', 'timeline_sections': 'id'}


def _cols(conn, table):
    if conn.pg:
        return [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position", (table,)).fetchall()]
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]


def copy(src, dst, dry):
    report = {}
    for t in ORDER:
        scols = _cols(src, t); dcols = _cols(dst, t)
        cols = [c for c in dcols if c in scols]
        rows = src.execute(f'SELECT {",".join(cols)} FROM {t}').fetchall()
        before = dst.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        added = 0
        for r in rows:
            vals = tuple(r[c] for c in cols)
            if dry:
                if not dst.execute(f'SELECT 1 FROM {t} WHERE {KEY[t]}=?', (r[KEY[t]],)).fetchone(): added += 1
                continue
            cur = dst.execute(f'INSERT INTO {t}({",".join(cols)}) VALUES({",".join("?"*len(cols))}) ON CONFLICT DO NOTHING', vals)
            added += max(cur.rowcount, 0)
        report[t] = (len(rows), before, added)
    if dst.pg and not dry:
        for t in ('sessions', 'timeline_sections'):
            dst.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),1), (SELECT MAX(id) FROM {t}) IS NOT NULL)")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('direction', choices=['import', 'export'])
    ap.add_argument('--sqlite', required=True)
    ap.add_argument('--database-url', default=os.environ.get('DATABASE_URL', ''))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if not a.database_url:
        sys.exit('Give --database-url (Render > Database > External Database URL) or set DATABASE_URL')
    tmp = tempfile.mkdtemp()
    try:
        if a.direction == 'import':
            if not os.path.exists(a.sqlite): sys.exit(f'{a.sqlite} not found')
            work = os.path.join(tmp, 'src.db'); shutil.copy2(a.sqlite, work)       # never touch the original
            database.configure('', work); database.init_db()                       # upgrades old (v9..v25) schemas on the COPY
            src = database.connect()
            database.configure(a.database_url, None); database.init_db(); dst = database.connect()
        else:
            if os.path.exists(a.sqlite): sys.exit(f'{a.sqlite} already exists; choose a new backup file name')
            database.configure('', a.sqlite); database.init_db(); dst = database.connect()
            database.configure(a.database_url, None); database.init_db(); src = database.connect()
        rep = copy(src, dst, a.dry_run)
        if a.dry_run:
            dst.rollback()
        else:
            dst.commit()
        print(f"{'DRY RUN - ' if a.dry_run else ''}{a.direction} finished")
        print(f"{'table':20}{'source rows':>12}{'dest before':>13}{'added':>8}{'skipped(existing)':>19}")
        for t, (n, before, added) in rep.items():
            print(f'{t:20}{n:>12}{before:>13}{added:>8}{n-added:>19}')
        if not a.dry_run:
            for t, (n, before, added) in rep.items():
                after = dst.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                assert after == before + added, f'count mismatch in {t}'
            print('verified: destination counts = before + added')
        src.close(); dst.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
