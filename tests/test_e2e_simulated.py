"""END-TO-END with a SIMULATED reader.
Real: gunicorn process, real HTTP, real HMAC authentication, the real nfc_bridge.py process, real PostgreSQL/SQLite.
Fake: only the PN532 (SimulatedReader writes URLs to JSON files instead of NFC tags). NOT a hardware test."""
import json, os, signal, socket, subprocess, sys, time, tempfile, pytest, requests
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = 'e2e-secret-token-0123456789abcdef'
PY = sys.executable

def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p

class Env:
    def __init__(self, tmp, port, db_url=None):
        self.tmp, self.port, self.db_url = tmp, port, db_url
        self.url = f'http://127.0.0.1:{port}'
        self.taps, self.tags = os.path.join(tmp, 'taps.txt'), os.path.join(tmp, 'tags')
        open(self.taps, 'w').close(); self.srv = self.br = None
    def start_server(self):
        env = {**os.environ, 'PORT': str(self.port), 'NFC_MODE': 'bridge', 'BRIDGE_TOKEN': TOKEN, 'ALLOW_SIMULATED_BRIDGE': '1',
               'BASE_URL': 'https://study.example.com', 'BRIDGE_STALE_SECONDS': '6', 'APP_PASSWORD': '',
               'DATABASE_URL': self.db_url or '', 'STUDYTOKEN_DB': os.path.join(self.tmp, 'e2e.db')}
        self.srv = subprocess.Popen([PY, '-m', 'gunicorn', '-c', 'gunicorn.conf.py', 'app:app'], cwd=ROOT, env=env,
                                    stdout=open(os.path.join(self.tmp, 'server.log'), 'a'), stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if requests.get(self.url + '/health', timeout=1).ok: return
            except Exception: time.sleep(0.2)
        raise RuntimeError('server did not start')
    def stop_server(self):
        if self.srv: self.srv.send_signal(signal.SIGTERM); self.srv.wait(15); self.srv = None
    def start_bridge(self, token=TOKEN, fail_writes=False, extra=(), allowed=True):
        benv = {**os.environ, 'ALLOWED_WRITE_HOSTS': 'study.example.com'} if allowed else {k: v for k, v in os.environ.items() if k != 'ALLOWED_WRITE_HOSTS'}
        cmd = [PY, 'local_bridge/nfc_bridge.py', '--server', self.url, '--token', token, '--simulate', '--sim-taps', self.taps,
               '--sim-tags', self.tags, '--env-file', os.path.join(self.tmp, 'none.env'), '--log-file', os.path.join(self.tmp, 'bridge.log'), *extra]
        if fail_writes: cmd.append('--sim-fail-writes')
        self.br = subprocess.Popen(cmd, cwd=ROOT, env=benv, stdout=open(os.path.join(self.tmp, 'bridge.out'), 'a'), stderr=subprocess.STDOUT)
    def stop_bridge(self, sig=signal.SIGINT):
        if self.br: self.br.send_signal(sig); self.br.wait(15); self.br = None
    def tap(self, uid): open(self.taps, 'a').write(uid + '\n')
    def status(self): return requests.get(self.url + '/api/nfc/status', timeout=5).json()
    def wait(self, pred, timeout=20, what=''):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                s = self.status()
                if pred(s): return s
            except Exception: pass
            time.sleep(0.2)
        raise AssertionError(f'timeout waiting for {what}; last={self.status() if self.srv else None}\nbridge log:\n' + open(os.path.join(self.tmp, 'bridge.out')).read()[-1500:])
    def post(self, path, **j): return requests.post(self.url + path, json=j, timeout=30)
    def close(self):
        self.stop_bridge(); self.stop_server()

@pytest.fixture(params=['sqlite', 'pg'])
def env(request):
    from conftest import _pg_url
    url = None
    if request.param == 'pg':
        url = _pg_url()
        if not url: pytest.skip('no PostgreSQL')
        import psycopg
        with psycopg.connect(url, autocommit=True) as c:
            for t in ('timeline_sections', 'sessions', 'courses', 'subjects'): c.execute(f'DROP TABLE IF EXISTS {t}')
    e = Env(tempfile.mkdtemp(), free_port(), url); e.start_server()
    yield e
    e.close()

def test_full_nfc_workflow_with_reconnect(env):
    env.start_bridge()
    s = env.wait(lambda s: s['available'] and s['status'] == 'READY', what='bridge online')
    assert 'SIMULATED' in s['port'] and s['bridge']['online']
    # 1. real-flow: Add Course -> Scan NFC (pairing) -> tap -> UID reaches the page
    tok = env.post('/api/nfc/token/new').json()['nfc_token']
    env.post('/api/nfc/pair/start'); time.sleep(0.5); env.tap('04:A1:B2:C3:D4:E5:F6')
    s = env.wait(lambda s: s.get('pair_uid'), what='pair uid'); assert s['pair_uid'] == '04:A1:B2:C3:D4:E5:F6'
    # 2. Save with pre-scanned UID -> course saved, URL written through the bridge (tag must be on the reader)
    r = env.post('/api/courses', subject_code='E2E1', name='Networks', chapter='1', title='Intro', duration='5', nfc_token=tok, nfc_uid=s['pair_uid']).json()
    assert r['nfc_write_status'] == 'SUCCESS', r
    written = json.load(open(os.path.join(env.tags, '04A1B2C3D4E5F6.json')))
    assert written['url'] == f'https://study.example.com/nfc/{tok}'
    # 3. UID -> token -> course
    res = requests.get(env.url + '/api/nfc/resolve/04:A1:B2:C3:D4:E5:F6').json()
    assert res['nfc_token'] == tok and res['course']['name'] == 'Networks'
    # 4. a later plain tap (not pairing) is seen by the watcher as a new event
    ev = env.status()['event_id']; time.sleep(3.2); env.tap('04:A1:B2:C3:D4:E5:F6')
    env.wait(lambda s: s['event_id'] > ev, what='tap event')
    # 5. server restart (Render redeploy): bridge must reconnect by itself
    env.stop_server(); time.sleep(1); env.start_server()
    env.wait(lambda s: s['available'] and s['bridge']['online'], timeout=60, what='reconnect after server restart')
    assert requests.get(env.url + '/api/nfc/resolve/04:A1:B2:C3:D4:E5:F6').json()['nfc_token'] == tok   # data persisted
    # 6. link flow after save (second chapter, second tag) incl. write
    tok2 = env.post('/api/nfc/token/new').json()['nfc_token']
    c2 = env.post('/api/courses', subject_code='E2E1', name='x', chapter='2', title='t', duration='5', nfc_token=tok2).json()
    env.post('/api/nfc/pair/start'); time.sleep(0.5); env.tap('04:00:00:00:00:02')
    s = env.wait(lambda s: s.get('pair_uid'), what='pair 2')
    out = env.post('/api/nfc/link', nfc_token=tok2, nfc_uid=s['pair_uid']).json()
    assert out['nfc_write_status'] == 'SUCCESS'
    assert json.load(open(os.path.join(env.tags, '0400000000' + '02.json')))['url'].endswith(tok2)

def test_wrong_token_never_connects_and_recovers_after_fix(env):
    env.start_bridge(token='W' * 40)
    time.sleep(4)
    assert env.status()['bridge']['online'] is False
    assert 'BAD_SIGNATURE' in open(os.path.join(env.tmp, 'bridge.out')).read()
    env.stop_bridge(); env.start_bridge()
    env.wait(lambda s: s['available'], what='online with right token')

def test_write_failure_is_reported_not_faked(env):
    env.start_bridge(fail_writes=True); env.wait(lambda s: s['available'])
    tok = env.post('/api/nfc/token/new').json()['nfc_token']
    env.tap('04:DE:AD:BE:EF'); time.sleep(1)
    r = env.post('/api/courses', subject_code='F1', name='n', chapter='1', title='t', duration='5', nfc_token=tok, nfc_uid='04:DE:AD:BE:EF').json()
    assert r['nfc_write_status'] == 'ERROR' and 'simulated write failure' in r['nfc_write_error']
    assert not os.path.exists(os.path.join(env.tags, '04DEADBEEF.json'))

def test_bridge_crash_detected_and_clean_exit_releases(env):
    env.start_bridge(); env.wait(lambda s: s['available'])
    env.stop_bridge(signal.SIGKILL)                          # crash: no bye
    env.wait(lambda s: s['status'] == 'LOCAL NFC BRIDGE OFFLINE', timeout=15, what='offline detection (stale)')
    env.start_bridge(); env.wait(lambda s: s['available'], timeout=30, what='restart after crash')
    env.stop_bridge(signal.SIGINT)                           # clean exit: bye
    env.wait(lambda s: not s['available'], timeout=5, what='released on clean exit')

def test_plain_http_to_remote_server_refused():
    p = subprocess.run([PY, 'local_bridge/nfc_bridge.py', '--server', 'http://example.com', '--token', 'x' * 40, '--env-file', 'none'],
                       cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and 'https' in (p.stderr + p.stdout)

def test_bridge_refuses_to_write_url_for_foreign_host(env):
    """Defence in depth: bridge only writes URLs whose host is allowed (default: the server's own host)."""
    env.start_bridge(allowed=False); env.wait(lambda s: s['available'])
    tok = env.post('/api/nfc/token/new').json()['nfc_token']; env.tap('04:BA:D0:00:01'); time.sleep(1)
    r = env.post('/api/courses', subject_code='H1', name='n', chapter='1', title='t', duration='5', nfc_token=tok, nfc_uid='04:BA:D0:00:01').json()
    assert r['nfc_write_status'] == 'ERROR' and 'host not allowed' in r['nfc_write_error']
    assert not os.path.exists(os.path.join(env.tags, '04BAD00001.json'))
