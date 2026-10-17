#!/usr/bin/env python3
"""StudyToken Local NFC Bridge (Windows).

Runs on the PC that has the PN532 attached. It connects OUTBOUND to the Render web app over
HTTPS (no inbound ports, no router changes), authenticates every request with an HMAC signature,
reports tag UIDs and performs NDEF URL writes requested by the web app.

    python nfc_bridge.py                 # normal use (reads bridge.env)
    python nfc_bridge.py --list-ports    # show COM ports
    python nfc_bridge.py --check-reader  # test the PN532 locally, no server needed
"""
from __future__ import annotations
import argparse, json, logging, logging.handlers, os, queue, random, re, signal, socket, sys, threading, time, uuid
from urllib.parse import urlparse, urlencode
try:
    import requests
except ImportError:
    sys.exit('Missing dependency: pip install -r requirements.txt')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_protocol as proto
from bridge_reader import Pn532Reader, SimulatedReader
import pn532_raw as raw

VERSION = '1.1'
log = logging.getLogger('bridge')
TOKEN_URL_RE = re.compile(r'^https?://[^/\s]+/nfc/STK-[0-9A-F]{8}$')


def load_env_file(path):
    out = {}
    if path and os.path.exists(path):
        for ln in open(path, encoding='utf-8-sig'):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class AuthProblem(Exception):
    def __init__(self, code, msg, retry=30):
        super().__init__(msg); self.code, self.retry = code, retry


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.server = cfg['server'].rstrip('/')
        self.token = cfg['token']
        self.bridge_id = cfg['bridge_id']
        self.instance_id = uuid.uuid4().hex
        self.http = requests.Session()
        self.stop = threading.Event()
        self.session = None
        self.version = -1
        self.events = queue.Queue()
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.reader_state = {'available': False, 'status': 'STARTING', 'port': None, 'error': None}
        self.done_writes = []
        self.allowed_hosts = {h.strip().lower() for h in (cfg.get('allowed_hosts') or urlparse(self.server).netloc).split(',') if h.strip()}
        self.last_tag = {'uid': None, 'time': 0.0}
        self.pairing = False
        Reader = SimulatedReader if cfg.get('simulate') else Pn532Reader
        kw = ({'taps_file': cfg.get('sim_taps'), 'tags_dir': cfg.get('sim_tags'), 'fail_writes': bool(cfg.get('sim_fail_writes'))}
              if cfg.get('simulate') else {'com_port': cfg.get('com_port', 'auto'), 'backend': cfg.get('backend', 'raw'), 'baud': cfg.get('baud', 115200)})
        self.reader = Reader(self._on_tag, self._on_state, self._on_write_result, **kw)

    # ------------------------------------------------------------- HTTP
    def _call(self, method, path, query=None, body=None, timeout=(8, 20)):
        target = path + (('?' + urlencode(query)) if query else '')
        raw = json.dumps(body).encode() if body is not None else b''
        hdr = proto.make_headers(self.token, self.bridge_id, method, target, raw)
        hdr['Content-Type'] = 'application/json'
        try:
            r = self.http.request(method, self.server + target, data=raw, headers=hdr, timeout=timeout)
        except requests.RequestException as e:
            raise ConnectionError(str(e).split('\n')[0][:200])
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200:
            return data
        code = data.get('code', f'HTTP_{r.status_code}')
        msg = data.get('error', r.reason)
        if code == 'CLOCK_SKEW':
            raise AuthProblem(code, f'{msg}. Sync the Windows clock (Settings > Time > Sync now).', 20)
        if code in ('BAD_SIGNATURE', 'MISSING_AUTH'):
            raise AuthProblem(code, f'{msg}. Check BRIDGE_TOKEN in bridge.env matches Render.', 30)
        if code == 'BRIDGE_DISABLED':
            raise AuthProblem(code, msg, 30)
        if code == 'BRIDGE_ALREADY_CONNECTED':
            raise AuthProblem(code, msg, 10)
        if code == 'SIMULATED_NOT_ALLOWED':
            raise AuthProblem(code, msg, 60)
        if r.status_code == 429:
            raise AuthProblem(code, 'rate limited by server', int(r.headers.get('Retry-After', 60)))
        if code == 'UNKNOWN_SESSION':
            raise LookupError(msg)
        raise ConnectionError(f'{code}: {msg}')

    # --------------------------------------------------- reader callbacks
    def _next_seq(self):
        with self.seq_lock:
            self.seq += 1
            return self.seq

    def _push(self, kind, ttl, **kw):
        self.events.put({'type': kind, 'seq': self._next_seq(), 'expires': time.time() + ttl, **kw})

    def _on_state(self, available, status, port, error):
        new = {'available': available, 'status': status, 'port': port, 'error': error}
        if new != self.reader_state:
            self.reader_state = new
            log.info('reader: %s %s', status, port or '')
            self._push('reader_state', 120, **new)

    def _on_tag(self, uid):
        now = time.time()
        if uid == self.last_tag['uid'] and now - self.last_tag['time'] < 1.0 and not self.pairing:
            return                                   # same tag still on the reader: don't flood the server
        self.last_tag.update(uid=uid, time=now)
        log.info('tag %s', uid)
        self._push('tag', 10, uid=uid)               # a tap older than 10 s is never replayed after an outage

    def _on_write_result(self, request_id, ok, err, uid):
        self.done_writes = (self.done_writes + [request_id])[-20:]
        self._push('write_result', 90, request_id=request_id, status='SUCCESS' if ok else 'ERROR', error=err, uid=uid)

    # ------------------------------------------------------ main loops
    def _sender(self):
        pending = None
        backoff = 1.0
        while not self.stop.is_set():
            if pending is None:
                try:
                    pending = self.events.get(timeout=0.5)
                except queue.Empty:
                    continue
            if time.time() > pending['expires']:
                log.warning('dropping stale %s event', pending['type']); pending = None; continue
            if not self.session:
                self.stop.wait(0.5); continue
            try:
                self._call('POST', '/api/bridge/event', body={**{k: v for k, v in pending.items() if k != 'expires'}, 'session': self.session})
                pending = None; backoff = 1.0
            except LookupError:
                self.session = None                 # poll loop will re-hello
            except (ConnectionError, AuthProblem) as e:
                log.warning('event send failed (%s); retrying in %.0fs', e, backoff)
                self.stop.wait(backoff); backoff = min(15, backoff * 2)

    def _valid_write(self, w):
        url, uid = w.get('url', ''), w.get('uid', '')
        if not TOKEN_URL_RE.match(url):
            return 'URL is not a StudyToken URL'
        if urlparse(url).netloc.lower() not in self.allowed_hosts:
            return f'URL host not allowed (allowed: {", ".join(sorted(self.allowed_hosts))})'
        if not re.fullmatch(r'([0-9A-F]{2}:)*[0-9A-F]{2}', uid):
            return 'bad UID'
        return None

    def _apply(self, d):
        self.version = d['version']
        was_pairing, self.pairing = self.pairing, bool(d.get('pairing'))
        if self.pairing:
            self.last_tag.update(uid=None, time=0.0)
            if not was_pairing:
                self.reader.rescan()                # a tag already resting on the reader is reported again
        w = d.get('write')
        if w and w['request_id'] not in self.done_writes:
            bad = self._valid_write(w)
            if bad:
                log.error('refusing write request: %s', bad)
                self._on_write_result(w['request_id'], False, bad, w.get('uid', ''))
            else:
                log.info('write request for %s -> %s', w['uid'], w['url'])
                self.reader.request_write(w['request_id'], w['uid'], w['url'], w['expires_in'])
        elif not w:
            self.reader.clear_write()

    def run(self):
        self.reader.start()
        threading.Thread(target=self._sender, daemon=True, name='sender').start()
        threading.Thread(target=self._loop, daemon=True, name='poll').start()
        while not self.stop.wait(0.5):      # short waits keep Ctrl+C responsive on Windows; a long-poll never delays exit
            pass
        self.shutdown()

    def _loop(self):
        fails = 0
        while not self.stop.is_set():
            try:
                if not self.session:
                    info = {'bridge_version': VERSION, 'version': VERSION, 'instance_id': self.instance_id,
                            'simulated': bool(self.cfg.get('simulate')), 'reader': self.reader_state, 'host': socket.gethostname()}
                    h = self._call('POST', '/api/bridge/hello', body=info, timeout=(10, 75))   # long read timeout: Render cold start
                    self.session, self.version = h['session'], -1
                    log.info('connected to %s', self.server)
                    self._push('reader_state', 120, **self.reader_state)
                d = self._call('GET', '/api/bridge/poll', query={'session': self.session, 'v': self.version, 'wait': 25}, timeout=(8, 40))
                self._apply(d); fails = 0
            except LookupError:
                log.warning('server forgot this session (restart?) - reconnecting'); self.session = None
            except AuthProblem as e:
                log.error('[%s] %s (retry in %ds)', e.code, e, e.retry); self.session = None; self.stop.wait(e.retry)
            except ConnectionError as e:
                fails += 1
                delay = min(30, 2 ** min(fails, 5)) * random.uniform(0.7, 1.3)
                log.warning('server unreachable: %s (retry in %.0fs)', e, delay); self.session = None; self.stop.wait(delay)

    def shutdown(self):
        self.reader.stop()
        if self.session:
            try: self._call('POST', '/api/bridge/bye', body={'session': self.session}, timeout=(3, 3))
            except Exception: pass
        log.info('bridge stopped')


def check_reader(com_port, baud=115200, backend='raw'):
    """Local hardware self-test (no server): probes every serial port, opens the PN532, waits for a tag and
    prints UID, tag type and the NDEF URL stored on it."""
    if backend == 'nfcpy':
        return check_reader_nfcpy(com_port)
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.exit('pyserial is not installed: pip install -r requirements.txt')
    ports = raw.candidate_ports(com_port)
    print('Ports to try:', ', '.join(ports) or '(none found)')
    pn, port, errs = raw.find_pn532(com_port, baud)
    for p, e in errs.items():
        print(f'  {p}: {e}')
    if not pn:
        bauds = [b for b in (115200, 57600, 9600, 230400) if b != baud]
        for b in bauds:                                   # only in this diagnostic: some boards use another speed
            pn, port, _ = raw.find_pn532(com_port, b)
            if pn:
                print(f'*** PN532 answered at {b} baud on {port}. Set BAUD={b} in bridge.env ***'); baud = b; break
    if not pn:
        sys.exit('\nNo PN532 answered on any port.\n'
                 ' - Is the board in UART/HSU mode? (DIP switches: SEL0=OFF SEL1=OFF for HSU on the Elechouse/Keyestudio boards)\n'
                 ' - Is the USB-serial driver installed (CH340/CP210x) and is the COM port visible in Device Manager?\n'
                 ' - Close every other program that uses the port (old StudyToken, Arduino IDE, PuTTY).\n'
                 ' - Swap TX/RX if you wired the board yourself. Try COM_PORT=COMx in bridge.env.')
    print(f'PN532 firmware {pn.firmware} opened on {port} @ {baud} baud. Present a tag (30 s)...')
    t0, last = time.time(), None
    try:
        while time.time() - t0 < 30:
            t = pn.list_passive()
            if not t:
                last = None; time.sleep(0.05); continue
            uid = raw.uid_str(t['uid'])
            if uid == last:
                time.sleep(0.2); continue
            last = uid
            print('UID :', uid, f"({len(t['uid'])} bytes)  ATQA={t['atqa'].hex()} SAK=0x{t['sak']:02X}")
            print('Type:', raw.describe_tag(pn, t))
            if raw.is_type2(t):
                try: print('NDEF URL:', raw.t2_read_url(pn) or '(none)')
                except raw.Pn532Error as e: print('NDEF: cannot read:', e)
            pn.release()
        print('Done.')
    finally:
        pn.close()


def check_reader_nfcpy(com_port):
    try:
        import nfc
    except ImportError:
        sys.exit('nfcpy is not installed: pip install nfcpy')
    r = Pn532Reader(lambda u: None, lambda *a: None, lambda *a: None, com_port=com_port, backend='nfcpy')
    clf, port = r._find(nfc)
    if not clf:
        sys.exit('No PN532 found via nfcpy.')
    print(f'PN532 opened on {port} (nfcpy). Present a tag (30 s)...')
    def on_connect(tag):
        print('UID :', tag.identifier.hex(':').upper()); print('Type:', tag)
        if tag.ndef:
            print('NDEF:', [str(x) for x in tag.ndef.records] or '(empty)')
        return False
    t0 = time.time()
    clf.connect(rdwr={'on-connect': on_connect}, terminate=lambda: time.time() - t0 > 30)
    clf.close()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description='StudyToken Local NFC Bridge')
    ap.add_argument('--env-file', default=os.path.join(here, 'bridge.env'))
    ap.add_argument('--server'); ap.add_argument('--token'); ap.add_argument('--port', help='COM port or auto')
    ap.add_argument('--list-ports', action='store_true'); ap.add_argument('--check-reader', action='store_true')
    ap.add_argument('--allow-insecure-http', action='store_true', help='allow http:// to a non-local server (testing only)')
    ap.add_argument('--backend', choices=['raw', 'nfcpy', 'auto']); ap.add_argument('--baud', type=int)
    ap.add_argument('--simulate', action='store_true', help='NO HARDWARE: simulated reader for tests')
    ap.add_argument('--sim-taps'); ap.add_argument('--sim-tags'); ap.add_argument('--sim-fail-writes', action='store_true')
    ap.add_argument('--log-file', default=os.path.join(here, 'bridge.log'))
    a = ap.parse_args()
    env = {**load_env_file(a.env_file), **{k: v for k, v in os.environ.items() if k.startswith(('SERVER_URL', 'BRIDGE_', 'COM_PORT', 'ALLOWED_', 'READER_BACKEND', 'BAUD'))}}
    handlers = [logging.StreamHandler()]
    try: handlers.append(logging.handlers.RotatingFileHandler(a.log_file, maxBytes=1_000_000, backupCount=3, encoding='utf-8'))
    except OSError: pass
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    if a.list_ports:
        try:
            from serial.tools import list_ports
            for p in list_ports.comports(): print(p.device, '-', p.description, f'[VID:PID={p.vid:04X}:{p.pid:04X}]' if p.vid else '')
        except ImportError: print('pyserial missing')
        return
    com = a.port or env.get('COM_PORT', 'auto')
    backend = (a.backend or env.get('READER_BACKEND') or 'raw').lower()
    baud = a.baud or int(env.get('BAUD') or 115200)
    if a.check_reader:
        return check_reader(com, baud, backend)
    server = (a.server or env.get('SERVER_URL', '')).strip()
    token = (a.token or env.get('BRIDGE_TOKEN', '')).strip()
    if not server or len(token) < 24:
        sys.exit('Set SERVER_URL and BRIDGE_TOKEN (>=24 chars) in bridge.env (see bridge.env.example)')
    host = urlparse(server).hostname or ''
    if urlparse(server).scheme != 'https' and host not in ('localhost', '127.0.0.1') and not a.allow_insecure_http:
        sys.exit('Refusing plain http:// to a remote server. Use the https:// Render URL.')
    if a.simulate:
        log.warning('*** SIMULATION MODE: no real PN532 is used. Never use for real tags. ***')
    cfg = {'server': server, 'token': token, 'bridge_id': env.get('BRIDGE_ID') or socket.gethostname(), 'com_port': com, 'backend': backend, 'baud': baud,
           'allowed_hosts': env.get('ALLOWED_WRITE_HOSTS'), 'simulate': a.simulate, 'sim_taps': a.sim_taps,
           'sim_tags': a.sim_tags, 'sim_fail_writes': a.sim_fail_writes}
    b = Bridge(cfg)
    signal.signal(signal.SIGINT, lambda *_: b.stop.set())
    if hasattr(signal, 'SIGTERM'): signal.signal(signal.SIGTERM, lambda *_: b.stop.set())
    log.info('StudyToken Local NFC Bridge %s -> %s (COM: %s, backend: %s)', VERSION, server, com, backend)
    b.run()


if __name__ == '__main__':
    main()
