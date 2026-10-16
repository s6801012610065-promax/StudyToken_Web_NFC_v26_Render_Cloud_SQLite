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
