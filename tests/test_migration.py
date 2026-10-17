"""Migration of an existing SQLite database (v26 schema AND an older v9-style one) to PostgreSQL and back."""
import os, sqlite3, subprocess, sys, tempfile, pytest
from conftest import ROOT, _pg_url

def make_v26(path):
    # created with the ORIGINAL v26 app.py schema (copy of the original init_db DDL)
    c = sqlite3.connect(path)
    c.executescript(open(os.path.join(ROOT, 'database.py')).read().split("SQLITE_SCHEMA = '''")[1].split("'''")[0])
    c.execute("INSERT INTO subjects VALUES('0101','Softdev')")
    c.execute("INSERT INTO courses(id,subject_code,name,nfc_token,tag_uid,chapter,title,duration,position,state) VALUES('c1','0101','Softdev','STK-AAAAAAAA','04:AA:BB:CC','1','Intro',600,300,'IN_PROGRESS')")
    c.execute("INSERT INTO sessions(id,course_id,course_name,chapter,title,started,ended,start_position,end_position) VALUES(7,'c1','Softdev','1','Intro','2026-10-01 10:00:00','2026-10-01 10:05:00',0,300)")
    c.execute("INSERT INTO timeline_sections(id,course_id,session_id,subject_code,chapter,seq,study_date,started_at,start_position,end_position,duration,description,is_auto,created) VALUES(3,'c1',7,'0101','1',1,'2026-10-01','2026-10-01 10:00:00',0,300,300,'basics',0,'2026-10-01 10:05:00')")
    c.commit(); c.close()

def make_v9(path):
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE courses(id TEXT PRIMARY KEY,name TEXT NOT NULL,tag_uid TEXT UNIQUE,chapter TEXT,title TEXT,duration INTEGER NOT NULL DEFAULT 180,position INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'NOT_STARTED',last_seen TEXT);"
                    "CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,course_id TEXT NOT NULL,started TEXT NOT NULL,ended TEXT,start_position INTEGER NOT NULL DEFAULT 0,end_position INTEGER);"
                    "INSERT INTO courses(id,name,tag_uid,chapter,title,duration,position) VALUES('CS101','Old Course','04:01:02:03','1','t',180,10);"
                    "INSERT INTO sessions(course_id,started,ended,start_position,end_position) VALUES('CS101','2025-01-01 09:00:00','2025-01-01 09:03:00',0,10);")
    c.commit(); c.close()

def run(*a):
    return subprocess.run([sys.executable, 'migrate_db.py', *a], cwd=ROOT, capture_output=True, text=True, timeout=120)

@pytest.mark.parametrize('maker', [make_v26, make_v9])
def test_import_idempotent_and_roundtrip(maker):
    url = _pg_url()
    if not url: pytest.skip('no PostgreSQL')
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        for t in ('timeline_sections', 'sessions', 'courses', 'subjects'): c.execute(f'DROP TABLE IF EXISTS {t}')
    tmp = tempfile.mkdtemp(); src = os.path.join(tmp, 'old.db'); maker(src)
    before = open(src, 'rb').read()
    d = run('import', '--sqlite', src, '--database-url', url, '--dry-run'); assert d.returncode == 0, d.stderr
    with psycopg.connect(url) as c: assert c.execute("SELECT to_regclass('courses') IS NULL OR (SELECT COUNT(*) FROM courses)=0").fetchone()[0]   # dry run wrote nothing
    r = run('import', '--sqlite', src, '--database-url', url); assert r.returncode == 0, r.stdout + r.stderr
    assert 'verified' in r.stdout
    r2 = run('import', '--sqlite', src, '--database-url', url); assert r2.returncode == 0 and 'skipped' in r2.stdout   # re-run: nothing duplicated
    assert open(src, 'rb').read() == before                                                                       # source untouched
    with psycopg.connect(url) as c:
        row = c.execute("SELECT subject_code,nfc_token,tag_uid FROM courses").fetchone()
        assert row[2] in ('04:AA:BB:CC', '04:01:02:03') and row[1].startswith('STK-') and row[0]       # UID -> token -> course kept (v9 gets token + code)
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        c.execute("INSERT INTO sessions(course_id,started) VALUES('x','now')")                          # sequence continues after imported ids
    out = os.path.join(tmp, 'backup.db')
    e = run('export', '--sqlite', out, '--database-url', url); assert e.returncode == 0, e.stdout + e.stderr
    q = sqlite3.connect(out); assert q.execute('SELECT COUNT(*) FROM courses').fetchone()[0] == 1; q.close()
