"""Server-side NFC hub used on Render (NFC_MODE=bridge).

Exposes the SAME functions as nfc_reader.py (get_state, start_pairing, cancel_pairing,
request_nfc_url_write) and the same state dictionary, so app.py and the browser GUI work
unchanged. The physical PN532 lives on the Windows PC; the Local NFC Bridge connects
outbound (HTTPS long-polling) and reports tags / executes write requests.
State is kept in memory -> the web service must run ONE process (see gunicorn.conf.py).
"""
from __future__ import annotations
import os, threading, time, uuid

DEBOUNCE_SECONDS = 3.0


class UnknownSession(Exception): ...
class BridgeBusy(Exception): ...
class SimulatedNotAllowed(Exception): ...


class NfcHub:
    def __init__(self):
        self.stale_seconds = float(os.environ.get('BRIDGE_STALE_SECONDS', '45'))
        self.write_ttl = float(os.environ.get('BRIDGE_WRITE_TTL', '60'))
        self.allow_simulated = os.environ.get('ALLOW_SIMULATED_BRIDGE', '0') == '1'
        self.cond = threading.Condition(threading.RLock())
        self.reset()

    def reset(self):
        with self.cond:
            self.state = {'available': False, 'status': 'WAITING FOR LOCAL NFC BRIDGE', 'port': None, 'last_uid': None,
                          'last_seen': None, 'last_error': None, 'event_id': 0, 'pairing': False, 'pair_uid': None,
                          'pair_event_id': None, 'pair_token': None, 'write_status': 'IDLE', 'write_uid': None,
                          'write_url': None, 'write_error': None, 'write_event_id': None}
            self.bridge = None            # dict once a bridge said hello
            self.version = 0              # bumps when what the bridge should do changes
            self.write = None
            self.write_counter = 0
            self.last_open = {'uid': None, 'time': 0.0}
            self.cond.notify_all()

    # ---------------------------------------------------------------- helpers
    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    def _online(self, now):
        return bool(self.bridge) and (now - self.bridge['last_contact']) <= self.stale_seconds

    def _expire_write(self, now):
        w = self.write
        if w and now > w['deadline']:
            self.state.update(write_status='ERROR', write_error='Timed out waiting for the NFC tag / bridge')
            w['event'].set()
            self.write = None
            self._bump()

    def _touch(self, session):
        if not self.bridge or self.bridge['session'] != session:
            raise UnknownSession()
        self.bridge['last_contact'] = time.time()

    # ------------------------------------------------- browser-facing (same API as nfc_reader)
    def get_state(self):
        with self.cond:
            now = time.time()
            self._expire_write(now)
            st = dict(self.state)
            online = self._online(now)
            b = self.bridge
            if not b:
                st.update(available=False, status='WAITING FOR LOCAL NFC BRIDGE', port=None)
            elif not online:
                st.update(available=False, status='LOCAL NFC BRIDGE OFFLINE', port=None)
            else:
                r = b['reader']
                st.update(available=bool(r.get('available')), status=r.get('status') or 'UNKNOWN',
                          port=('SIMULATED (no hardware)' if b['simulated'] else
                                (f"{r['port']} via Local Bridge" if r.get('port') else None)),
                          last_error=r.get('error'))
            st['bridge'] = {'online': online, 'id': b['bridge_id'] if b else None, 'simulated': bool(b and b['simulated']),
                            'version': b['version'] if b else None,
                            'seconds_since_contact': round(now - b['last_contact'], 1) if b else None}
            return st

    def start_pairing(self):
        with self.cond:
            self.last_open.update({'uid': None, 'time': 0.0})
            self.state.update(pairing=True, pair_uid=None, pair_event_id=None, pair_token=None)
            self._bump()

    def cancel_pairing(self):
        with self.cond:
            self.state.update(pairing=False, pair_uid=None, pair_event_id=None, pair_token=None)
            self._bump()

    def request_nfc_url_write(self, uid, target_url, wait_seconds=8.0):
        normalized = str(uid or '').strip().upper()
        url = str(target_url or '').strip()
        with self.cond:
            now = time.time()
            if not self._online(now) or not (self.bridge['reader'].get('available')):
                self.state.update(write_status='NFC_UNAVAILABLE', write_uid=normalized, write_url=url,
                                  write_error='Local NFC Bridge or PN532 reader is not available')
                return self.get_state()
            if self.write:
                self.write['event'].set()
            ev = threading.Event()
            self.write_counter += 1
            self.write = {'request_id': uuid.uuid4().hex, 'uid': normalized, 'url': url, 'event': ev,
                          'deadline': now + self.write_ttl}
            self.state.update(write_status='WAITING_FOR_TAG', write_uid=normalized, write_url=url, write_error=None,
                              write_event_id=self.write_counter)
            self._bump()
        ev.wait(max(0.0, float(wait_seconds)))
        return self.get_state()

    # ------------------------------------------------------------ bridge-facing
    def bridge_hello(self, bridge_id, instance_id, info):
        with self.cond:
            now = time.time()
            if info.get('simulated') and not self.allow_simulated:
                raise SimulatedNotAllowed()
            b = self.bridge
            if b and self._online(now) and b['instance_id'] != instance_id:
                raise BridgeBusy(f"another bridge instance ({b['bridge_id']}) is connected")
            reader = dict(info.get('reader') or {})
            self.bridge = {'session': uuid.uuid4().hex, 'bridge_id': bridge_id, 'instance_id': instance_id,
                           'last_contact': now, 'version': str(info.get('version', '')),
                           'simulated': bool(info.get('simulated')), 'reader': reader, 'last_seq': 0}
            self._bump()
            return self.bridge['session']

    def _desired(self, now):
        w = self.write
        return {'version': self.version, 'pairing': bool(self.state['pairing']), 'server_time': int(now),
                'write': ({'request_id': w['request_id'], 'uid': w['uid'], 'url': w['url'],
                           'expires_in': max(1, int(w['deadline'] - now))} if w else None)}

    def bridge_poll(self, session, since_version, wait):
        deadline = time.time() + max(0.0, wait)
        with self.cond:
            self._touch(session)
            while True:
                now = time.time()
                self._expire_write(now)
                if self.version != since_version or now >= deadline:
                    self._touch(session)
                    return self._desired(now)
                self.cond.wait(timeout=min(1.0, deadline - now))
                if not self.bridge or self.bridge['session'] != session:
                    raise UnknownSession()

    def bridge_bye(self, session):
        with self.cond:
            if self.bridge and self.bridge['session'] == session:
                self.bridge['last_contact'] = 0.0
                self.bridge['reader'] = dict(self.bridge['reader'], available=False)

    def bridge_event(self, session, ev):
        """Returns True if applied, False if it was a duplicate (retry) event."""
        with self.cond:
            self._touch(session)
            seq = int(ev.get('seq') or 0)
            if seq and seq <= self.bridge['last_seq']:
                return False
            if seq:
                self.bridge['last_seq'] = seq
            kind = ev.get('type')
            if kind == 'reader_state':
                self.bridge['reader'] = {k: ev.get(k) for k in ('available', 'status', 'port', 'error')}
            elif kind == 'tag':
                self._on_tag(str(ev.get('uid') or '').strip().upper())
            elif kind == 'write_result':
                self._on_write_result(ev)
            return True

    def _on_tag(self, uid):
        if not uid:
            return
        now = time.time()
        pairing_now = bool(self.state['pairing'])
        event_id = int(self.state['event_id']) + 1
        if uid == self.last_open['uid'] and now - self.last_open['time'] < DEBOUNCE_SECONDS and not pairing_now:
            return
        self.last_open.update({'uid': uid, 'time': now})
        self.state.update(last_uid=uid, last_seen=time.strftime('%Y-%m-%d %H:%M:%S'), event_id=event_id)
        if pairing_now:
            self.state.update(pair_uid=uid, pair_event_id=event_id, pairing=False)
            self._bump()

    def _on_write_result(self, ev):
        w = self.write
        if not w or ev.get('request_id') != w['request_id']:
            return                                  # stale / already timed out
        ok = ev.get('status') == 'SUCCESS'
        self.state.update(write_status='SUCCESS' if ok else 'ERROR', write_error=None if ok else str(ev.get('error') or 'write failed')[:300],
                          last_uid=w['uid'], last_seen=time.strftime('%Y-%m-%d %H:%M:%S'))
        w['event'].set()
        self.write = None
        self._bump()


hub = NfcHub()
get_state = lambda: hub.get_state()
start_pairing = lambda: hub.start_pairing()
cancel_pairing = lambda: hub.cancel_pairing()
request_nfc_url_write = lambda uid, target_url, wait_seconds=8.0: hub.request_nfc_url_write(uid, target_url, wait_seconds)
