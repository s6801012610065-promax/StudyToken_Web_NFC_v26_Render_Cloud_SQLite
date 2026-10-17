"""PN532 raw driver tested against a byte-level PN532 + NTAG simulator (no hardware, no pyserial needed).
The simulator speaks the real PN532 UART frame format, so framing/checksums/commands are exercised for real."""
import os, sys, threading, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'local_bridge'))
import pn532_raw as raw
from bridge_reader import Pn532Reader


class FakeTag:
    def __init__(self, uid, pages=135, cc=(0xE1, 0x10, 0x3E, 0x00), sak=0x00, locked=False):
        self.uid, self.sak, self.mem = bytes(uid), sak, [bytearray(4) for _ in range(pages)]
        self.mem[3][:] = bytes(cc)
        self.locked = locked
        self.version = bytes([0, 4, 4, 2, 1, 0, 0x11, 3])


class FakePn532Serial:
    """Behaves like a PN532 behind a USB-UART adapter."""
    def __init__(self):
        self.tag = None
        self.rx = bytearray()      # bytes the host will read
        self.tx = bytearray()      # bytes the host wrote
        self.awake = False
        self.closed = False
        self.timeout = 0.05
        self.writes = 0

    # -- pyserial-like API
    def write(self, b): self.tx += b; self._parse()
    def flush(self): pass
    def reset_input_buffer(self): self.rx.clear()
    def close(self): self.closed = True
    def read(self, n):
        if not self.rx:
            time.sleep(0.005); return b''
        out = bytes(self.rx[:n]); del self.rx[:n]; return out

    # -- PN532
    def _send(self, payload):
        self.rx += raw.build_frame(bytes([0xD5]) + payload)

    def _parse(self):
        while True:
            i = self.tx.find(b'\x00\x00\xff')
            if i < 0 or len(self.tx) < i + 5: return
            ln = self.tx[i + 3]
            if len(self.tx) < i + 5 + ln + 2: return
            body = bytes(self.tx[i + 5:i + 5 + ln]); assert (sum(body) + self.tx[i + 5 + ln]) & 0xFF == 0
            del self.tx[:i + 5 + ln + 2]
            self.rx += raw.ACK
            assert body[0] == 0xD4
            self._cmd(body[1], body[2:])

    def _cmd(self, c, p):
        if c == 0x02: self._send(b'\x03\x32\x01\x06\x07')
        elif c in (0x14, 0x32): self._send(bytes([c + 1]))
        elif c == 0x52: self._send(b'\x53\x00')
        elif c == 0x4A:
            t = self.tag
            if not t: self._send(b'\x4B\x00')
            else: self._send(b'\x4B\x01\x01\x00\x44' + bytes([t.sak, len(t.uid)]) + t.uid)
        elif c == 0x40:
            t = self.tag
            if not t: return self._send(b'\x41\x01')
            d = p[1:]
            if d[0] == 0x30:
                pg = d[1]; data = b''.join(bytes(t.mem[(pg + k) % len(t.mem)]) for k in range(4)); self._send(b'\x41\x00' + data)
            elif d[0] == 0xA2:
                if t.locked or d[1] >= len(t.mem): return self._send(b'\x41\x14')
                t.mem[d[1]][:] = d[2:6]; self.writes += 1; self._send(b'\x41\x00')
            elif d[0] == 0x60: self._send(b'\x41\x00' + t.version)
            else: self._send(b'\x41\x01')


def open_fake(tag=None):
    s = FakePn532Serial(); s.tag = tag
    pn = raw.Pn532(s); pn.init(); return pn, s


class Driver(unittest.TestCase):
    def test_init_and_firmware(self):
        pn, _ = open_fake(); self.assertEqual(pn.firmware, '1.6')

    def test_uid_7_byte_ntag215(self):
        uid = bytes.fromhex('04A1B2C3D4E5F6')
        pn, _ = open_fake(FakeTag(uid))
        t = pn.list_passive(); self.assertEqual(raw.uid_str(t['uid']), '04:A1:B2:C3:D4:E5:F6')
        self.assertIn('NTAG215', raw.describe_tag(pn, t))

    def test_uid_4_byte_and_no_tag(self):
        pn, s = open_fake()
        self.assertIsNone(pn.list_passive())
        s.tag = FakeTag(bytes.fromhex('DEADBEEF'), sak=0x08)
        t = pn.list_passive(); self.assertEqual(raw.uid_str(t['uid']), 'DE:AD:BE:EF')
        self.assertFalse(raw.is_type2(t))
        with self.assertRaises(raw.Pn532Error): raw.write_ndef_url(pn, t, 'https://x.example/nfc/STK-AAAAAAAA')

    def test_write_ntag215_and_ntag216_roundtrip(self):
        for pages, cap in ((135, 0x3E), (231, 0x6D)):
            url = 'https://study.onrender.com/nfc/STK-1A2B3C4D'
            pn, s = open_fake(FakeTag(bytes.fromhex('0411223344556677'), pages=pages, cc=(0xE1, 0x10, cap, 0)))
            t = pn.list_passive(); raw.write_ndef_url(pn, t, url)
            self.assertEqual(raw.t2_read_url(pn), url)
            # exact on-tag bytes: TLV 03 len, D1 01 len 'U' 04 'study...'
            flat = b''.join(bytes(p) for p in s.tag.mem[4:12])
            self.assertEqual(flat[:2], bytes([0x03, len(raw.ndef_uri_message(url))]))
            self.assertEqual(flat[2:6], bytes([0xD1, 0x01, len(url) - 8 + 1, 0x55]))
            self.assertEqual(flat[6], 0x04)                      # https:// abbreviation

    def test_write_formats_blank_tag(self):
        pn, s = open_fake(FakeTag(bytes.fromhex('04AABBCCDDEEFF'), cc=(0, 0, 0, 0)))
        t = pn.list_passive(); raw.write_ndef_url(pn, t, 'https://a.example/nfc/STK-00000001')
        self.assertEqual(bytes(s.tag.mem[3]), bytes([0xE1, 0x10, 0x3E, 0x00]))

    def test_locked_tag_reports_error(self):
        pn, _ = open_fake(FakeTag(bytes.fromhex('0411223344556677'), cc=(0xE1, 0x10, 0x3E, 0x0F)))
        with self.assertRaisesRegex(raw.Pn532Error, 'locked'): raw.write_ndef_url(pn, pn.list_passive(), 'https://a.example/nfc/STK-00000001')

    def test_write_failure_is_reported_not_faked(self):
        pn, _ = open_fake(FakeTag(bytes.fromhex('0411223344556677'), locked=True))
        with self.assertRaises(raw.Pn532Error): raw.write_ndef_url(pn, pn.list_passive(), 'https://a.example/nfc/STK-00000001')

    def test_no_ack_gives_helpful_error(self):
        class Dead(FakePn532Serial):
            def write(self, b): pass
        with self.assertRaisesRegex(raw.Pn532Timeout, 'no ACK'): raw.Pn532(Dead()).init()


class ReaderLoop(unittest.TestCase):
    """Pn532Reader (the object used by the bridge and by local mode) driven by the simulator."""
    def setUp(self):
        self.tags, self.states, self.results = [], [], []
        self.ser = FakePn532Serial()
        self._orig = raw.find_pn532
        self.r = Pn532Reader(self.tags.append, lambda *a: self.states.append(a), lambda *a: self.results.append(a), com_port='auto')
    def tearDown(self):
        raw.find_pn532 = self._orig; self.r.stop(); time.sleep(0.3)

    def start(self):
        pn_init = raw.Pn532(self.ser); pn_init.init()
        raw.find_pn532 = lambda *a, **k: (pn_init, 'COM9', {})
        self.r.start()
        self.wait(lambda: any(s[0] for s in self.states))

    def wait(self, cond, t=4.0):
        end = time.time() + t
        while time.time() < end:
            if cond(): return True
            time.sleep(0.02)
        self.fail('timeout')

    def test_uid_reported_once_per_tap_and_again_after_removal(self):
        self.start()
        self.ser.tag = FakeTag(bytes.fromhex('0411223344556677'))
        self.wait(lambda: self.tags == ['04:11:22:33:44:55:66:77'])
        time.sleep(0.6); self.assertEqual(len(self.tags), 1)             # held on the reader: no flood
        self.ser.tag = None; time.sleep(0.5)
        self.ser.tag = FakeTag(bytes.fromhex('0411223344556677'))
        self.wait(lambda: len(self.tags) == 2)

    def test_rescan_reports_tag_already_resting(self):
        self.start()
        self.ser.tag = FakeTag(bytes.fromhex('0411223344556677')); self.wait(lambda: len(self.tags) == 1)
        self.r.rescan(); self.wait(lambda: len(self.tags) == 2)

    def test_pending_write_executes_while_tag_rests_and_is_not_reported_as_tap(self):
        self.start()
        tag = FakeTag(bytes.fromhex('0411223344556677')); self.ser.tag = tag
        self.wait(lambda: len(self.tags) == 1)
        url = 'https://study.onrender.com/nfc/STK-AABBCCDD'
        self.r.request_write('rid1', '04:11:22:33:44:55:66:77', url, 30)
        self.wait(lambda: self.results)
        self.assertEqual(self.results[0][:3], ('rid1', True, None))
        self.assertEqual(len(self.tags), 1)
        pn = raw.Pn532(self.ser); self.assertEqual(raw.t2_read_url(pn), url)

    def test_write_error_surfaces(self):
        self.start()
        self.ser.tag = FakeTag(bytes.fromhex('0411223344556677'), locked=True); self.wait(lambda: len(self.tags) == 1)
        self.r.request_write('rid2', '04:11:22:33:44:55:66:77', 'https://study.onrender.com/nfc/STK-AABBCCDD', 30)
        self.wait(lambda: self.results); self.assertFalse(self.results[0][1]); self.assertTrue(self.results[0][2])

    def test_unplug_goes_back_to_searching(self):
        self.start()
        self.ser.read = lambda n: (_ for _ in ()).throw(OSError('device gone'))
        self.wait(lambda: any(s[1].startswith(('SEARCHING', 'WAITING')) for s in self.states[1:]))


if __name__ == '__main__':
    unittest.main()
