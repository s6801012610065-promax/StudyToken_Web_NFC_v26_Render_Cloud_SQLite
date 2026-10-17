import base64, pytest
from conftest import make_app

def basic(u='studytoken', p='s3cret'): return {'Authorization': 'Basic ' + base64.b64encode(f'{u}:{p}'.encode()).decode()}

@pytest.fixture
def sec(monkeypatch):
    a = make_app('sqlite', monkeypatch, APP_PASSWORD='s3cret'); return a, a.app.test_client()

def mk(c):
    tok = c.post('/api/nfc/token/new', json={}, headers=basic()).get_json()['nfc_token']
    j = c.post('/api/courses', json=dict(subject_code='S', name='N', chapter='1', title='t', duration='5', nfc_token=tok), headers=basic()).get_json()
    return j['course_id'], tok

def test_dashboard_requires_password(sec):
    a, c = sec
    for p in ('/', '/history', '/desktop', '/api/courses', '/api/history', '/api/nfc/status'):
        assert c.get(p).status_code == 401, p
    assert c.get('/api/courses', headers=basic(p='wrong')).status_code == 401
    assert c.get('/api/courses', headers=basic()).status_code == 200
    assert c.get('/health').status_code == 200 and c.get('/static/app.js').status_code == 200

def test_phone_flow_works_without_password_via_cookie(sec):
    a, c = sec; cid, tok = mk(c)
    phone = a.app.test_client()                                        # fresh browser, no credentials
    assert phone.post('/api/sessions/start', json={'course_id': cid}).status_code == 401   # not opened via NFC URL yet
    assert phone.get('/nfc/' + tok).status_code == 200                 # tap -> cookie granted
    sid = phone.post('/api/sessions/start', json={'course_id': cid}).get_json()['session_id']
    assert phone.post(f'/api/sessions/{sid}/autosave', json={'position': 30}).status_code == 200
    assert phone.post(f'/api/sessions/{sid}/sections', json={'position': 30, 'description': 'x'}).status_code == 200
    assert phone.get(f'/api/courses/{cid}/timeline').status_code == 200
    assert phone.post(f'/api/sessions/{sid}/finish', json={'position': 30}).status_code == 200
    assert phone.get('/api/courses').status_code == 401                # but cannot manage/list courses
    other = a.app.test_client(); assert other.post(f'/api/sessions/{sid}/autosave', json={'position': 1}).status_code == 401

def test_cross_site_writes_blocked(sec):
    a, c = sec
    r = c.post('/api/nfc/token/new', json={}, headers={**basic(), 'Origin': 'https://evil.example'}); assert r.status_code == 403
    r = c.post('/api/nfc/token/new', json={}, headers={**basic(), 'Sec-Fetch-Site': 'cross-site'}); assert r.status_code == 403
    r = c.post('/api/nfc/token/new', json={}, headers={**basic(), 'Origin': 'http://localhost'}); assert r.status_code == 200

def test_open_mode_unchanged(monkeypatch):
    a = make_app('sqlite', monkeypatch); c = a.app.test_client()
    assert c.get('/api/courses').status_code == 200
