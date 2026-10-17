"""Optional access control for the public deployment (all opt-in via APP_PASSWORD).

* /nfc/<token>, static files, /health and /api/bridge/* stay reachable without a password
  (phones tapping a tag must work; the bridge has its own HMAC authentication).
* Everything else (Dashboard, Courses, History, management API) needs HTTP Basic auth.
* Study endpoints (/api/sessions/*, /api/courses/<id>/timeline) are allowed with Basic auth OR with a
  signed cookie that is only issued when the page /nfc/<valid token> was opened.
* Cross-site POST/PUT/DELETE requests are rejected (CSRF), plus basic security headers.
"""
from __future__ import annotations
import hmac, os, re, time, hashlib
from urllib.parse import urlparse
from flask import request, session, jsonify, Response
import database, bridge_api

PUBLIC_PREFIXES = ('/static/', '/nfc/', '/api/bridge/')
PUBLIC_EXACT = ('/health', '/favicon.ico', '/robots.txt')
TIMELINE_RE = re.compile(r'^/api/courses/([0-9a-f]{32})/timeline$')
SESSION_RE = re.compile(r'^/api/sessions/(\d+)/')
MAX_GRANTS = 40


def grant_course(course_id):
    g = [c for c in session.get('grants', []) if c != course_id][-(MAX_GRANTS - 1):]
    g.append(course_id)
    session['grants'] = g
    session.permanent = True


def _basic_ok(password, user):
    a = request.authorization
    if not a or a.type != 'basic':
        return False
    ok_u = hmac.compare_digest((a.username or '').encode(), user.encode())
    ok_p = hmac.compare_digest((a.password or '').encode(), password.encode())
    return ok_u and ok_p


def _challenge():
    r = Response('Authentication required', 401, {'WWW-Authenticate': 'Basic realm="StudyToken", charset="UTF-8"'})
    return r


def _course_of_session(sid):
    conn = database.connect()
    try:
        r = conn.execute('SELECT course_id FROM sessions WHERE id=?', (sid,)).fetchone()
        return r['course_id'] if r else None
    finally:
        conn.close()


def install(app):
    password = os.environ.get('APP_PASSWORD', '')
    user = os.environ.get('APP_USER', 'studytoken')
    if not app.config.get('SECRET_KEY'):
        base = os.environ.get('SECRET_KEY') or os.environ.get('BRIDGE_TOKEN') or password
        app.config['SECRET_KEY'] = (hashlib.sha256(b'studytoken-cookie|' + base.encode()).hexdigest()
                                    if base else os.urandom(32).hex())
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '1' if os.environ.get('RENDER') else '0') == '1',
                      SESSION_COOKIE_NAME='stk_session')

    @app.before_request
    def _guard():
        p = request.path
        if p.startswith('/api/bridge/'):
            return None
        # CSRF: browsers send Origin / Sec-Fetch-Site on cross-site writes
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify({'error': 'cross-site request blocked'}), 403
            origin = request.headers.get('Origin')
            if origin and urlparse(origin).netloc != request.host:
                return jsonify({'error': 'cross-origin request blocked'}), 403
        if not password:
            return None                               # open mode = original behaviour
        if p in PUBLIC_EXACT or p.startswith(PUBLIC_PREFIXES):
            return None
        ip = request.remote_addr or '?'
        if bridge_api.too_many_failures(ip):
            return jsonify({'error': 'too many failed attempts, wait a minute'}), 429
        if _basic_ok(password, user):
            return None
        # study page endpoints: cookie granted by opening /nfc/<token>
        grants = set(session.get('grants', []))
        m = TIMELINE_RE.match(p)
        if m and m.group(1) in grants:
            return None
        if p == '/api/sessions/start' and request.method == 'POST':
            cid = (request.get_json(silent=True) or {}).get('course_id')
            if cid in grants:
                return None
        m = SESSION_RE.match(p)
        if m and _course_of_session(int(m.group(1))) in grants:
            return None
        if request.authorization:
            bridge_api.note_failure(ip)
        return _challenge()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        if request.path.startswith('/api/'):
            resp.headers['Cache-Control'] = 'no-store'
        return resp
