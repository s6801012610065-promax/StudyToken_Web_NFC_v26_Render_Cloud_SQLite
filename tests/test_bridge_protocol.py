"""Bridge <-> server protocol: authentication, replay, clock skew, hub logic.
SIMULATED: requests are made by the test client; no PN532 involved."""
import json, threading, time, pytest
from conftest import make_app
from local_bridge import bridge_protocol as P

TOKEN = 'x' * 40

@pytest.fixture
def ap(monkeypatch):
    return make_app('sqlite', monkeypatch)

def call(appmod, method, path, body=None, token=TOKEN, now=None, nonce=None, tamper=False, bid='pc1'):
    raw = json.dumps(body).encode() if body is not None else b''
    h = P.make_headers(token, bid, method, path, raw, now)
    if nonce: 
        h[P.H_NONCE] = nonce; h[P.H_SIG] = P.sign(token, method, path, h[P.H_TS], nonce, raw)
    if tamper: raw = raw + b' '
    c = appmod.app.test_client()
    return c.open(path, method=method, data=raw, headers=h, content_type='application/json')

def hello(ap, inst='i1', bid='pc1', sim=True, reader=None):
    r = call(ap, 'POST', '/api/bridge/hello', {'instance_id': inst, 'simulated': sim, 'version': '1',
             'reader': reader or {'available': True, 'status': 'READY', 'port': 'COM3'}}, bid=bid)
    return r

def test_auth_rejections(ap):
    c = ap.app.test_client()
    assert c.post('/api/bridge/hello', json={}).status_code == 401                       # no headers
    assert call(ap, 'POST', '/api/bridge/hello', {}, token='y' * 40).get_json()['code'] == 'BAD_SIGNATURE'
    assert call(ap, 'POST', '/api/bridge/hello', {}, tamper=True).get_json()['code'] == 'BAD_SIGNATURE'   # body altered
    r = call(ap, 'POST', '/api/bridge/hello', {}, now=time.time() - 600)
    assert r.status_code == 401 and r.get_json()['code'] == 'CLOCK_SKEW'
    assert hello(ap).status_code == 200
    assert call(ap, 'POST', '/api/bridge/hello', {'instance_id': 'i1', 'simulated': True}, nonce='abc123').status_code == 200
    assert call(ap, 'POST', '/api/bridge/hello', {'instance_id': 'i1', 'simulated': True}, nonce='abc123').get_json()['code'] == 'REPLAY'

def test_server_without_token_is_disabled(monkeypatch):
    a = make_app('sqlite', monkeypatch, BRIDGE_TOKEN='')
    assert call(a, 'POST', '/api/bridge/hello', {}).get_json()['code'] == 'BRIDGE_DISABLED'

def test_rate_limit_after_repeated_failures(ap):
    for _ in range(15): call(ap, 'POST', '/api/bridge/hello', {}, token='z' * 40)
    assert call(ap, 'POST', '/api/bridge/hello', {}).status_code == 429

def test_simulated_bridge_rejected_by_default(monkeypatch):
    a = make_app('sqlite', monkeypatch, ALLOW_SIMULATED_BRIDGE='0')
    assert hello(a).get_json()['code'] == 'SIMULATED_NOT_ALLOWED'

def test_second_bridge_rejected_but_same_instance_may_reconnect(ap):
    s1 = hello(ap, 'i1').get_json()['session']
    assert hello(ap, 'i2', 'pc2').get_json()['code'] == 'BRIDGE_ALREADY_CONNECTED'
    s2 = hello(ap, 'i1').get_json()['session']; assert s2 != s1
    assert call(ap, 'GET', f'/api/bridge/poll?session={s1}&v=-1&wait=0').get_json()['code'] == 'UNKNOWN_SESSION'

def test_status_before_and_after_bridge(ap):
    c = ap.app.test_client()
    st = c.get('/api/nfc/status').get_json()
    assert st['available'] is False and st['status'] == 'WAITING FOR LOCAL NFC BRIDGE'
    hello(ap)
    st = c.get('/api/nfc/status').get_json()
    assert st['available'] and st['status'] == 'READY' and 'SIMULATED' in st['port'] and st['bridge']['simulated']

def event(ap, sess, **kw):
    return call(ap, 'POST', '/api/bridge/event', {'session': sess, **kw})

def test_pairing_tag_event_and_debounce(ap):
    c = ap.app.test_client(); sess = hello(ap).get_json()['session']
    c.post('/api/nfc/pair/start')
    d = call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json(); assert d['pairing'] is True
    assert event(ap, sess, type='tag', uid='04:AA:BB:CC', seq=1).status_code == 200
    st = c.get('/api/nfc/status').get_json()
    assert st['pair_uid'] == '04:AA:BB:CC' and st['pairing'] is False and st['last_uid'] == '04:AA:BB:CC'
    eid = st['event_id']
    event(ap, sess, type='tag', uid='04:AA:BB:CC', seq=2)                      # same tag within 3 s -> debounced
    assert c.get('/api/nfc/status').get_json()['event_id'] == eid
    event(ap, sess, type='tag', uid='04:11:11:11', seq=3)
    assert c.get('/api/nfc/status').get_json()['event_id'] == eid + 1
    assert event(ap, sess, type='tag', uid='04:22:22:22', seq=3).get_json()['duplicate'] is True   # retry of seq 3

def test_poll_wakes_up_immediately_on_change(ap):
    c = ap.app.test_client(); sess = hello(ap).get_json()['session']
    v = call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json()['version']
    t0 = time.time(); out = {}
    th = threading.Thread(target=lambda: out.update(call(ap, 'GET', f'/api/bridge/poll?session={sess}&v={v}&wait=10').get_json()))
    th.start(); time.sleep(0.5); c.post('/api/nfc/pair/start'); th.join(5)
    assert out['pairing'] is True and time.time() - t0 < 3

def test_write_flow_success_error_timeout_and_unavailable(ap, monkeypatch):
    c = ap.app.test_client(); sess = hello(ap).get_json()['session']
    hub = __import__('nfc_hub').hub
    # success
    res = {}
    th = threading.Thread(target=lambda: res.update(hub.request_nfc_url_write('04:AA', 'https://study.example.com/nfc/STK-AAAAAAAA', 5)))
    th.start(); time.sleep(0.3)
    d = call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json()
    assert d['write']['uid'] == '04:AA' and d['write']['url'].endswith('STK-AAAAAAAA')
    event(ap, sess, type='write_result', request_id=d['write']['request_id'], status='SUCCESS', seq=1); th.join(5)
    assert res['write_status'] == 'SUCCESS'
    assert call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json()['write'] is None
    # error reported by hardware
    th = threading.Thread(target=lambda: res.update(hub.request_nfc_url_write('04:AA', 'https://study.example.com/nfc/STK-BBBBBBBB', 5)))
    th.start(); time.sleep(0.3)
    rid = call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json()['write']['request_id']
    event(ap, sess, type='write_result', request_id=rid, status='ERROR', error='Tag is locked', seq=2); th.join(5)
    assert res['write_status'] == 'ERROR' and res['write_error'] == 'Tag is locked'
    # no tag presented -> WAITING_FOR_TAG after wait, then TTL expiry -> ERROR
    hub.write_ttl = 1.0
    r = hub.request_nfc_url_write('04:AA', 'https://study.example.com/nfc/STK-CCCCCCCC', 0.2)
    assert r['write_status'] == 'WAITING_FOR_TAG'
    time.sleep(1.3); assert hub.get_state()['write_status'] == 'ERROR' and 'Timed out' in hub.get_state()['write_error']
    # stale result for an old request is ignored
    event(ap, sess, type='write_result', request_id='old', status='SUCCESS', seq=3)
    assert hub.get_state()['write_status'] == 'ERROR'

def test_bridge_goes_offline_when_silent(ap):
    hub = __import__('nfc_hub').hub; hub.stale_seconds = 0.5
    hello(ap); time.sleep(0.8)
    st = ap.app.test_client().get('/api/nfc/status').get_json()
    assert st['available'] is False and st['status'] == 'LOCAL NFC BRIDGE OFFLINE'
    r = hub.request_nfc_url_write('04:AA', 'https://x/nfc/STK-AAAAAAAA', 1)
    assert r['write_status'] == 'NFC_UNAVAILABLE'

def test_add_course_with_prescan_writes_through_bridge(ap):
    """Full Save-with-UID path: the web request blocks until the (simulated) bridge confirms the write."""
    c = ap.app.test_client(); sess = hello(ap).get_json()['session']
    tok = c.post('/api/nfc/token/new', json={}).get_json()['nfc_token']
    box = {}
    def save():
        box['r'] = c.post('/api/courses', json=dict(subject_code='S1', name='N', chapter='1', title='t', duration='5', nfc_token=tok, nfc_uid='04:AB:CD:EF')).get_json()
    th = threading.Thread(target=save); th.start(); time.sleep(0.5)
    w = call(ap, 'GET', f'/api/bridge/poll?session={sess}&v=-1&wait=0').get_json()['write']
    assert w['url'] == f'https://study.example.com/nfc/{tok}' and w['uid'] == '04:AB:CD:EF'
    event(ap, sess, type='write_result', request_id=w['request_id'], status='SUCCESS', seq=1); th.join(10)
    assert box['r']['nfc_write_status'] == 'SUCCESS' and box['r']['tag_uid'] == '04:AB:CD:EF'
