# คู่มือบันทึกและรีเพลย์การแตะ/ปัด (ใช้งานบน Docker)

คู่มือนี้สรุปขั้นตอนภาษาคนสำหรับบันทึกการแตะ/ปัดบนอุปกรณ์ Android และรีเพลย์กลับโดยใช้สคริปต์ภายในคอนเทนเนอร์ `controller` (ไม่ต้องติดตั้ง Python/ADB บน Windows host).

## 0) สิ่งที่ต้องมีบน Windows
- ติดตั้ง **WSL2 + Docker Desktop** (และ `usbipd-win` ถ้าต่ออุปกรณ์ผ่าน USB แล้ว passthrough เข้า WSL/VM)
- โค้ดโปรเจกต์ต้องอยู่ในไดเรกทอรีที่ Docker เห็นได้ เช่น `D:\android-controller` (หรือ path ที่แมปให้คอนเทนเนอร์ใช้งาน)
- ไม่ต้องติดตั้ง Python บน Windows; ทุกอย่างรันในคอนเทนเนอร์

## 1) เปิดคอนเทนเนอร์
จากโฟลเดอร์โปรเจกต์บน Windows (PowerShell/WSL):
```bash
docker compose up -d --build
```
แล้วเข้าเชลล์คอนเทนเนอร์ `controller`:
```bash
docker compose exec controller bash
```
คำสั่งต่อจากนี้ให้รันในเชลล์คอนเทนเนอร์

## 2) จับคู่ UI dump + สกรีนช็อตก่อน
บันทึก XML + PNG พร้อม timestamp/stage เดียวกัน (ช่วยหาจุด element ได้ตรง):
```bash
capture-ui-and-screen.py -g login-screen -s <DEVICE_SERIAL_OR_IP>
```
ไฟล์จะอยู่ที่ `/work/ui-dumps` ชื่อ `<timestamp>-<stage>.xml` และ `.png`

## 3) รีเพลย์จาก log พิกัดดิบ (tap/swipe ตามเวลาจริง)
ถ้ามีไฟล์ JSON/CSV จาก `touch-event-capture.py` แล้ว รีเพลย์ตาม timestamp และความยาวปัดเดิม:
```bash
replay-log.py /work/touch-events.json --speed 1.5
```
- ปรับ `--speed` เพื่อเร่ง/ช้า หรือใช้ `--fixed-delay` กำหนดช่วงพักคงที่

## 4) รีเพลย์แบบอ้างอิง element
ใช้ log ที่บันทึก element (เช่น `element-actions.json`) พร้อม UI dump ล่าสุดเพื่อหาพิกัดกลางของ element อัตโนมัติ:
```bash
replay-log.py /work/element-actions.json --ui-source /work/ui-dumps --verify screenshot
```
- เลือก `--verify ui|screenshot|both` เพอดึงหลักฐานหลังแต่ละสเต็ป

## 5) วาด marker ซ้ำจุดกดบนสกรีนช็อต (เพื่อตรวจสอบ)
สร้างภาพระบุจุดกด/เส้นปัดจาก log พิกัดดิบ:
```bash
overlay-touches.py /work/touch-events.json /work/ui-dumps/<timestamp>-login-screen.png
```
- ผลลัพธ์มี suffix `-marked` หรือกำหนดชื่อเองด้วย `-o`

## 6) สรุปสั้น
- จับ UI + สกรีนช็อตก่อน → ได้บริบทตำแหน่ง element
- รีเพลย์ได้ทั้งแบบพิกัดดิบ หรืออ้างอิง element ให้แม่นขึ้น
- ใช้ `--speed`/`--fixed-delay` ปรับจังหวะ และ `--verify` ขอหลักฐานระหว่างทาง

## 7) หาไฟล์สกรีนช็อตบนเครื่อง host (Windows)
ค่าเริ่มต้นของสคริปต์จะบันทึกภาพที่ `/img/captures` และคัดลอกไปที่ `/img/sent` ภายในคอนเทนเนอร์ ซึ่งถูกแมปกับโฟลเดอร์ `D:\android-controller\img` บน host ตาม `docker-compose.yml`:
- ภายในคอนเทนเนอร์: `/img/captures` และ `/img/sent`
- บน Windows host: `D:\android-controller\img\captures` และ `D:\android-controller\img\sent`

ถ้าไม่เห็นไฟล์:
1) ตรวจสอบว่า compose ที่ใช้มีการแมป volume `/img` แล้ว (ต้องเป็น path แบบ `D:/android-controller/img`).  
2) ตรวจสอบในคอนเทนเนอร์ว่าไฟล์ถูกสร้างจริงหรือไม่:
```bash
docker compose exec controller ls -la /img/captures
```
3) สร้างโฟลเดอร์บน host ล่วงหน้าได้ เช่น `D:\android-controller\img\captures` (บางครั้ง Docker Desktop จะไม่สร้างให้โดยอัตโนมัติ)
