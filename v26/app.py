from __future__ import annotations
import os, sqlite3, uuid, threading, re, socket, webbrowser, time as _time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, render_template, request

CLOUD_MODE = os.environ.get('STUDYTOKEN_CLOUD_MODE', '').strip().lower() in {'1', 'true', 'yes'} or bool(os.environ.get('RENDER'))

try:
    if CLOUD_MODE:
        raise ImportError('NFC reader is disabled in cloud mode; physical PN532/COM ports are not available on Render.')
    from nfc_reader import start_nfc_reader, get_state as get_nfc_state, start_pairing, cancel_pairing, request_nfc_url_write
except Exception:
    start_nfc_reader = None
    request_nfc_url_write = lambda uid, target_url, wait_seconds=8.0: {"available": False, "write_status": "NFC_UNAVAILABLE", "write_error": "nfc_reader.py unavailable"}
    start_pairing = lambda: None
    cancel_pairing = lambda: None
    get_nfc_state = lambda: {"available": False, "status": "DISABLED", "port": None, "last_uid": None, "last_seen": None, "last_error": "nfc_reader.py unavailable", "event_id": 0}

BASE_DIR=Path(__file__).resolve().parent
# On Render, use the persistent disk mounted at /var/data. Locally, keep the
# original database under ./data. Set STUDYTOKEN_DB to override either path.
DEFAULT_DB_PATH = Path('/var/data/study_token.db') if os.environ.get('RENDER') else BASE_DIR/'data'/'study_token.db'
DB_PATH=Path(os.environ.get('STUDYTOKEN_DB', DEFAULT_DB_PATH))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
app=Flask(__name__)
app.config['JSON_SORT_KEYS']=False

@app.context_processor
def inject_deployment_config():
    return {'cloud_mode': CLOUD_MODE}

def now_str(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def normalize_uid(value):
    text=str(value or '').strip().upper().replace(' ','').replace('-',':')
    if ':' not in text and len(text)%2==0 and re.fullmatch(r'[0-9A-F]+',text or ''):
        text=':'.join(text[i:i+2] for i in range(0,len(text),2))
    return text

def normalize_number(value):
    s=str(value or '').strip().upper().replace(' ','')
    thai=str.maketrans('๐๑๒๓๔๕๖๗๘๙','0123456789')
    s=s.translate(thai)
    if s.isdigit(): return str(int(s))
    if not s or not re.fullmatch(r'[IVXLCDM]+',s): return None
    vals={'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    total=0; prev=0
    for ch in reversed(s):
        v=vals[ch]
        if v<prev: total-=v
        else: total+=v; prev=v
    return str(total)

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def table_cols(conn, table): return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}

def init_db():
    conn=db()
    conn.executescript('''
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
    ''')
    cols=table_cols(conn,'courses')
    migrations=[('subject_code','TEXT'),('nfc_token','TEXT')]
    for col,typ in migrations:
        if col not in cols: conn.execute(f'ALTER TABLE courses ADD COLUMN {col} {typ}')
    # Existing v9 data: preserve UID pairing, give every course a token and use old Course ID as subject code.
    cols=table_cols(conn,'courses')
    for r in conn.execute('SELECT id,name,subject_code,nfc_token FROM courses').fetchall():
        code=(r['subject_code'] or r['id']).strip()
        token=r['nfc_token'] or ('STK-'+uuid.uuid4().hex[:8].upper())
        conn.execute('UPDATE courses SET subject_code=?, nfc_token=? WHERE id=?',(code,token,r['id']))
        conn.execute('INSERT INTO subjects(code,name) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name',(code,r['name']))
    # Sessions already support snapshots in v9; add missing columns for older databases.
    scols=table_cols(conn,'sessions')
    for col,typ in [('course_name','TEXT'),('chapter','TEXT'),('title','TEXT'),('segment_started','TEXT')]:
        if col not in scols: conn.execute(f'ALTER TABLE sessions ADD COLUMN {col} {typ}')
    conn.execute('''UPDATE sessions SET course_name=(SELECT name FROM courses c WHERE c.id=sessions.course_id), chapter=(SELECT chapter FROM courses c WHERE c.id=sessions.course_id), title=(SELECT title FROM courses c WHERE c.id=sessions.course_id) WHERE course_name IS NULL''')
    conn.commit(); conn.close()

def row_dict(r): return dict(r) if r else None

# ---- Chapter Section Timeline helpers -------------------------------------
SECTION_MAX_LEN=200
# Finish Study never asks for a note. If there is unsaved study time left in the
# last segment, it is stored as one automatic section (flagged is_auto) so the
# timeline durations add up. Set to False to keep that time in History only.
AUTO_SECTION_ON_FINISH=True
AUTO_SECTION_TEXT='Study session finished (no progress note)'

def course_sections(conn, course_id):
    return [row_dict(r) for r in conn.execute('SELECT * FROM timeline_sections WHERE course_id=? ORDER BY seq',(course_id,)).fetchall()]

def segment_start_for(conn, s):
    """Position (seconds) where the currently open segment of session s begins."""
    r=conn.execute('SELECT MAX(end_position) FROM timeline_sections WHERE session_id=?',(s['id'],)).fetchone()
    return max(int(s['start_position'] or 0), int(r[0] or 0))

def insert_section(conn, s, c, start_pos, end_pos, description, is_auto=0, client_id=None):
    started_at=s['segment_started'] or s['started']
    seq=conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM timeline_sections WHERE course_id=?',(c['id'],)).fetchone()[0]
    cur=conn.execute('''INSERT INTO timeline_sections(course_id,session_id,subject_code,chapter,seq,study_date,started_at,start_position,end_position,duration,description,is_auto,client_id,created)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(c['id'],s['id'],c['subject_code'],c['chapter'],seq,started_at[:10],started_at,start_pos,end_pos,end_pos-start_pos,description,1 if is_auto else 0,client_id,now_str()))
    return conn.execute('SELECT * FROM timeline_sections WHERE id=?',(cur.lastrowid,)).fetchone()
def progress(c): return min(100,int(int(c['position'])*100/max(1,int(c['duration']))))

def get_nfc_base_url(port=None):
    configured=(os.environ.get('BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
    if configured: return configured
    port=port or int(os.environ.get('PORT','5000'))
    ip='127.0.0.1'; sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try: sock.connect(('8.8.8.8',80)); ip=sock.getsockname()[0] or ip
    except OSError: pass
    finally: sock.close()
    return f'http://{ip}:{port}'

def get_course_by_token(token):
    conn=db(); r=conn.execute('SELECT * FROM courses WHERE upper(nfc_token)=upper(?)',(str(token).strip(),)).fetchone(); conn.close(); return r

def get_course_by_uid(uid):
    uid=normalize_uid(uid); conn=db(); r=conn.execute('SELECT * FROM courses WHERE upper(tag_uid)=upper(?)',(uid,)).fetchone(); conn.close(); return r

def next_chapter(code):
    conn=db(); rows=conn.execute('SELECT chapter FROM courses WHERE subject_code=? ORDER BY id',(code,)).fetchall(); conn.close()
    nums=[int(x) for r in rows if (x:=normalize_number(r['chapter'])) is not None]
    return str(max(nums)+1 if nums else 1)

@app.get('/')
def index(): return render_template('index.html')
@app.get('/history')
def history_page(): return render_template('history.html')
@app.get('/desktop')
def desktop_page(): return render_template('desktop.html',nfc=get_nfc_state())

@app.get('/nfc/<token>')
def nfc_open(token):
    course=get_course_by_token(token)
    if not course:
        return render_template('nfc_unknown.html',token=token),404
    return render_template('study.html',course=row_dict(course),opened_from_nfc=True,source=request.args.get('source','mobile'))

@app.post('/api/nfc/token/new')
def api_new_nfc_token():
    """Generate a preview StudyToken. It is finalized only when Course is saved."""
    for _ in range(10):
        token='STK-'+uuid.uuid4().hex[:8].upper()
        conn=db()
        exists=conn.execute('SELECT 1 FROM courses WHERE upper(nfc_token)=upper(?)',(token,)).fetchone()
        conn.close()
        if not exists:
            return jsonify({'ok':True,'nfc_token':token})
    return jsonify({'error':'could not generate unique NFC token'}),500

@app.get('/api/courses')
def api_courses():
    conn=db(); rows=conn.execute('SELECT * FROM courses ORDER BY subject_code, CAST(CASE WHEN chapter GLOB "[0-9]*" THEN chapter ELSE "0" END AS INTEGER), name').fetchall()
    data=[]
    for r in rows:
        x=row_dict(r); x['progress']=progress(x)
        stats=conn.execute('SELECT COUNT(*) AS cnt, COALESCE(SUM(MAX(0,COALESCE(end_position,start_position)-start_position)),0) AS secs FROM sessions WHERE course_id=? AND ended IS NOT NULL',(x['id'],)).fetchone()
        x['sessions_count']=stats['cnt']; x['studied_seconds']=stats['secs']
        x['sections_count']=conn.execute('SELECT COUNT(*) FROM timeline_sections WHERE course_id=?',(x['id'],)).fetchone()[0]
        data.append(x)
    conn.close()
    return jsonify(data)

@app.get('/api/courses/<course_id>/status')
def api_course_status(course_id):
    """Store status of course: how many times studied and total minutes watched."""
    conn=db(); c=conn.execute('SELECT * FROM courses WHERE id=?',(course_id,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'course not found'}),404
    stats=conn.execute('SELECT COUNT(*) AS cnt, COALESCE(SUM(MAX(0,COALESCE(end_position,start_position)-start_position)),0) AS secs FROM sessions WHERE course_id=? AND ended IS NOT NULL',(course_id,)).fetchone()
    sessions=conn.execute('SELECT started,ended,start_position,end_position FROM sessions WHERE course_id=? ORDER BY started DESC',(course_id,)).fetchall()
    conn.close()
    x=row_dict(c); x['progress']=progress(x)
    return jsonify({'course':x,'sessions_count':stats['cnt'],'studied_seconds':stats['secs'],'sessions':[row_dict(s) for s in sessions]})

@app.put('/api/courses/<course_id>')
def api_edit_course(course_id):
    """Edit NFC data: course name / chapter / title / duration linked to a StudyToken."""
    p=request.get_json(force=True)
    conn=db(); c=conn.execute('SELECT * FROM courses WHERE id=?',(course_id,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'course not found'}),404

    name=str(p.get('name',c['name'])).strip() or c['name']
    chapter=normalize_number(p.get('chapter',c['chapter']))
    if not chapter:
        conn.close(); return jsonify({'error':'chapter must be a number such as 1, ๑, II or IV'}),400
    title=str(p.get('title',c['title'] or '')).strip()

    duration=c['duration']
    if str(p.get('duration','')).strip():
        try:
            minutes=int(str(p.get('duration')).strip()); assert minutes>0
            duration=minutes*60
        except Exception:
            conn.close(); return jsonify({'error':'time must be a positive number of minutes'}),400

    conn.execute('UPDATE courses SET name=?,chapter=?,title=?,duration=? WHERE id=?',(name,chapter,title,duration,course_id))
    conn.execute('UPDATE subjects SET name=? WHERE code=?',(name,c['subject_code']))
    conn.execute('UPDATE timeline_sections SET chapter=? WHERE course_id=?',(chapter,course_id))
    conn.commit()
    updated=conn.execute('SELECT * FROM courses WHERE id=?',(course_id,)).fetchone()
    conn.close()
    x=row_dict(updated); x['progress']=progress(x)
    return jsonify({'ok':True,'course':x})

@app.get('/api/subjects/<code>')
def api_subject(code):
    conn=db(); r=conn.execute('SELECT code,name FROM subjects WHERE upper(code)=upper(?)',(str(code).strip(),)).fetchone(); conn.close()
    if not r: return jsonify({'found':False,'code':str(code).strip()}),404
    return jsonify({'found':True,'code':r['code'],'name':r['name'],'next_chapter':next_chapter(r['code'])})

@app.get('/api/history')
def api_history():
    conn=db(); rows=conn.execute('''SELECT s.*, COALESCE(s.course_name,c.name) AS name, COALESCE(s.chapter,c.chapter) AS chapter, COALESCE(s.title,c.title) AS title FROM sessions s LEFT JOIN courses c ON c.id=s.course_id WHERE s.ended IS NOT NULL ORDER BY s.started DESC''').fetchall(); conn.close(); return jsonify([row_dict(r) for r in rows])
@app.get('/api/today')
def api_today():
    day=datetime.now().strftime('%Y-%m-%d'); conn=db(); rows=conn.execute('SELECT start_position,COALESCE(end_position,start_position) AS end_position FROM sessions WHERE substr(started,1,10)=?',(day,)).fetchall(); conn.close(); return jsonify({'seconds':sum(max(0,int(r[1] or 0)-int(r[0] or 0)) for r in rows)})

@app.post('/api/courses')
def api_add_course():
    """Create a course/chapter first. NFC UID is linked in a second step."""
    p=request.get_json(force=True)
    code=str(p.get('subject_code',p.get('id',''))).strip()
    name=str(p.get('name','')).strip()
    if not code:
        return jsonify({'error':'course code is required'}),400

    conn=db()
    existing=conn.execute('SELECT * FROM subjects WHERE upper(code)=upper(?)',(code,)).fetchone()
    if existing:
        name=existing['name']
    elif not name:
        conn.close(); return jsonify({'error':'course name is required for a new course code'}),400

    chapter=normalize_number(p.get('chapter',''))
    if not chapter:
        conn.close(); return jsonify({'error':'chapter must be a number such as 1, ๑, II or IV'}),400
    title=str(p.get('title','')).strip()
    try:
        minutes=int(str(p.get('duration','')).strip())
        assert minutes>0
    except Exception:
        conn.close(); return jsonify({'error':'time must be a positive number of minutes'}),400

    token=str(p.get('nfc_token','')).strip().upper()
    if not re.fullmatch(r'STK-[0-9A-F]{8}',token):
        conn.close(); return jsonify({'error':'generate a valid NFC Token before saving'}),400

    # UID is optional here. If the client already scanned a physical NFC tag
    # before Save (Scan NFC button, same principle as v9's Add Course), it is
    # linked and the URL is written to it automatically in this same request.
    # Otherwise the course is saved first and the tag is scanned/linked
    # afterwards via /api/nfc/link (older two-step flow), unchanged.
    uid_raw=p.get('nfc_uid','')
    uid=normalize_uid(uid_raw) if str(uid_raw).strip() else ''

    if conn.execute('SELECT 1 FROM courses WHERE upper(nfc_token)=upper(?)',(token,)).fetchone():
        conn.close(); return jsonify({'error':'NFC Token already exists. Generate a new token.'}),409

    if uid:
        other=conn.execute(
            'SELECT id,nfc_token,subject_code,name,chapter FROM courses WHERE upper(tag_uid)=upper(?)',
            (uid,)
        ).fetchone()
        if other:
            conn.close()
            return jsonify({'error':f'This NFC UID is already linked to Token {other["nfc_token"]} ({other["subject_code"]} Chapter {other["chapter"]})'}),409

    course_id=uuid.uuid4().hex
    try:
        conn.execute(
            'INSERT INTO subjects(code,name) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name',
            (code,name)
        )
        conn.execute(
            'INSERT INTO courses(id,subject_code,name,nfc_token,tag_uid,chapter,title,duration) VALUES(?,?,?,?,?,?,?,?)',
            (course_id,code,name,token,uid or None,chapter,title,minutes*60)
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback(); conn.close(); return jsonify({'error':str(e)}),409
    conn.close()

    nfc_url=f'{get_nfc_base_url()}/nfc/{token}'
    write_status=None
    write_error=None
    if uid:
        # Same principle as v9's api_add_course: write the URL to the
        # physical tag automatically, right after the course is saved.
        write_status='NFC_UNAVAILABLE'
        state=get_nfc_state()
        if state.get('available') and request_nfc_url_write:
            result=request_nfc_url_write(uid,nfc_url,wait_seconds=8.0)
            write_status=result.get('write_status','WAITING_FOR_TAG')
            write_error=result.get('write_error')

    return jsonify({
        'ok':True,
        'course_id':course_id,
        'subject_code':code,
        'name':name,
        'chapter':chapter,
        'nfc_token':token,
        'tag_uid':uid or None,
        'nfc_url':nfc_url,
        'nfc_write_status':write_status,
        'nfc_write_error':write_error,
        'message':('Course saved and URL written to the scanned NFC tag.' if uid else ('Course saved. Copy nfc_url and write it to an NFC tag using a compatible NFC-writing device.' if CLOUD_MODE else 'Course saved. Now scan an NFC Tag to link its UID to this NFC Token.'))
    })


@app.post('/api/nfc/link')
def api_nfc_link():
    """Link a scanned physical NFC UID to an already-saved StudyToken course."""
    p=request.get_json(force=True)
    token=str(p.get('nfc_token','')).strip().upper()
    uid=normalize_uid(p.get('nfc_uid',p.get('uid','')))
    if not re.fullmatch(r'STK-[0-9A-F]{8}',token):
        return jsonify({'error':'invalid NFC Token'}),400
    if not uid:
        return jsonify({'error':'NFC UID is required'}),400

    conn=db()
    course=conn.execute('SELECT * FROM courses WHERE upper(nfc_token)=upper(?)',(token,)).fetchone()
    if not course:
        conn.close(); return jsonify({'error':'NFC Token not found'}),404

    other=conn.execute(
        'SELECT id,nfc_token,subject_code,name,chapter FROM courses WHERE upper(tag_uid)=upper(?) AND id<>?',
        (uid,course['id'])
    ).fetchone()
    if other:
        conn.close()
        return jsonify({'error':f'This NFC UID is already linked to Token {other["nfc_token"]} ({other["subject_code"]} Chapter {other["chapter"]})'}),409

    conn.execute('UPDATE courses SET tag_uid=?,last_seen=? WHERE id=?',(uid,now_str(),course['id']))
    conn.commit()
    updated=conn.execute('SELECT * FROM courses WHERE id=?',(course['id'],)).fetchone()
    conn.close()

    url=f'{get_nfc_base_url()}/nfc/{token}'
    write_status='NFC_UNAVAILABLE'
    write_error=None
    state=get_nfc_state()
    if state.get('available') and request_nfc_url_write:
        result=request_nfc_url_write(uid,url,wait_seconds=4.0)
        write_status=result.get('write_status','WAITING_FOR_TAG')
        write_error=result.get('write_error')

    return jsonify({
        'ok':True,
        'nfc_uid':uid,
        'nfc_token':token,
        'course':row_dict(updated),
        'nfc_url':url,
        'nfc_write_status':write_status,
        'nfc_write_error':write_error
    })

@app.delete('/api/courses/<course_id>')
def api_delete_course(course_id):
    conn=db(); cur=conn.execute('DELETE FROM courses WHERE id=?',(course_id,)); conn.commit(); conn.close(); return jsonify({'ok':cur.rowcount>0})

@app.post('/api/sessions/start')
def api_start_session():
    p=request.get_json(force=True); cid=p.get('course_id'); conn=db(); c=conn.execute('SELECT * FROM courses WHERE id=?',(cid,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'course not found'}),404
    stamp=now_str(); cur=conn.execute('INSERT INTO sessions(course_id,course_name,chapter,title,started,start_position,end_position,segment_started) VALUES(?,?,?,?,?,?,?,?)',(c['id'],c['name'],c['chapter'],c['title'],stamp,c['position'],c['position'],stamp))
    conn.execute("UPDATE courses SET state='STUDYING',last_seen=? WHERE id=?",(stamp,cid)); conn.commit(); sid=cur.lastrowid; conn.close(); return jsonify({'session_id':sid,'course':row_dict(c)})

@app.post('/api/sessions/<int:sid>/autosave')
def api_autosave(sid):
    p=request.get_json(force=True)
    try: pos=max(0,int(p.get('position',0)))
    except: return jsonify({'error':'invalid position'}),400
    stamp=now_str(); conn=db(); s=conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'session not found'}),404
    if s['ended']:  # late autosave after Finish must not reopen the course
        conn.close(); return jsonify({'ok':True,'ignored':True,'position':int(s['end_position'] or 0),'completed':False})
    dr=conn.execute('SELECT duration FROM courses WHERE id=?',(s['course_id'],)).fetchone(); duration=int(dr[0]) if dr else max(pos,1); pos=min(pos,duration)
    # MAX(): progress only moves forward, so a delayed autosave can never roll back a saved section.
    conn.execute('UPDATE sessions SET end_position=MAX(COALESCE(end_position,0),?) WHERE id=?',(pos,sid))
    if s['segment_started']:  # segment running
        conn.execute("UPDATE courses SET position=MAX(position,?),state='STUDYING',last_seen=? WHERE id=?",(pos,stamp,s['course_id']))
    else:                     # paused after Save: keep the paused state
        conn.execute("UPDATE courses SET position=MAX(position,?),last_seen=? WHERE id=?",(pos,stamp,s['course_id']))
    conn.commit(); conn.close(); return jsonify({'ok':True,'position':pos,'completed':pos>=duration})

@app.post('/api/sessions/<int:sid>/sections')
def api_save_section(sid):
    """Save: close the running segment and store it as a new timeline section."""
    p=request.get_json(force=True,silent=True) or {}
    desc=' '.join(str(p.get('description','')).split())
    if not desc: return jsonify({'error':'Please describe what you have studied.','code':'NO_DESCRIPTION'}),400
    if len(desc)>SECTION_MAX_LEN: return jsonify({'error':f'Description is too long (max {SECTION_MAX_LEN} characters).','code':'TOO_LONG'}),400
    try: pos=int(p.get('position'))
    except Exception: return jsonify({'error':'invalid position'}),400
    client_id=str(p.get('client_id','') or '').strip()[:64] or None
    conn=db(); conn.execute('BEGIN IMMEDIATE')   # serialise concurrent saves: seq + segment start stay consistent
    s=conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'session not found'}),404
    if client_id:  # same dialog confirmed twice (double click / retry) -> return the section already stored
        dup=conn.execute('SELECT * FROM timeline_sections WHERE session_id=? AND client_id=?',(sid,client_id)).fetchone()
        if dup:
            out={'ok':True,'duplicate':True,'section':row_dict(dup),'sections':course_sections(conn,dup['course_id']),'position':int(dup['end_position']),'completed':False}
            conn.close(); return jsonify(out)
    if s['ended']: conn.close(); return jsonify({'error':'This study session is already finished.','code':'SESSION_FINISHED'}),409
    c=conn.execute('SELECT * FROM courses WHERE id=?',(s['course_id'],)).fetchone()
    if not c: conn.close(); return jsonify({'error':'course not found'}),404
    pos=min(max(0,pos),int(c['duration']))
    seg_start=segment_start_for(conn,s)
    if pos<=seg_start:
        conn.close(); return jsonify({'error':'No new study time since the last saved section.','code':'EMPTY_SEGMENT'}),409
    row=insert_section(conn,s,c,seg_start,pos,desc,0,client_id)
    stamp=now_str()
    conn.execute('UPDATE sessions SET end_position=MAX(COALESCE(end_position,0),?),segment_started=NULL WHERE id=?',(pos,sid))
    conn.execute("UPDATE courses SET position=MAX(position,?),state='IN_PROGRESS',last_seen=? WHERE id=?",(pos,stamp,c['id']))
    out={'ok':True,'duplicate':False,'section':row_dict(row),'sections':course_sections(conn,c['id']),'position':pos,'completed':pos>=int(c['duration'])}
    conn.commit(); conn.close(); return jsonify(out)

@app.post('/api/sessions/<int:sid>/resume')
def api_resume_session(sid):
    """Start timing the next segment after a Save. Paused time is never counted."""
    conn=db(); conn.execute('BEGIN IMMEDIATE'); s=conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'session not found'}),404
    if s['ended']: conn.close(); return jsonify({'error':'This study session is already finished.','code':'SESSION_FINISHED'}),409
    stamp=now_str()
    if not s['segment_started']:   # already running (double click) -> nothing to change
        conn.execute('UPDATE sessions SET segment_started=? WHERE id=?',(stamp,sid))
        conn.execute("UPDATE courses SET state='STUDYING',last_seen=? WHERE id=?",(stamp,s['course_id']))
    conn.commit(); conn.close(); return jsonify({'ok':True,'position':int(s['end_position'] or 0),'segment_started':s['segment_started'] or stamp})

def finish_session(sid,pos,auto_section=True):
    stamp=now_str(); conn=db(); conn.execute('BEGIN IMMEDIATE')
    s=conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error':'session not found'}),404
    c=conn.execute('SELECT * FROM courses WHERE id=?',(s['course_id'],)).fetchone()
    if s['ended']:  # Finish clicked twice / beacon after finish -> idempotent, nothing is written again
        done=bool(c and c['state']=='FINISH'); conn.close(); return jsonify({'ok':True,'completed':done,'already_finished':True})
    if not c: conn.execute('UPDATE sessions SET ended=?,end_position=?,segment_started=NULL WHERE id=?',(stamp,pos,sid)); conn.commit(); conn.close(); return jsonify({'ok':True,'completed':False})
    pos=min(max(pos,int(s['end_position'] or 0)),int(c['duration'])); completed=pos>=int(c['duration'])
    section=None
    if auto_section and AUTO_SECTION_ON_FINISH:
        seg_start=segment_start_for(conn,s)
        if pos>seg_start:  # unsaved time left; nothing left (e.g. Finish right after Save) -> no empty/duplicate section
            section=row_dict(insert_section(conn,s,c,seg_start,pos,AUTO_SECTION_TEXT,1,None))
    conn.execute('UPDATE sessions SET course_name=?,chapter=?,title=?,ended=?,end_position=?,segment_started=NULL WHERE id=?',(c['name'],c['chapter'],c['title'],stamp,pos,sid))
    conn.execute('UPDATE courses SET position=MAX(position,?),state=?,last_seen=? WHERE id=?',(pos,'FINISH' if completed else 'IN_PROGRESS',stamp,c['id']))
    conn.commit(); conn.close(); return jsonify({'ok':True,'completed':completed,'deleted':False,'section':section})

def _pos_from_request():
    p=request.get_json(force=True,silent=True) or {}
    try: return max(0,int(p.get('position',0)))
    except Exception: return None

@app.post('/api/sessions/<int:sid>/finish')
def api_finish(sid):
    pos=_pos_from_request()
    if pos is None: return jsonify({'error':'invalid position'}),400
    return finish_session(sid,pos,True)
@app.post('/api/sessions/<int:sid>/close')
def api_close_session(sid):
    # Tab closed / refreshed: close the session and keep its time, but do not invent a timeline section.
    pos=_pos_from_request()
    if pos is None: return jsonify({'error':'invalid position'}),400
    return finish_session(sid,pos,False)

@app.get('/api/courses/<course_id>/timeline')
def api_course_timeline(course_id):
    conn=db(); c=conn.execute('SELECT * FROM courses WHERE id=?',(course_id,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'course not found'}),404
    secs=course_sections(conn,course_id); conn.close()
    x=row_dict(c)
    return jsonify({'course':{k:x[k] for k in ('id','subject_code','name','chapter','title','duration','position')},'sections':secs,'count':len(secs),'total_seconds':sum(int(r['duration']) for r in secs)})

@app.get('/api/config')
def api_config(): return jsonify({'base_url':get_nfc_base_url(),'nfc':get_nfc_state(),'cloud_mode':CLOUD_MODE})
@app.get('/api/nfc/status')
def api_nfc_status(): return jsonify(get_nfc_state())
@app.get('/api/nfc/verify/<query>')
def api_nfc_verify(query):
    """Check whether an NFC Token or UID exists in the database.
    Returns the matching course immediately when found, otherwise a not-found result."""
    q=str(query).strip()
    course=get_course_by_token(q)
    if not course:
        course=get_course_by_uid(q)
    if not course:
        return jsonify({'found':False,'query':q}),404
    conn=db()
    stats=conn.execute('SELECT COUNT(*) AS cnt, COALESCE(SUM(MAX(0,COALESCE(end_position,start_position)-start_position)),0) AS secs FROM sessions WHERE course_id=? AND ended IS NOT NULL',(course['id'],)).fetchone()
    conn.close()
    x=row_dict(course); x['progress']=progress(x)
    x['sessions_count']=stats['cnt']; x['studied_seconds']=stats['secs']
    return jsonify({'found':True,'query':q,'course':x})

@app.get('/api/nfc/resolve/<uid>')
def api_nfc_resolve(uid):
    uid=normalize_uid(uid); c=get_course_by_uid(uid)
    if not c: return jsonify({'paired':False,'uid':uid}),404
    return jsonify({'paired':True,'uid':uid,'nfc_token':c['nfc_token'],'course':row_dict(c)})
@app.post('/api/nfc/pair/start')
def api_nfc_pair_start():
    if CLOUD_MODE: return jsonify({'error':'Cloud mode: no PN532 reader is attached. Write the course URL to the NFC tag using a compatible NFC-writing device/app.'}),503
    start_pairing()
    return jsonify({'ok':True,'message':'Pairing mode started. Tap one NFC tag on the PN532.'})
@app.post('/api/nfc/pair/cancel')
def api_nfc_pair_cancel():
    if CLOUD_MODE: return jsonify({'ok':True,'cloud_mode':True})
    cancel_pairing(); return jsonify({'ok':True})
@app.get('/health')
def health(): return {'ok':True,'time':now_str()}

init_db()
if __name__=='__main__':
    host=os.environ.get('HOST','0.0.0.0'); port=int(os.environ.get('PORT','5000')); base_url=get_nfc_base_url(port)
    if not CLOUD_MODE and start_nfc_reader: start_nfc_reader(base_url)
    if not CLOUD_MODE:
        threading.Thread(target=lambda: (_time.sleep(1.2), webbrowser.open(f'{base_url}/desktop')),daemon=True).start()
    app.run(host=host,port=port,debug=False)
