# Deploy StudyToken Web v26 to Render (Cloud mode + SQLite)

## สิ่งที่เปลี่ยน
- เพิ่ม `render.yaml` สำหรับสร้าง Render Web Service อัตโนมัติ
- ใช้ `gunicorn` แทน `app.run()` ใน production
- ใช้ SQLite ที่ `/var/data/study_token.db` บน Render Persistent Disk
- ใช้ `RENDER_EXTERNAL_URL` เพื่อสร้าง URL ของ NFC Token เป็น Public URL ของ Render
- ปิดการพยายามเปิด PN532/COM ใน Cloud mode เพราะ Render เข้าถึง USB/COM ของคอมที่บ้านไม่ได้
- หน้า Add Course บันทึก Course และสร้าง URL ได้โดยไม่รอการสแกนจาก PN532

## Deploy
1. แตก ZIP แล้วสร้าง GitHub repository โดยให้อัปโหลดทั้ง `render.yaml`, `RENDER_DEPLOY_TH.md` และโฟลเดอร์ `v26/` ไว้ที่ root ของ repository
2. ใน Render เลือก **New + → Blueprint** แล้วเชื่อม repository นี้
3. Render จะอ่าน `render.yaml`; ตรวจสอบ service `studytoken-web` และสร้าง Web Service พร้อม Persistent Disk 1 GB
4. เลือกแพ็กเกจ Starter หรือแพ็กเกจแบบชำระเงินที่รองรับ Persistent Disk (Free Web Service ใช้ Persistent Disk ไม่ได้)
5. กดยืนยัน Apply/Deploy แล้วรอจนสถานะ Live
6. เปิด URL `https://<ชื่อบริการ>.onrender.com` และทดสอบ `/health` ซึ่งควรตอบ JSON ที่มี `ok: true`
7. ทดสอบ Add Course, Dashboard, History, Study Timer และ Timeline แล้วตรวจว่าข้อมูลยังอยู่หลัง Deploy ใหม่

## ข้อจำกัด NFC ใน Cloud mode
- เมื่อบันทึก Course ระบบจะสร้าง URL รูปแบบ `https://<ชื่อบริการ>.onrender.com/nfc/STK-XXXXXXXX`
- คัดลอก URL นี้ไปเขียนลง NFC Tag ด้วยโทรศัพท์/อุปกรณ์ที่รองรับการเขียน NDEF URL หรือใช้ QR Code แทนได้
- โค้ดเดิมที่อ่าน UID/เขียน Tag ผ่าน PN532 บน COM จะไม่ทำงานบน Render และถูกปิดใน Cloud mode
- การเปิด URL จาก NFC Tag ไม่ได้แปลว่าเว็บอ่าน UID ของแท็กได้โดยอัตโนมัติ; UID mapping ยังต้องมีอุปกรณ์อ่าน NFC ที่เชื่อมกับระบบ
- iPhone 13 สามารถเปิด URL ที่เขียนไว้ใน NFC Tag ได้เมื่อแท็กมี NDEF URL ที่ถูกต้อง แต่ Safari บน iOS ไม่รองรับ Web NFC API สำหรับอ่าน/เขียนแท็กจากหน้าเว็บทั่วไป

## SQLite และความคงทนของข้อมูล
SQLite ยังคงเป็นฐานข้อมูลตามที่เลือก โดยไฟล์ฐานข้อมูลต้องอยู่ใต้ `/var/data` ซึ่งเป็น mount path ของ Persistent Disk หากลบ disk หรือเปลี่ยนชื่อ/บริการโดยไม่ย้ายข้อมูล ไฟล์ฐานข้อมูลอาจสูญหายได้ ควรสำรองไฟล์เป็นระยะ

## Local development
ในเครื่องเดิมยังใช้ `python app.py` ได้ตามปกติ และยังใช้ `nfc_reader.py` ได้เมื่อมีไลบรารีและฮาร์ดแวร์ PN532 ที่รองรับติดตั้งอยู่

## ความปลอดภัยก่อนใช้งานจริง
โปรเจกต์ต้นฉบับยังไม่มีระบบล็อกอิน/สิทธิ์ผู้ดูแล API สำหรับเพิ่ม แก้ไข และลบ Course จึงไม่ควรใส่ข้อมูลส่วนตัวหรือเปิดให้ใช้งานจริงแบบสาธารณะก่อนเพิ่ม Authentication/Authorization ให้เสร็จ เพราะผู้ที่เข้าถึง URL อาจเรียก API ที่เปลี่ยนข้อมูลได้
