# StudyToken Web + NFC (v27 — Render + Local NFC Bridge)

เวอร์ชันนี้ย้าย Flask app ขึ้น **Render (HTTPS สาธารณะ)** โดย GUI, Dashboard, Courses, Chapters, Timer, History, Timeline และ workflow เดิมทั้งหมดยังเหมือนเดิม (ไฟล์ `static/` และ `templates/` เหมือนต้นฉบับ v26 ทุกไบต์)
สิ่งที่เพิ่ม: **Local NFC Bridge สำหรับ Windows** (ต่อ PN532 จริงผ่าน USB/COM), **PostgreSQL**, การสื่อสารที่ปลอดภัย, และเครื่องมือย้ายข้อมูล

## 1. สถาปัตยกรรม

```
 โทรศัพท์ ──แตะ Tag──► เปิด https://xxx.onrender.com/nfc/STK-XXXXXXXX ─┐
                                                                       ▼
 เบราว์เซอร์บน PC ◄──────────── HTTPS ──────────────►  Render: Flask (gunicorn) ◄──► PostgreSQL
                                                                       ▲
 PN532 ──USB/COM── Windows PC: Local NFC Bridge ──── HTTPS (ขาออกเท่านั้น) ┘
                   (nfc_bridge.py, ลายเซ็น HMAC, auto-reconnect)
```

- Render เข้าถึง COM port ของเครื่องคุณไม่ได้ → Bridge บน PC **เชื่อมออกไปหา Render เอง** (ไม่ต้องเปิดพอร์ต/ตั้งค่า router)
- Bridge ส่ง **UID** ที่อ่านจากเครื่องอ่านจริงขึ้นไป และรับคำสั่ง "เขียน NDEF URL ลง Tag" จากเว็บ
- ความสัมพันธ์เดิมยังคงอยู่: **`UID → NFC Token → Course`** (เก็บในฐานข้อมูล, เหมือนเดิม)
- URL ที่เขียนลง Tag = `https://<โดเมน Render>/nfc/<NFC Token>` (ใช้ `RENDER_EXTERNAL_URL` อัตโนมัติ หรือ `BASE_URL` ถ้าตั้งไว้)
- เปิดเว็บบน PC แล้วกด **Scan NFC / Save Course** ได้เหมือนเดิม เพราะ API `/api/nfc/*` ยังหน้าตาเดิม (เบื้องหลังเปลี่ยนเป็นคุยกับ Bridge)

## 2. ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `app.py` | Flask app เดิม + รองรับ PostgreSQL + โหมด NFC `bridge`/`local` + ความปลอดภัย |
| `database.py` | ชั้นฐานข้อมูล: SQLite (เดิม) หรือ PostgreSQL (เมื่อมี `DATABASE_URL`) + migration schema |
| `nfc_hub.py`, `bridge_api.py` | ฝั่งเซิร์ฟเวอร์ที่คุยกับ Bridge (`/api/bridge/*`) |
| `security.py` | รหัสผ่านเว็บ (ไม่บังคับ), cookie สำหรับหน้า study, ป้องกัน CSRF, security headers |
| `local_bridge/` | **โปรแกรมบน Windows**: `nfc_bridge.py`, `bridge_reader.py`, `run_bridge.bat`, `check_reader.bat`, `bridge.env.example` |
| `migrate_db.py` | ย้ายข้อมูล SQLite → PostgreSQL (`import`) และสำรอง PostgreSQL → SQLite (`export`) |
| `render.yaml`, `requirements.txt`, `gunicorn.conf.py` | ตั้งค่า deploy บน Render |
| `requirements-local.txt`, `run_web.bat` | โหมดเดิมบน PC (อ่าน PN532 ตรงด้วย `nfc_reader.py` + SQLite) ยังใช้ได้ |
| `tests/` | ชุดทดสอบ (ดูหัวข้อ 9) |

## 3. Deploy ขึ้น GitHub

1. สมัคร/เข้าสู่ระบบ GitHub → **New repository** (ตั้งเป็น **Private** ได้) เช่นชื่อ `studytoken`
2. แตก ZIP แล้วเปิด Terminal ในโฟลเดอร์โปรเจกต์ (โฟลเดอร์ที่มี `render.yaml`):
   ```bash
   git init
   git add .
   git commit -m "StudyToken v27 Render"
   git branch -M main
   git remote add origin https://github.com/<ชื่อคุณ>/studytoken.git
   git push -u origin main
   ```
3. `.gitignore` กันไม่ให้ `data/`, `*.db`, `bridge.env` ขึ้น GitHub แล้ว — **อย่า commit ไฟล์ฐานข้อมูลหรือ token**

## 4. Deploy บน Render (Blueprint)

1. เข้า https://dashboard.render.com → **New → Blueprint** → เลือก repo `studytoken` → **Connect**
2. Render อ่าน `render.yaml` แล้วสร้าง: Web Service `studytoken-web` + PostgreSQL `studytoken-db` (region Singapore) → กด **Apply**
3. รอ build เสร็จ (สถานะ *Live*) แล้วเปิด URL ที่ Render ให้ เช่น `https://studytoken-web-xxxx.onrender.com`
4. เปิด **studytoken-web → Environment** แล้วจดค่า:
   - `BRIDGE_TOKEN` → ใส่ใน `bridge.env` ของ Bridge (หัวข้อ 5)
   - `APP_PASSWORD` → รหัสผ่านเข้า Dashboard (username = `studytoken`) ลบตัวแปรนี้ถ้าไม่ต้องการล็อกอิน (ไม่แนะนำเมื่อเปิดสาธารณะ)
5. ตรวจ: เปิด `https://<url>/health` ต้องได้ `{"ok":true,...}` และ `https://<url>/health/full` ต้องได้ `"ok":true`

> หากใช้โดเมนของตัวเอง ให้ตั้ง `BASE_URL=https://โดเมนของคุณ` (และใส่โดเมนนั้นใน `ALLOWED_WRITE_HOSTS` ของ `bridge.env`)

### ข้อจำกัดของแผน Free ของ Render (สำคัญ)
- **PostgreSQL Free หมดอายุหลังสร้าง 30 วัน** (มีช่วงผ่อนผัน 14 วันให้อัปเกรด มิฉะนั้นถูกลบ) และไม่มี backup ตามเอกสาร Render → ถ้าต้องการเก็บข้อมูลถาวร ให้อัปเกรดฐานข้อมูลเป็นแผนเสียเงิน (เช่น `basic-256mb`) ใน Render Dashboard หรืออย่างน้อยสำรองเป็นระยะด้วย `python migrate_db.py export`
- **Web Service Free จะ sleep หลังไม่มี traffic 15 นาที** และตื่นช้าราว 1 นาที; ตอนที่ Bridge ทำงานอยู่จะส่งคำขอต่อเนื่อง จึงควรไม่ sleep แต่จะใช้ชั่วโมง Free instance ต่อเนื่อง (มีโควตารายเดือน) — ถ้าต้องการเสถียร ใช้แผน Starter
- ระบบเก็บสถานะ NFC ไว้ในหน่วยความจำ จึงรัน **1 instance / 1 process เท่านั้น** (`gunicorn.conf.py` ตั้ง `workers=1`) อย่าตั้ง scale > 1
- ข้อมูลแผน/ราคาของ Render เปลี่ยนได้ ตรวจที่ https://render.com/docs/free ก่อนตัดสินใจ

## 5. ติดตั้ง Local NFC Bridge บน Windows

ต้องมี Python 3.10+ (ติ๊ก *Add Python to PATH*) และ PN532 แบบ **UART/HSU ผ่าน USB-Serial** (CH340/CP210x/FTDI) — ตั้งสวิตช์บนบอร์ดเป็นโหมด HSU

**v27.1:** Bridge ใช้ไดรเวอร์ PN532 ของตัวเอง (`pn532_raw.py`) แทน `nfcpy` เป็นค่าเริ่มต้น → ได้ **UID ทันทีจากชิป** ไม่ต้องรอให้ parse NDEF ได้ จึงอ่าน **NTAG213/215/216** (UID 7 ไบต์) และ Tag ที่ยังว่างได้ และเขียน URL ลง NTAG ได้โดยตรง (ตรวจอ่านกลับหลังเขียน) ถ้าต้องการแบบเดิมตั้ง `READER_BACKEND=nfcpy` (หรือ `auto` = ลองไดรเวอร์ใหม่ก่อน)

1. คัดลอกโฟลเดอร์ `local_bridge/` ไปไว้บน PC ที่ต่อ PN532 (หรือ clone repo)
2. คัดลอก `bridge.env.example` เป็น `bridge.env` แล้วแก้:
   ```
   SERVER_URL=https://studytoken-web-xxxx.onrender.com
   BRIDGE_TOKEN=<ค่า BRIDGE_TOKEN จาก Render>
   COM_PORT=auto          # หรือระบุ เช่น COM3
   ```
3. **ทดสอบเครื่องอ่านก่อน** (ไม่ต้องใช้เซิร์ฟเวอร์): ดับเบิลคลิก `check_reader.bat` → แสดงรายการ COM, เปิด PN532, แล้วรอให้แตะ Tag (30 วินาที) จะพิมพ์ UID และ NDEF ที่มีอยู่
4. ดับเบิลคลิก `run_bridge.bat` (สร้าง venv, ติดตั้ง, รัน, และรีสตาร์ตให้อัตโนมัติถ้าหลุด) ปล่อยหน้าต่างไว้ตลอดที่ใช้งาน
5. เปิดหน้า `/desktop` → สถานะต้องเป็น `● READY` และ PORT เป็น `COMx via Local Bridge`
6. ต้องปิดโปรแกรมอื่นที่จับ COM port เดียวกัน (เช่นเวอร์ชัน v26 โหมด local หรือ GUI เก่า) ไม่งั้นจะเปิดพอร์ตไม่ได้

อยากให้รันตอนเปิดเครื่อง: ใช้ Windows **Task Scheduler** → Create Task → Trigger *At log on* → Action เปิด `run_bridge.bat`

## 6. ใช้งาน NFC (เหมือนเดิม)

รองรับ Tag: **NTAG213 / NTAG215 / NTAG216** (เขียน URL ได้) — Tag อื่น (เช่น MIFARE Classic) อ่าน UID ได้แต่เขียน URL ไม่ได้

1. Add Course → กรอกข้อมูล → **Generate NFC Token**
2. (ทางเลือก) กด **Scan NFC** แล้วแตะ Tag บน PN532 → ได้ UID จริง
3. **Save Course**
   - ถ้าสแกน UID ไว้ก่อน → ระบบผูก `UID → Token → Course` และเขียน URL ลง Tag ทันที (Tag ต้องวางอยู่บนเครื่องอ่าน)
   - ถ้ายังไม่สแกน → บันทึกก่อน แล้วระบบเข้าโหมด pairing ให้แตะ Tag เพื่อผูกและเขียน URL
4. แตะ Tag ด้วยโทรศัพท์ → เปิด `https://.../nfc/STK-XXXXXXXX` → หน้า Study (Timer/Timeline)
5. แตะ Tag บน PN532 ที่หน้า Dashboard/History → เด้งเข้าหน้า Study เหมือนเดิม

สถานะการเขียน: `SUCCESS` / `WAITING_FOR_TAG` (ยังไม่เจอ Tag, หมดเวลา 60 วินาที) / `ERROR` (เช่น Tag ถูกล็อก, ไม่ใช่ NDEF) / `NFC_UNAVAILABLE` (Bridge หรือเครื่องอ่านออฟไลน์) — ไม่มีการ "แกล้งสำเร็จ"

## 7. ย้ายข้อมูลเดิม (SQLite → PostgreSQL)

ข้อมูลเดิมอยู่ที่ `data/study_token.db` บนเครื่องคุณ (**อย่าอัปขึ้น GitHub**)

1. Render → `studytoken-db` → copy **External Database URL** (ถ้าเชื่อมไม่ได้ ตรวจ *Access Control* ของฐานข้อมูล ให้อนุญาต IP ของคุณ)
2. ในโฟลเดอร์โปรเจกต์:
   ```bash
   pip install -r requirements.txt
   python migrate_db.py import --sqlite data/study_token.db --database-url "postgresql://..." --dry-run
   python migrate_db.py import --sqlite data/study_token.db --database-url "postgresql://..."
   ```
3. ความปลอดภัยของสคริปต์: ไม่แก้ไฟล์ต้นทาง (ทำงานบนสำเนา), ข้ามแถวที่มีอยู่แล้วแทนการเขียนทับ, รักษา id เดิม, ทำใน transaction เดียว (สำเร็จทั้งหมดหรือไม่ทำเลย), ตรวจจำนวนแถวหลังเสร็จ, รันซ้ำได้โดยไม่ซ้ำซ้อน, อัปเกรด schema เก่า (v9–v25) ให้อัตโนมัติ
4. สำรองข้อมูลจาก PostgreSQL: `python migrate_db.py export --sqlite backup-2026-11-01.db --database-url "postgresql://..."`
5. ที่อยู่ของ Tag ที่เขียนไว้แล้วด้วย IP/URL เก่า (`http://IP:5000/nfc/...`) ต้องเขียนใหม่เป็น URL ของ Render (เปิด Course → ผูก/แตะ Tag เพื่อเขียนซ้ำผ่าน `/api/nfc/link`)

## 8. ความปลอดภัย

**Web ↔ Bridge**
- ทุกคำขอจาก Bridge เซ็นด้วย **HMAC-SHA256** (ใช้ `BRIDGE_TOKEN`, ไม่ส่งตัว token ออกไปในสาย) ครอบคลุม method, path, เวลา, nonce, และ hash ของ body
- กัน replay (nonce ใช้ซ้ำไม่ได้, เวลาคลาดเคลื่อนเกิน 120 วินาทีถูกปฏิเสธ พร้อมข้อความบอกให้ซิงก์นาฬิกา), จำกัดการลองผิด (15 ครั้ง/นาที/IP)
- บังคับ HTTPS: Bridge ปฏิเสธ `http://` ไปยังเซิร์ฟเวอร์ระยะไกล
- Bridge เขียนเฉพาะ URL รูปแบบ `https://<host ที่อนุญาต>/nfc/STK-XXXXXXXX` เท่านั้น (กันคำสั่งเขียน URL แปลกปลอม)
- อนุญาต Bridge ทีละ 1 ตัว; หมุนเวียน token ได้โดยตั้ง `BRIDGE_TOKEN_PREVIOUS` ชั่วคราว
- **Timeout/reconnect:** long-poll 25 วินาที, backoff + jitter สูงสุด 30 วินาที, ส่งเหตุการณ์ซ้ำได้อย่างปลอดภัย (seq กันซ้ำ), แตะ Tag ที่เก่ากว่า 10 วินาทีจะไม่ถูกส่งย้อนหลัง, เซิร์ฟเวอร์ restart แล้ว Bridge เชื่อมต่อใหม่เอง, Bridge เงียบเกิน 45 วินาที = แสดง *LOCAL NFC BRIDGE OFFLINE*

**เว็บไซต์สาธารณะ (เพิ่มเติม ปิดได้)**
- มี `APP_PASSWORD`: Dashboard/Courses/History/API จัดการ ต้องล็อกอิน (HTTP Basic); `/nfc/<token>`, `/health`, static เปิดสาธารณะเพื่อให้โทรศัพท์ใช้ได้
- หน้า Study ใช้ cookie ที่ออกให้เฉพาะเมื่อเปิด `/nfc/<token>` ที่ถูกต้อง จึงเรียก `/api/sessions/*` ของ Course นั้นได้โดยไม่ต้องล็อกอิน
- กัน CSRF (ตรวจ Origin/Sec-Fetch-Site) และมี security headers
- หมายเหตุ: Token มี 8 ตัว hex (เหมือนเดิม) ถือเป็น "ลิงก์ลับ" ไม่ใช่รหัสผ่านที่แข็งแรงมาก

## 9. การทดสอบ — แยก "จำลอง" กับ "ฮาร์ดแวร์จริง"

รันทั้งหมด: `pip install pytest pgserver -r requirements.txt` แล้ว `pytest tests -q` ()

| ชุดทดสอบ | ใช้อะไรจริง | จำลองอะไร |
|---|---|---|
| `test_app_features.py` | ฟีเจอร์เดิมทั้งหมดบน **SQLite และ PostgreSQL จริง** | ไม่มี NFC (ส่งค่า UID ผ่าน API เหมือน GUI) |
| `test_bridge_protocol.py`, `test_security.py` | ลายเซ็น, replay, clock skew, rate limit, flow pairing/เขียน, cookie, CSRF | คำขอจาก Bridge ถูกสร้างโดยเทสต์ |
| `test_e2e_simulated.py` | gunicorn จริง + โปรเซส `nfc_bridge.py` จริง + HTTP + HMAC + DB จริง, restart เซิร์ฟเวอร์, bridge crash | **เฉพาะ PN532** (`--simulate`: แตะ Tag = เขียนบรรทัดลงไฟล์, "เขียน Tag" = เขียน JSON) |
| `test_migration.py` | ย้ายข้อมูล SQLite (v26 และ v9) ↔ PostgreSQL จริง | — |

**ที่ยังไม่ได้ทดสอบกับฮาร์ดแวร์จริง (ผมไม่มี PN532):** การเปิด COM port บน Windows, การอ่าน UID จริง, การเขียน NDEF ลง Tag จริง, การเปิด URL จากโทรศัพท์จริง, และการ deploy บน Render จริง โค้ดอ่าน/เขียน PN532 ใช้วิธีเดียวกับ `nfc_reader.py` เดิม (`nfcpy`, `com:COMx:pn532`, `ndef.UriRecord`) แต่ต้องยืนยันบนเครื่องคุณ

### Checklist ทดสอบจริง (ทำตามลำดับ)
1. `check_reader.bat` → เห็น PN532 และแตะ Tag แล้วได้ UID
2. `run_bridge.bat` → `/desktop` ขึ้น `READY`, PORT = `COMx via Local Bridge` (ไม่ใช่ SIMULATED)
3. Add Course → Scan NFC → UID บนหน้าเว็บตรงกับที่ `check_reader` เห็น
4. Save Course (แตะ Tag ค้างไว้) → ข้อความ "เขียน URL ลง NFC สำเร็จ"
5. ใช้โทรศัพท์แตะ Tag → เปิด URL ของ Render → Timer/Timeline ทำงาน
6. แตะ Tag บน PN532 ที่ Dashboard → เข้าหน้า Study ของ Course ที่ถูกต้อง (UID → Token → Course)
7. ถอด USB แล้วเสียบใหม่ → สถานะกลับ `READY` เอง; ปิด/เปิด `run_bridge.bat` → เชื่อมต่อใหม่เอง
8. ทดลอง Tag ที่ถูกล็อก → ต้องขึ้น ERROR ไม่ใช่ SUCCESS

## 10. ตัวแปรสภาพแวดล้อม

| ตัวแปร | ค่า/ความหมาย |
|---|---|
| `DATABASE_URL` | PostgreSQL (Render ใส่ให้จาก render.yaml); ไม่ตั้ง = ใช้ SQLite เดิม |
| `NFC_MODE` | `bridge` (Render) หรือ `local` (อ่าน PN532 ตรง, ค่าเริ่มต้นบน PC) |
| `BRIDGE_TOKEN` | ความลับร่วมกับ Bridge (≥ 24 ตัวอักษร) |
| `APP_PASSWORD`, `APP_USER` | รหัสผ่าน/ผู้ใช้ (ค่าเริ่มต้น `studytoken`) ถ้าไม่ตั้ง = เปิดสาธารณะไม่มีล็อกอิน |
| `SECRET_KEY` | กุญแจเซ็น cookie |
| `BASE_URL` | โดเมนที่เขียนลง Tag (ค่าเริ่มต้น `RENDER_EXTERNAL_URL`) |
| `BRIDGE_STALE_SECONDS` (45), `BRIDGE_WRITE_TTL` (60), `ALLOW_SIMULATED_BRIDGE` (0) | ปรับ timeout / อนุญาต bridge จำลอง (ห้ามเปิดใน production) |

## 11. แก้ปัญหา

| อาการ | สาเหตุ/วิธีแก้ |
|---|---|
| `WAITING FOR LOCAL NFC BRIDGE` | ยังไม่ได้รัน Bridge หรือ `SERVER_URL` ผิด; ดู `local_bridge/bridge.log` |
| Bridge log: `BAD_SIGNATURE` | `BRIDGE_TOKEN` ใน `bridge.env` ไม่ตรงกับ Render |
| Bridge log: `CLOCK_SKEW` | นาฬิกา Windows เพี้ยน → Settings > Time > Sync now |
| `BRIDGE_ALREADY_CONNECTED` | มี Bridge อีกตัว/อีกหน้าต่างทำงานอยู่ (รอ ~45 วินาที หรือปิดตัวเดิม) |
| `WAITING FOR NFC READER` | Bridge ไม่เจอ PN532: ตรวจสาย/COM, ปิดโปรแกรมที่จับพอร์ต, ลอง `COM_PORT=COM3` (ข้อความ error ใต้สถานะบอกเหตุผลของแต่ละพอร์ต) |
| READY แต่แตะแล้วไม่มี UID | รัน `check_reader.bat` แล้วส่งผลให้ดู; ดู `local_bridge/bridge.log` ว่ามีบรรทัด `tag 04:..` ไหม; ตรวจว่า Tag แตะตรงเสาอากาศ PN532 และไม่มีโลหะรองอยู่ (NTAG ติดสติกเกอร์บนโลหะจะอ่านไม่ได้) |
| `no ACK from PN532` | บอร์ดไม่ได้อยู่โหมด UART/HSU (ปรับสวิตช์ SEL0/SEL1), baud ไม่ตรง (`BAUD=` ใน bridge.env), หรือสลับสาย TX/RX |
| เขียนแล้ว `URL host not allowed` | `BASE_URL` เป็นโดเมนอื่น → ใส่ใน `ALLOWED_WRITE_HOSTS` |
| `NFC LIBRARY NOT INSTALLED` | `pip install -r local_bridge/requirements.txt` ใน venv ของ Bridge |
| เข้าเว็บครั้งแรกช้า ~1 นาที | Free plan ตื่นจาก sleep |

---

# เอกสารเดิม (v26) — ยังใช้ได้ในโหมด local

# StudyToken Web + NFC Token + PN532

## หลักการใหม่
- **NFC Token** เป็นตัวระบุ Course ที่เปิดผ่าน URL เช่น `http://IP:5000/nfc/STK-A1B2C3D4`
- **UID** เป็นรหัสฮาร์ดแวร์ของ Tag ใช้ภายในเท่านั้น เพื่อค้นว่า UID นี้กำลังชี้ไป Token ไหน
- Database มี `subjects` สำหรับ `รหัสวิชา -> ชื่อวิชา`
- Course แต่ละ Chapter จะมี Token ของตัวเอง
- ถ้าใช้ Tag เดิมกับ Chapter ใหม่ ระบบจะย้าย UID ให้ชี้ Token ใหม่ แต่ Token/URL เก่าที่ยังมีอยู่ยังเปิดได้

## Add Course
1. กรอกรหัสวิชา
2. ถ้ารหัสมีใน database ระบบล็อกชื่อวิชาและตั้ง Chapter ถัดไปให้อัตโนมัติ เช่น 1 -> 2
3. ถ้าเป็นรหัสใหม่ ให้กรอกชื่อวิชา
4. กรอก Chapter, Title และ Time (min)
5. กด Scan NFC แล้วแตะ Tag
6. กด Save Course
7. ระบบสร้าง NFC Token และเขียน URL NDEF ลง Tag

Chapter รองรับ `1`, `๑`, `2`, `IV`, `VI` และจะเก็บเป็นเลขอารบิก เช่น `4`, `6`

## NFC Reader
ระบบสแกน `COM1` ถึง `COM30` ตามลำดับและใช้พอร์ตแรกที่พบ PN532 ได้ โดยจะสแกนซ้ำแบบต่อเนื่องทุกประมาณ 1.5 วินาที จึงสามารถเสียบ/ถอด PN532 ได้โดยไม่ต้องรีสตาร์ตเว็บหรือรีเฟรชหน้า สถานะหน้าเว็บอัปเดตแบบ realtime และไม่แสดงรายการ error ของ COM ที่หาไม่เจอ

## Timer
ช่องเวลาใน Add Course/Course Editor เป็น **Time (minutes)** และไม่ล็อกค่าเริ่มต้น และเก็บภายในเป็นวินาทีเพื่อการจับเวลาที่ละเอียด
เมื่อครบ 100% จะบันทึก Session, ตั้ง Course เป็น `FINISH` และกลับ Dashboard โดย Course ไม่ถูกลบ เพื่อให้เห็นสถานะ `Finish` ข้างปุ่ม Delete

## Flow การสแกน
### PN532
`UID -> Database -> NFC Token -> Course`

### โทรศัพท์
`NDEF URL -> NFC Token -> Database -> Course`

ดังนั้น UID ไม่ใช่ public URL อีกต่อไป


## Add Course + NFC Token workflow (v14)
1. Add Course form generates an NFC Token automatically.
2. Save Course first; UID is not required at this step.
3. After save, the app automatically enters NFC pairing mode and asks the user to scan an NFC Tag.
4. PN532 reads the physical Tag UID.
5. The server links `UID -> NFC Token -> Course`.
6. The app then attempts to write the Token URL to that NFC Tag.


## Chapter Section Timeline (v26)
- **Save** (study page): pauses the timer, asks "What have you studied up to this point?" and stores the finished segment as a new timeline section (study date, start time, duration, description, sequence number, subject code + chapter). **Resume Study** starts the next segment; paused time is never counted. **Cancel** in the dialog just continues the same segment.
- **Finish Study** never asks for text. Unsaved time left in the last segment is stored as one automatic section (italic, "no progress note"); finishing right after a Save creates nothing extra. Set `AUTO_SECTION_ON_FINISH=False` in `app.py` to keep that time in History only.
- Closing / refreshing the tab closes the session and keeps its time but does not create a section; saved sections are in SQLite (`timeline_sections`) and reload with the page.
- Timeline is shown on the study page (with a live "Studying now" node) and from the **Timeline** button on each Dashboard course card. Markers are the cumulative study time at the start of each section, like video chapters.
- API: `POST /api/sessions/<id>/sections`, `POST /api/sessions/<id>/resume`, `GET /api/courses/<id>/timeline`. Existing databases are migrated automatically.
