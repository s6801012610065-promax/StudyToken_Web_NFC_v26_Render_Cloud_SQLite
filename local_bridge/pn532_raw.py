"""Minimal, dependency-light PN532 driver for UART/HSU boards (USB-serial adapters: CH340, CP210x, FTDI ...).

Why this exists: reading the UID must NOT depend on NDEF parsing. nfcpy only reports a tag after it has
fully "activated" it as a known tag type, so some NTAG215/216 (and clones) never produce a UID there.
Here the UID comes straight from the PN532 InListPassiveTarget answer, and NTAG215/216/213 (NFC Forum
Type 2) tags are written directly with READ/WRITE commands.
"""
from __future__ import annotations
import logging, sys, time

log = logging.getLogger('bridge.pn532')

ACK = b'\x00\x00\xff\x00\xff\x00'
WAKEUP = b'\x55\x55' + b'\x00' * 14
URI_PREFIXES = ['', 'http://www.', 'https://www.', 'http://', 'https://']   # NFC Forum URI record abbreviations used here
T2_NAMES = {0x12: 'NTAG213', 0x3E: 'NTAG215', 0x6D: 'NTAG216', 0x06: 'MIFARE Ultralight'}
GETVERSION_STORAGE = {0x0F: ('NTAG213', 144), 0x11: ('NTAG215', 496), 0x13: ('NTAG216', 872)}


class Pn532Error(Exception): ...
class Pn532Timeout(Pn532Error): ...
class TagLost(Pn532Error): ...


def build_frame(data: bytes) -> bytes:
    n = len(data)
    return bytes([0, 0, 0xFF, n & 0xFF, (0x100 - n) & 0xFF]) + data + bytes([(0x100 - sum(data)) & 0xFF, 0])


class Pn532:
    def __init__(self, ser):
        self.ser = ser

    # ------------------------------------------------------------ transport
    @classmethod
    def open(cls, port, baud=115200):
        import serial                      # pyserial
        ser = serial.Serial(port, baud, timeout=0.05, write_timeout=1.0)
        pn = cls(ser)
        try:
            pn.init()
        except Exception:
            pn.close()
            raise
        return pn

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _read(self, n, deadline):
        buf = b''
        while len(buf) < n:
            if time.monotonic() > deadline:
                raise Pn532Timeout('timeout waiting for PN532')
            buf += self.ser.read(n - len(buf))
        return buf

    def _read_frame(self, deadline):
        win = b''
        while win != b'\x00\x00\xff':                       # preamble + start code
            win = (win + self._read(1, deadline))[-3:]
        ln, lcs = self._read(2, deadline)
        if ln == 0x00 and lcs == 0xFF:
            self._read(1, deadline); return 'ack', b''
        if ln == 0xFF and lcs == 0x00:
            self._read(1, deadline); return 'nack', b''
        if ln == 0xFF and lcs == 0xFF:
            raise Pn532Error('extended PN532 frame not supported')
        if (ln + lcs) & 0xFF:
            raise Pn532Error('bad PN532 length checksum')
        data = self._read(ln, deadline)
        dcs, _post = self._read(2, deadline)
        if (sum(data) + dcs) & 0xFF:
            raise Pn532Error('bad PN532 data checksum')
        if ln == 1 and data[0] == 0x7F:
            raise Pn532Error('PN532 reported a syntax error')
        return 'data', data

    def command(self, cmd, params=b'', timeout=1.0):
        frame = build_frame(b'\xD4' + bytes([cmd]) + bytes(params))
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame); self.ser.flush()
        except Exception as e:
            raise Pn532Error(f'serial write failed: {e}')
        deadline = time.monotonic() + 0.6
        while True:                                          # wait for ACK
            try:
                kind, _ = self._read_frame(deadline)
            except Pn532Timeout:
                raise Pn532Timeout('no ACK from PN532 (wrong COM port / baud rate / board not in UART-HSU mode?)')
            if kind == 'ack':
                break
            if kind == 'nack':
                raise Pn532Error('PN532 sent NACK')
        deadline = time.monotonic() + timeout
        while True:                                          # wait for the answer
            kind, data = self._read_frame(deadline)
            if kind == 'data' and len(data) >= 2 and data[0] == 0xD5 and data[1] == cmd + 1:
                return data[2:]

    # ------------------------------------------------------------ chip
    def wakeup(self):
        try:
            self.ser.write(WAKEUP); self.ser.flush()
        except Exception as e:
            raise Pn532Error(f'serial write failed: {e}')
        time.sleep(0.05)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def init(self):
        last = None
        for _ in range(3):
            try:
                self.wakeup()
                r = self.command(0x02, timeout=0.6)          # GetFirmwareVersion
                if len(r) < 4 or r[0] != 0x32:
                    raise Pn532Error(f'not a PN532 (IC=0x{r[0]:02X})' if r else 'empty firmware answer')
                self.firmware = f'{r[1]}.{r[2]}'
                break
            except Pn532Error as e:
                last = e
        else:
            raise last
        self.command(0x14, b'\x01\x14\x01')                  # SAMConfiguration: normal mode
        self.command(0x32, b'\x05\xFF\x01\x03')              # RFConfiguration: passive activation tries=3 (call returns quickly)
        return self.firmware

    def list_passive(self):
        """Poll for one ISO14443A tag. Returns {'uid','atqa','sak'} or None."""
        r = self.command(0x4A, b'\x01\x00', timeout=1.5)
        if not r or r[0] == 0 or len(r) < 6:
            return None
        n = r[5]
        return {'uid': bytes(r[6:6 + n]), 'atqa': bytes(r[2:4]), 'sak': r[4]}

    def release(self):
        try:
            self.command(0x52, b'\x00', timeout=0.5)
        except Pn532Error:
            pass

    def data_exchange(self, data):
        r = self.command(0x40, b'\x01' + bytes(data), timeout=1.0)
        err = r[0] & 0x3F
        if err == 0x01:
            raise TagLost('tag stopped answering')
        if err:
            raise Pn532Error(f'tag error 0x{err:02X}')
        return bytes(r[1:])


def uid_str(uid: bytes) -> str:
    return uid.hex(':').upper()


def is_type2(target) -> bool:
    return target['sak'] == 0x00                              # NTAG21x / MIFARE Ultralight


# ---------------------------------------------------------------- NFC Forum Type 2 (NTAG213/215/216)
def t2_read(pn, page):
    return pn.data_exchange(bytes([0x30, page]))[:16]


def t2_write(pn, page, four):
    pn.data_exchange(bytes([0xA2, page]) + bytes(four))


def describe_tag(pn, target):
    """Human readable tag type, from the Capability Container (does not disturb the tag)."""
    if not is_type2(target):
        return {0x08: 'MIFARE Classic 1K', 0x18: 'MIFARE Classic 4K', 0x20: 'ISO-DEP (DESFire/smartcard)'}.get(target['sak'], f"SAK 0x{target['sak']:02X}") + ' (UID only, NDEF write not supported)'
    try:
        cc = t2_read(pn, 3)[:4]
    except Pn532Error as e:
        return f'Type 2 tag (cannot read: {e})'
    if cc[0] != 0xE1:
        return 'Type 2 tag, blank/not NDEF formatted (will be formatted on first write)' if cc == b'\0\0\0\0' else 'Type 2 tag, not NDEF'
    return f"{T2_NAMES.get(cc[2], 'Type 2')} ({cc[2] * 8} bytes NDEF)" + (' - LOCKED' if cc[3] & 0x0F else '')


def ndef_uri_message(url: str) -> bytes:
    code, rest = 0, url
    for i in (2, 1, 4, 3):                                    # longest prefixes first
        if url.startswith(URI_PREFIXES[i]):
            code, rest = i, url[len(URI_PREFIXES[i]):]; break
    payload = bytes([code]) + rest.encode('utf-8')
    if len(payload) > 255:
        raise Pn532Error('URL too long')
    return bytes([0xD1, 0x01, len(payload), 0x55]) + payload  # MB|ME|SR, TNF=well-known, type "U"


def ndef_tlv(message: bytes) -> bytes:
    n = len(message)
    head = bytes([0x03, n]) if n < 255 else bytes([0x03, 0xFF, n >> 8, n & 0xFF])
    return head + message + b'\xFE'


def parse_uri_from_pages(raw: bytes):
    """Return the URL of the first NDEF URI record in TLV data, or None."""
    i = 0
    while i < len(raw):
        t = raw[i]
        if t == 0x00: i += 1; continue
        if t == 0xFE: return None
        if i + 1 >= len(raw): return None
        ln, hdr = raw[i + 1], 2
        if ln == 0xFF:
            if i + 3 >= len(raw): return None
            ln, hdr = (raw[i + 2] << 8) | raw[i + 3], 4
        if t == 0x03:
            m = raw[i + hdr:i + hdr + ln]
            if len(m) >= 5 and m[0] & 0x07 == 0x01 and m[3:4] == b'U':
                pl = m[2]; p = m[4:4 + pl]
                if p and p[0] < len(URI_PREFIXES):
                    return URI_PREFIXES[p[0]] + p[1:].decode('utf-8', 'replace')
            return None
        i += hdr + ln
    return None


def t2_read_url(pn, max_pages=40):
    raw = b''
    for page in range(4, 4 + max_pages, 4):
        raw += t2_read(pn, page)
        if b'\xFE' in raw:
            break
    return parse_uri_from_pages(raw)


def write_ndef_url(pn, target, url, verify=True):
    """Write a single NDEF URI record to an NTAG213/215/216 (Type 2) tag that is already selected."""
    if not is_type2(target):
        raise Pn532Error(f"Tag type not supported for NDEF write (SAK 0x{target['sak']:02X}) - use NTAG215/216")
    cc = t2_read(pn, 3)[:4]
    if cc[0] != 0xE1:
        if cc != b'\0\0\0\0':
            raise Pn532Error('Tag is not NDEF formatted or does not support NDEF')
        try:
            v = pn.data_exchange(b'\x60')                     # GET_VERSION (NTAG only)
            name, cap = GETVERSION_STORAGE[v[6]] if len(v) >= 8 and v[6] in GETVERSION_STORAGE else (None, 0)
        except Pn532Error:
            name, cap = None, 0
        if not cap:
            raise Pn532Error('Blank tag of unknown size: cannot format')
        cc = bytes([0xE1, 0x10, cap // 8, 0x00])
        t2_write(pn, 3, cc)
    if cc[3] & 0x0F:
        raise Pn532Error('Tag is locked and cannot be written')
    tlv = ndef_tlv(ndef_uri_message(url))
    if len(tlv) > cc[2] * 8:
        raise Pn532Error('URL does not fit on this tag')
    tlv += b'\0' * (-len(tlv) % 4)
    last = None
    for attempt in range(3):
        try:
            for i in range(0, len(tlv), 4):
                t2_write(pn, 4 + i // 4, tlv[i:i + 4])
            if verify and t2_read_url(pn) != url:
                raise Pn532Error('Verification failed: tag content differs after write')
            return
        except TagLost:
            raise Pn532Error('Tag moved away during write - hold the tag still on the reader')
        except Pn532Error as e:
            last = e
    raise last


# ---------------------------------------------------------------- port discovery
SKIP_WORDS = ('bluetooth',)
USB_SERIAL_VIDS = (0x1A86, 0x10C4, 0x0403, 0x067B)           # CH340, CP210x, FTDI, PL2303


def candidate_ports(com_port='auto', max_port=30):
    if com_port and com_port.lower() != 'auto':
        return [com_port.upper() if com_port.upper().startswith('COM') else com_port]
    listed = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if any(w in (p.description or '').lower() for w in SKIP_WORDS):
                continue
            listed.append((0 if p.vid in USB_SERIAL_VIDS else 1, p.device))
    except Exception:
        pass
    if listed:
        return [d for _, d in sorted(listed)]
    return [f'COM{n}' for n in range(1, max_port + 1)] if sys.platform.startswith('win') else []


def find_pn532(com_port='auto', baud=115200, max_port=30, stop=None, prefer=None):
    """Return (Pn532, port_name, errors). errors maps port -> short reason for ports that did not answer."""
    errors = {}
    ports = candidate_ports(com_port, max_port)
    if prefer and prefer in ports:
        ports.remove(prefer); ports.insert(0, prefer)
    for port in ports:
        if stop is not None and stop.is_set():
            break
        try:
            pn = Pn532.open(port, baud)
            return pn, port, errors
        except Exception as e:
            errors[port] = str(e).split('\n')[0][:120]
    return None, None, errors
