"""Database layer for StudyToken.

* No DATABASE_URL  -> SQLite file (exactly the original v26 behaviour, used locally).
* DATABASE_URL set -> PostgreSQL (Render). Uses a small connection pool.

app.py talks to both through the same tiny API:
    conn = connect();  conn.execute(sql_with_?_placeholders, params)
    conn.begin_write() conn.insert_returning_id() conn.commit() conn.close()
SQL written in app.py is portable; the few dialect differences go through helpers
(gmax, IntegrityError, begin_write, insert_returning_id).
"""
from __future__ import annotations
import os, re, sqlite3, uuid, threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PG_ADVISORY_WRITE_LOCK = 727001      # serialises "write transactions" on PostgreSQL
PG_ADVISORY_INIT_LOCK = 727002

IS_PG = False
DATABASE_URL = ''
DB_PATH: Path = BASE_DIR / 'data' / 'study_token.db'
_pool = None
_pool_lock = threading.Lock()

try:
    import psycopg
    from psycopg_pool import ConnectionPool
    IntegrityError = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)
except Exception:                                   # psycopg not installed (pure local SQLite use)
    psycopg = None
    ConnectionPool = None
    IntegrityError = (sqlite3.IntegrityError,)


# --------------------------------------------------------------------------- rows
class Row:
    """Row for PostgreSQL that behaves like sqlite3.Row: r['name'], r[0], dict(r), keys()."""
    __slots__ = ('_cols', '_idx', '_vals')

    def __init__(self, cols, idx, vals):
        self._cols, self._idx, self._vals = cols, idx, vals

    def __getitem__(self, k):
        if isinstance(k, str):
            return self._vals[self._idx[k]]
        return self._vals[k]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f'Row({dict(zip(self._cols, self._vals))})'


def _row_factory(cursor):
    cols = [d.name for d in (cursor.description or [])]
    idx = {c: i for i, c in enumerate(cols)}        # last duplicate wins, same as dict(sqlite3.Row)
    return lambda vals: Row(cols, idx, vals)


def _qmark_to_pyformat(sql: str) -> str:
    return sql.replace('%', '%%').replace('?', '%s')


# ------------------------------------------------------------------------ connection
class Conn:
    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql, params=()):
        if self.pg:
            cur = self.raw.cursor()
            cur.execute(_qmark_to_pyformat(sql), tuple(params))
            return cur
        return self.raw.execute(sql, params)

    def executescript(self, script):
        if self.pg:
            for stmt in [s.strip() for s in script.split(';') if s.strip()]:
                self.execute(stmt)
        else:
            self.raw.executescript(script)

    def begin_write(self):
        """Start a transaction that excludes other writers (SQLite BEGIN IMMEDIATE equivalent)."""
        if self.pg:
            self.execute('SELECT pg_advisory_xact_lock(%d)' % PG_ADVISORY_WRITE_LOCK)
        else:
            self.raw.execute('BEGIN IMMEDIATE')

    def insert_returning_id(self, sql, params=()):
        if self.pg:
            return self.execute(sql + ' RETURNING id', params).fetchone()[0]
        return self.execute(sql, params).lastrowid

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if self.pg:
            try:
                self.raw.rollback()                 # anything not committed is discarded (same as sqlite close())
            except Exception:
                pass
            _pool.putconn(self.raw)
        else:
            self.raw.close()


def gmax(a: str, b: str) -> str:
    """Two-argument max as an SQL fragment: MAX(a,b) on SQLite, GREATEST(a,b) on PostgreSQL."""
    return f'GREATEST({a},{b})' if IS_PG else f'MAX({a},{b})'


# --------------------------------------------------------------------- configuration
def _normalize_url(url: str) -> str:
    url = (url or '').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return url


def configure(database_url: str | None = None, sqlite_path: str | os.PathLike | None = None):
    """(Re)configure the backend. Called once at import from the environment; tests call it again."""
    global IS_PG, DATABASE_URL, DB_PATH, _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
        DATABASE_URL = _normalize_url(os.environ.get('DATABASE_URL', '') if database_url is None else database_url)
        IS_PG = bool(DATABASE_URL)
        DB_PATH = Path(sqlite_path or os.environ.get('STUDYTOKEN_DB') or (BASE_DIR / 'data' / 'study_token.db'))
        if IS_PG:
            if ConnectionPool is None:
                raise RuntimeError('DATABASE_URL is set but psycopg is not installed (pip install -r requirements.txt)')
            _pool = ConnectionPool(
                DATABASE_URL, min_size=1, max_size=int(os.environ.get('DB_POOL_MAX', '10')),
                kwargs={'row_factory': _row_factory, 'connect_timeout': 10,
                        'options': '-c statement_timeout=20000'},
                check=ConnectionPool.check_connection,   # transparently replaces connections dropped by a DB restart
                timeout=15, max_idle=300, open=True)
            _pool.wait(timeout=30)
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def connect() -> Conn:
    if IS_PG:
        return Conn(_pool.getconn(timeout=15), True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return Conn(c, False)


def describe() -> str:
    if IS_PG:
        m = re.match(r'^[a-z]+://[^@/]*@([^/:?]+)(?::(\d+))?/([^?]+)', DATABASE_URL)
        return f'PostgreSQL ({m.group(1)}/{m.group(3)})' if m else 'PostgreSQL'
    return f'SQLite ({DB_PATH})'


# ------------------------------------------------------------------------------ schema
SQLITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS subjects (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    subject_code TEXT,
    name TEXT NOT NULL,
    nfc_token TEXT UNIQUE,
    tag_uid TEXT UNIQUE,
    chapter TEXT,
    title TEXT,
    duration INTEGER NOT NULL DEFAULT 180,
    position INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'NOT_STARTED',
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    course_name TEXT,
    chapter TEXT,
    title TEXT,
    started TEXT NOT NULL,
    ended TEXT,
    start_position INTEGER NOT NULL DEFAULT 0,
    end_position INTEGER
);
CREATE TABLE IF NOT EXISTS timeline_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    session_id INTEGER,
    subject_code TEXT,
    chapter TEXT,
    seq INTEGER NOT NULL,
    study_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    start_position INTEGER NOT NULL,
    end_position INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    description TEXT NOT NULL,
    is_auto INTEGER NOT NULL DEFAULT 0,
    client_id TEXT,
    created TEXT NOT NULL,
    UNIQUE(course_id, seq),
    UNIQUE(session_id, client_id),
    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_timeline_course ON timeline_sections(course_id, seq);
'''

PG_SCHEMA = '''
CREATE TABLE IF NOT EXISTS subjects (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    subject_code TEXT,
    name TEXT NOT NULL,
    nfc_token TEXT UNIQUE,
    tag_uid TEXT UNIQUE,
    chapter TEXT,
    title TEXT,
    duration INTEGER NOT NULL DEFAULT 180,
    position INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'NOT_STARTED',
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    course_id TEXT NOT NULL,
    course_name TEXT,
    chapter TEXT,
    title TEXT,
    started TEXT NOT NULL,
    ended TEXT,
    start_position INTEGER NOT NULL DEFAULT 0,
    end_position INTEGER,
    segment_started TEXT
);
CREATE TABLE IF NOT EXISTS timeline_sections (
    id SERIAL PRIMARY KEY,
    course_id TEXT NOT NULL,
    session_id INTEGER,
    subject_code TEXT,
    chapter TEXT,
    seq INTEGER NOT NULL,
    study_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    start_position INTEGER NOT NULL,
    end_position INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    description TEXT NOT NULL,
    is_auto INTEGER NOT NULL DEFAULT 0,
    client_id TEXT,
    created TEXT NOT NULL,
    UNIQUE(course_id, seq),
    UNIQUE(session_id, client_id),
    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_timeline_course ON timeline_sections(course_id, seq)
'''

TABLES = ('subjects', 'courses', 'sessions', 'timeline_sections')


def _sqlite_cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def init_db():
    """Create tables and migrate older data. Safe to run on every start."""
    conn = connect()
    try:
        if IS_PG:
            conn.execute('SELECT pg_advisory_xact_lock(%d)' % PG_ADVISORY_INIT_LOCK)
            conn.executescript(PG_SCHEMA)
        else:
            conn.executescript(SQLITE_SCHEMA)
            # Older SQLite databases (v9..v25): add the columns that did not exist yet.
            cols = _sqlite_cols(conn, 'courses')
            for col, typ in [('subject_code', 'TEXT'), ('nfc_token', 'TEXT')]:
                if col not in cols:
                    conn.execute(f'ALTER TABLE courses ADD COLUMN {col} {typ}')
            scols = _sqlite_cols(conn, 'sessions')
            for col, typ in [('course_name', 'TEXT'), ('chapter', 'TEXT'), ('title', 'TEXT'), ('segment_started', 'TEXT')]:
                if col not in scols:
                    conn.execute(f'ALTER TABLE sessions ADD COLUMN {col} {typ}')
        # Existing data: keep UID pairing, give every course a token, use old Course ID as subject code.
        for r in conn.execute('SELECT id,name,subject_code,nfc_token FROM courses').fetchall():
            code = (r['subject_code'] or r['id']).strip()
            token = r['nfc_token'] or ('STK-' + uuid.uuid4().hex[:8].upper())
            conn.execute('UPDATE courses SET subject_code=?, nfc_token=? WHERE id=?', (code, token, r['id']))
            conn.execute('INSERT INTO subjects(code,name) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name',
                         (code, r['name']))
        conn.execute('''UPDATE sessions SET course_name=(SELECT name FROM courses c WHERE c.id=sessions.course_id), chapter=(SELECT chapter FROM courses c WHERE c.id=sessions.course_id), title=(SELECT title FROM courses c WHERE c.id=sessions.course_id) WHERE course_name IS NULL''')
        conn.commit()
    finally:
        conn.close()


configure()
