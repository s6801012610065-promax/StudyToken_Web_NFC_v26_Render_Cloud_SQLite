"""HTTP endpoints for the Local NFC Bridge (/api/bridge/*). Every request must be HMAC-signed."""
from __future__ import annotations
import json, logging, os, threading, time
from flask import Blueprint, jsonify, request
from local_bridge import bridge_protocol as bp_
import nfc_hub

log = logging.getLogger('bridge_api')
bp = Blueprint('bridge', __name__, url_prefix='/api/bridge')
_nonces: dict[str, float] = {}
_fails: dict[str, list[float]] = {}
_lock = threading.RLock()
MAX_BODY = 64 * 1024
MAX_FAILS_PER_MIN = 15


def _secrets():
    out = [os.environ.get('BRIDGE_TOKEN', '').strip(), os.environ.get('BRIDGE_TOKEN_PREVIOUS', '').strip()]
    return [s for s in out if len(s) >= 24]


def _err(status, code, msg, **extra):
    r = jsonify({'ok': False, 'code': code, 'error': msg, 'server_time': int(time.time()), **extra})
    r.status_code = status
    return r


def _client_ip():
    return request.remote_addr or '?'


def note_failure(ip):
    now = time.time()
    with _lock:
        lst = [t for t in _fails.get(ip, []) if now - t < 60]
        lst.append(now)
        _fails[ip] = lst


def too_many_failures(ip):
    now = time.time()
    with _lock:
        return len([t for t in _fails.get(ip, []) if now - t < 60]) >= MAX_FAILS_PER_MIN


def _authenticate():
    """Return (bridge_id, body_bytes) or an error response."""
    ip = _client_ip()
    secs = _secrets()
    if not secs:
        return _err(503, 'BRIDGE_DISABLED', 'BRIDGE_TOKEN is not configured on the server (min 24 chars)')
    if too_many_failures(ip):
        r = _err(429, 'TOO_MANY_FAILURES', 'Too many failed authentication attempts')
        r.headers['Retry-After'] = '60'
        return r
    if request.content_length and request.content_length > MAX_BODY:
        return _err(413, 'TOO_LARGE', 'Body too large')
    h = request.headers
    bid, ts, nonce, sig = h.get(bp_.H_ID, ''), h.get(bp_.H_TS, ''), h.get(bp_.H_NONCE, ''), h.get(bp_.H_SIG, '')
    if not (bid and ts and nonce and sig) or len(nonce) > 64 or len(bid) > 80:
        note_failure(ip)
        return _err(401, 'MISSING_AUTH', 'Missing authentication headers')
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        note_failure(ip)
        return _err(401, 'MISSING_AUTH', 'Bad timestamp')
    body = request.get_data()
    target = request.path + ('?' + request.query_string.decode('ascii', 'ignore') if request.query_string else '')
    if not bp_.check_signature(secs, request.method, target, ts, nonce, body, sig):
        note_failure(ip)
        log.warning('bridge auth: bad signature from %s', ip)
        return _err(401, 'BAD_SIGNATURE', 'Signature rejected (wrong BRIDGE_TOKEN?)')
    if skew > bp_.MAX_SKEW_SECONDS:
        return _err(401, 'CLOCK_SKEW', f'Clock differs by {int(skew)}s (max {bp_.MAX_SKEW_SECONDS}s); fix the PC clock')
    now = time.time()
    with _lock:
        for k in [k for k, t in _nonces.items() if t < now]:
            del _nonces[k]
        if nonce in _nonces:
            note_failure(ip)
            return _err(401, 'REPLAY', 'Nonce already used')
        _nonces[nonce] = now + 2 * bp_.MAX_SKEW_SECONDS
    return bid, body


def _json(body):
    try:
        d = json.loads(body.decode() or '{}')
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _guard(fn):
    def wrapper():
        a = _authenticate()
        if not isinstance(a, tuple):
            return a
        try:
            return fn(a[0], a[1])
        except nfc_hub.UnknownSession:
            return _err(409, 'UNKNOWN_SESSION', 'Session unknown (server restarted?) - send hello again')
        except nfc_hub.BridgeBusy as e:
            return _err(409, 'BRIDGE_ALREADY_CONNECTED', str(e))
        except nfc_hub.SimulatedNotAllowed:
            return _err(403, 'SIMULATED_NOT_ALLOWED', 'Simulated bridges are not accepted by this server')
    wrapper.__name__ = fn.__name__
    return wrapper


@bp.post('/hello')
@_guard
def hello(bid, body):
    d = _json(body)
    sess = nfc_hub.hub.bridge_hello(bid, str(d.get('instance_id') or bid), d)
    return jsonify({'ok': True, 'session': sess, 'protocol': bp_.PROTOCOL_VERSION, 'server_time': int(time.time()),
                    'poll_wait': 25, 'stale_after': nfc_hub.hub.stale_seconds})


@bp.get('/poll')
@_guard
def poll(bid, body):
    try:
        since = int(request.args.get('v', '-1'))
        wait = min(25.0, max(0.0, float(request.args.get('wait', '25'))))
    except ValueError:
        return _err(400, 'BAD_REQUEST', 'bad v/wait')
    d = nfc_hub.hub.bridge_poll(request.args.get('session', ''), since, wait)
    return jsonify({'ok': True, **d})


@bp.post('/event')
@_guard
def event(bid, body):
    d = _json(body)
    applied = nfc_hub.hub.bridge_event(str(d.get('session') or ''), d)
    return jsonify({'ok': True, 'duplicate': not applied})


@bp.post('/bye')
@_guard
def bye(bid, body):
    nfc_hub.hub.bridge_bye(str(_json(body).get('session') or ''))
    return jsonify({'ok': True})
