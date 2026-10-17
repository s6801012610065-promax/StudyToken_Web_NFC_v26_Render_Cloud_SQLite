"""PN532 access for the Local NFC Bridge.

Pn532Reader  = REAL hardware via nfcpy (same approach as the original nfc_reader.py: com:COMx:pn532).
SimulatedReader = NO hardware, only for automated tests (--simulate). It reports itself as simulated.
"""
from __future__ import annotations
import json, logging, os, threading, time

log = logging.getLogger('bridge.reader')


class ReaderBase:
    def __init__(self, on_tag, on_state, on_write_result):
        self.on_tag, self.on_state, self.on_write_result = on_tag, on_state, on_write_result
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pending = None            # {'request_id','uid','url','deadline'}
        self.thread = None

    def request_write(self, request_id, uid, url, expires_in):
        with self._lock:
            if self._pending and self._pending['request_id'] == request_id:
                return
            self._pending = {'request_id': request_id, 'uid': uid.upper(), 'url': url, 'deadline': time.time() + expires_in}

    def clear_write(self):
        with self._lock:
            self._pending = None

    def _take_pending_for(self, uid):
        with self._lock:
            p = self._pending
            if p and time.time() > p['deadline']:
                self._pending = None
                p = None
            return p if (p and p['uid'] == uid) else None

    def _finish_write(self, p, ok, err=None):
        with self._lock:
            if self._pending and self._pending['request_id'] == p['request_id']:
                self._pending = None
        self.on_write_result(p['request_id'], ok, err, p['uid'])

    def start(self):
        self.thread = threading.Thread(target=self._run, name='reader', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()


class Pn532Reader(ReaderBase):
    def __init__(self, *a, com_port='auto', max_port=30, **kw):
        super().__init__(*a, **kw)
        self.com_port, self.max_port = (com_port or 'auto'), max_port

    def _open(self, nfc, name):
        try:
            return nfc.ContactlessFrontend(f'com:{name}:pn532'), None
        except Exception as e:
            return None, str(e)

    def _find(self, nfc):
        if self.com_port.lower() != 'auto':
            name = self.com_port.upper()
            clf, err = self._open(nfc, name)
            return (clf, name) if clf else (None, None)
        for n in range(1, self.max_port + 1):       # same COM1..COM30 scan as the original reader
            if self.stop_event.is_set():
                return None, None
            clf, _ = self._open(nfc, f'COM{n}')
            if clf:
                return clf, f'COM{n}'
        return None, None

    def _write_ndef_url(self, tag, url):
        import ndef
        if tag.ndef is None:
            raise RuntimeError('Tag is not NDEF formatted or does not support NDEF')
        if not tag.ndef.is_writeable:
            raise RuntimeError('Tag is locked and cannot be written')
        rec = ndef.UriRecord(url)
        try:
            tag.ndef.records = [rec]
        except Exception as first:
            try:
                empty = int(tag.ndef.length) == 0
            except Exception:
                empty = False
            if not empty:
                raise first
            if tag.ndef.format() is False:
                raise first
            tag.ndef.records = [rec]

    def _run(self):
        try:
            import nfc
        except Exception as exc:
            self.on_state(False, 'NFC LIBRARY NOT INSTALLED', None, str(exc))
            log.error('nfcpy not installed: %s  (pip install nfcpy)', exc)
            return
        clf = None
        try:
            while not self.stop_event.is_set():
                if clf is None:
                    self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                    clf, port = self._find(nfc)
                    if clf:
                        log.info('PN532 found on %s', port)
                        self.on_state(True, 'READY', port, None)
                        continue
                    self.on_state(False, 'WAITING FOR NFC READER', None, None)
                    self.stop_event.wait(1.5)
                    continue

                def on_connect(tag):
                    uid = tag.identifier.hex(':').upper()
                    p = self._take_pending_for(uid)
                    if p:
                        try:
                            self._write_ndef_url(tag, p['url'])
                            log.info('NDEF URL written to %s', uid)
                            self._finish_write(p, True)
                        except Exception as exc:
                            log.error('write failed for %s: %s', uid, exc)
                            self._finish_write(p, False, str(exc))
                        return False
                    self.on_tag(uid)
                    return False

                try:
                    clf.connect(rdwr={'on-connect': on_connect}, terminate=lambda: self.stop_event.is_set())
                    if not self.stop_event.is_set():
                        raise RuntimeError('reader loop ended')
                except Exception as exc:
                    log.warning('reader disconnected/error: %s', exc)
                    self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                    try:
                        clf.close()
                    except Exception:
                        pass
                    clf = None
                    self.stop_event.wait(0.5)
        finally:
            if clf:
                try:
                    clf.close()
                except Exception:
                    pass
            self.on_state(False, 'STOPPED', None, None)


class SimulatedReader(ReaderBase):
    """Test double. Taps are read from a text file (one UID per line appended by the test);
    written URLs are stored in <tags_dir>/<UID>.json so tests can verify them. NOT hardware."""
    def __init__(self, *a, taps_file=None, tags_dir=None, fail_writes=False, **kw):
        super().__init__(*a, **kw)
        self.taps_file, self.tags_dir, self.fail_writes = taps_file, tags_dir, fail_writes
        self._offset = 0
        self._present = None

    def _run(self):
        self.on_state(True, 'READY', 'SIM', None)
        while not self.stop_event.is_set():
            try:
                if self.taps_file and os.path.exists(self.taps_file):
                    with open(self.taps_file, 'r') as f:
                        f.seek(self._offset)
                        lines = f.read().splitlines()
                        self._offset = f.tell()
                    for ln in lines:
                        self._present = ln.strip().upper() or None
                        if self._present:
                            self._tap(self._present)
                if self._present:               # tag stays on the reader -> a pending write can still hit it
                    self._try_write(self._present)
            except Exception as e:
                log.error('sim error: %s', e)
            self.stop_event.wait(0.1)

    def _try_write(self, uid):
        p = self._take_pending_for(uid)
        if not p:
            return False
        if self.fail_writes:
            self._finish_write(p, False, 'simulated write failure')
        else:
            if self.tags_dir:
                os.makedirs(self.tags_dir, exist_ok=True)
                with open(os.path.join(self.tags_dir, uid.replace(':', '') + '.json'), 'w') as f:
                    json.dump({'uid': uid, 'url': p['url']}, f)
            self._finish_write(p, True)
        return True

    def _tap(self, uid):
        if not self._try_write(uid):
            self.on_tag(uid)
