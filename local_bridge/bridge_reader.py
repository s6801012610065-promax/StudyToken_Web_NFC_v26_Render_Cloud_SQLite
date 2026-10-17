"""PN532 access for the Local NFC Bridge.

Pn532Reader  = REAL hardware. Default backend "raw": own PN532 UART driver (pn532_raw.py) - the UID comes straight
               from the chip, so NTAG213/215/216 always give a UID and NDEF URLs are written directly.
               Backend "nfcpy" (legacy) or "auto" (raw first, nfcpy as fallback) via READER_BACKEND.
SimulatedReader = NO hardware, only for automated tests (--simulate). It reports itself as simulated.
"""
from __future__ import annotations
import json, logging, os, threading, time
try:
    from . import pn532_raw as raw
except ImportError:            # run as a plain script from inside local_bridge/
    import pn532_raw as raw

log = logging.getLogger('bridge.reader')


class ReaderBase:
    def __init__(self, on_tag, on_state, on_write_result):
        self.on_tag, self.on_state, self.on_write_result = on_tag, on_state, on_write_result
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pending = None            # {'request_id','uid','url','deadline'}
        self.thread = None
        self._rescan = False

    def rescan(self):
        """Announce a tag that is already on the reader once more (used when pairing starts)."""
        self._rescan = True

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
    def __init__(self, *a, com_port='auto', max_port=30, backend='raw', baud=115200, **kw):
        super().__init__(*a, **kw)
        self.com_port, self.max_port = (com_port or 'auto'), max_port
        self.backend = (backend or 'raw').lower()
        self.baud = int(baud or 115200)
        self._last_port = None

    # ------------------------------------------------------------------ raw backend (default)
    def _announce(self, pn, target):
        """A tag was just detected: perform a pending write for it, otherwise report its UID."""
        uid = raw.uid_str(target['uid'])
        p = self._take_pending_for(uid)
        if p:
            self._do_write(pn, target, p)
        else:
            log.info('tag %s (%s)', uid, raw.describe_tag(pn, target))
            self.on_tag(uid)

    def _do_write(self, pn, target, p):
        try:
            raw.write_ndef_url(pn, target, p['url'])
            log.info('NDEF URL written to %s', p['uid'])
            self._finish_write(p, True)
        except raw.Pn532Timeout:
            raise
        except Exception as exc:
            log.error('write failed for %s: %s', p['uid'], exc)
            self._finish_write(p, False, str(exc))

    def _still_there(self, pn, target):
        if raw.is_type2(target):
            try:
                raw.t2_read(pn, 0)
                return True
            except raw.Pn532Timeout:
                raise
            except raw.Pn532Error:
                pass
        t = pn.list_passive()
        return bool(t) and t['uid'] == target['uid']

    def _serve_raw(self, pn):
        """Poll the PN532 until it disappears (raises) or stop is requested."""
        present = None
        while not self.stop_event.is_set():
            if present is None:
                present = pn.list_passive()
                if present is None:
                    self.stop_event.wait(0.05)
                else:
                    self._rescan = False
                    self._announce(pn, present)
                continue
            if not self._still_there(pn, present):
                present = None
                pn.release()
                continue
            uid = raw.uid_str(present['uid'])
            p = self._take_pending_for(uid)           # Save Course pressed while the tag is already on the reader
            if p:
                self._do_write(pn, present, p)
            elif self._rescan:
                self._rescan = False
                self.on_tag(uid)
            self.stop_event.wait(0.1)

    def _run_raw(self, with_nfcpy_fallback=False):
        nfc = None
        if with_nfcpy_fallback:
            try:
                import nfc
            except Exception:
                nfc = None
        try:
            while not self.stop_event.is_set():
                self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                pn, port, errs = raw.find_pn532(self.com_port, self.baud, self.max_port, self.stop_event, self._last_port)
                if pn:
                    self._last_port = port
                    log.info('PN532 (firmware %s) found on %s', getattr(pn, 'firmware', '?'), port)
                    self.on_state(True, 'READY', port, None)
                    try:
                        self._serve_raw(pn)
                    except Exception as exc:
                        log.warning('reader disconnected/error: %s', exc)
                    finally:
                        pn.close()
                    if not self.stop_event.is_set():
                        self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                        self.stop_event.wait(0.5)
                    continue
                if nfc:                                  # legacy fallback for readers the raw driver cannot talk to (e.g. USB ones)
                    clf, nport = self._find(nfc)
                    if clf:
                        log.info('reader found by nfcpy on %s', nport)
                        self.on_state(True, 'READY', nport, None)
                        self._serve_nfcpy(clf)
                        continue
                hint = '; '.join(f'{k}: {v}' for k, v in list(errs.items())[:3])[:240] or None
                self.on_state(False, 'WAITING FOR NFC READER', None, hint)
                self.stop_event.wait(1.5)
        finally:
            self.on_state(False, 'STOPPED', None, None)

    # ------------------------------------------------------------------ nfcpy backend (legacy)
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

    def _serve_nfcpy(self, clf):
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
            while not self.stop_event.is_set():
                clf.connect(rdwr={'on-connect': on_connect}, terminate=lambda: self.stop_event.is_set())
        except Exception as exc:
            log.warning('reader disconnected/error: %s', exc)
        finally:
            try:
                clf.close()
            except Exception:
                pass
            if not self.stop_event.is_set():
                self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                self.stop_event.wait(0.5)

    def _run_nfcpy(self):
        try:
            import nfc
        except Exception as exc:
            self.on_state(False, 'NFC LIBRARY NOT INSTALLED', None, str(exc))
            log.error('nfcpy not installed: %s  (pip install nfcpy)', exc)
            return
        try:
            while not self.stop_event.is_set():
                self.on_state(False, 'SEARCHING FOR NFC READER...', None, None)
                clf, port = self._find(nfc)
                if not clf:
                    self.on_state(False, 'WAITING FOR NFC READER', None, None)
                    self.stop_event.wait(1.5)
                    continue
                log.info('reader found on %s', port)
                self.on_state(True, 'READY', port, None)
                self._serve_nfcpy(clf)
        finally:
            self.on_state(False, 'STOPPED', None, None)

    def _run(self):
        if self.backend == 'nfcpy':
            self._run_nfcpy()
        else:
            self._run_raw(with_nfcpy_fallback=(self.backend == 'auto'))


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
