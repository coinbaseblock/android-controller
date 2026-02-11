# Universal Record & Play Guide

## Overview

ระบบ Universal Recording รองรับ 2 แบบการอัดและเล่น ในไฟล์เดียว (.urf.json):

| แบบ | อัด (Record) | เล่น (Play) | ใช้ตอนไหน |
|-----|-------------|-------------|-----------|
| **Raw** | อัดการใช้นิ้วจริงๆ (getevent) | เล่นตาม coordinate เดิม | ต้องการความแม่นยำตามพิกัด |
| **Annotated** | เลือก element ทีละตัว | หา element อัตโนมัติ แล้วกด | ต้องการความเสถียร (element ย้ายได้) |
| **Mixed** | ผสมทั้งสองแบบ | เล่นได้ทั้งหมด | ใช้งานจริง |

---

## Quick Start

### 1. Record

#### แบบ Raw (อัดนิ้วจริง)
```bash
# อัดทุก touch event (เหมือนใช้นิ้ว)
docker compose exec -it controller \
  universal-recorder.py --mode raw -p com.example.app -o /work/my-flow.urf.json

# อัด raw + auto-annotate (บอกด้วยว่ากดอะไร)
docker compose exec -it controller \
  universal-recorder.py --mode raw --annotate -p com.example.app -o /work/my-flow.urf.json
```

เมื่อรัน:
- ใช้นิ้วทำทุกอย่างบนมือถือ
- ระบบจะอัดทุก touch down/move/up
- `--annotate` จะบอกด้วยว่ากดลง element อะไร
- กด **Ctrl+C** เมื่อเสร็จ

#### แบบ Annotated (เลือก element)
```bash
docker compose exec -it controller \
  universal-recorder.py --mode annotated -p com.example.app -o /work/my-flow.urf.json
```

จะเปิด interactive mode:
```
=== Universal Recorder (Annotated Mode) ===

rec> o              ← เปิดแอป
rec> w 3            ← รอ 3 วินาที
rec> t              ← ดึง UI แล้วเลือก element
                      → แสดงรายการ:
                        [ 0] Button  btn_login  'Login'  @(540,1200)
                        [ 1] TextView  title  'Welcome'  @(540,200)
                      เลือก #: 0
                      → + element tap: Login @(540,1200)
rec> s home         ← ถ่ายหน้าจอ "home" + ส่ง
rec> tap 540 800    ← กดพิกัดตรงๆ
rec> swipe 540 1800 540 600 300   ← ปัดขึ้น
rec> k 4            ← กด BACK
rec> c              ← ปิดแอป
rec> l              ← ดูรายการทั้งหมด
rec> u              ← ลบอันล่าสุด
rec> q              ← บันทึกและออก
```

### 2. Edit (แก้ไข)

```bash
docker compose exec -it controller \
  flow-editor.py /work/my-flow.urf.json
```

Editor commands:
```
edit> l              ← ดู frames ทั้งหมด
edit> l 50           ← ดู 50 frames ล่าสุด
edit> d 5            ← ดูรายละเอียด frame #5
edit> del 5          ← ลบ frame #5
edit> del 5-10       ← ลบ frame #5 ถึง #10
edit> dis 3          ← ปิด frame #3 (ไม่ลบ แค่ข้าม)
edit> en 3           ← เปิด frame #3 กลับ
edit> i 5 tap        ← แทรก tap ที่ตำแหน่ง 5
edit> i 5 wait       ← แทรก wait
edit> i 5 screenshot ← แทรก screenshot
edit> i 5 element    ← แทรก element tap
edit> m 3 7          ← ย้าย frame #3 ไปตำแหน่ง 7
edit> t 5 +500       ← เลื่อนเวลา frame #5 ไป +500ms
edit> t 5 -200       ← เลื่อนเวลา frame #5 ไป -200ms
edit> t all 2.0      ← ขยายเวลาทุก frame 2 เท่า (ช้าลง)
edit> t all 0.5      ← ลดเวลาทุก frame ครึ่งหนึ่ง (เร็วขึ้น)
edit> t shift 1000   ← เลื่อนทุก frame ไป +1000ms
edit> speed 1.5      ← ตั้งค่า playback speed
edit> loop on        ← เปิด loop
edit> loop 10        ← loop 10 รอบ
edit> loop delay 3   ← พักระหว่าง loop 3 วินาที
edit> error retry    ← ตั้ง error mode (retry/skip/pause/stop)
edit> collapse       ← ยุบ raw touches เป็น gestures
edit> compact        ← ลบ disabled frames ออก
edit> merge f2.json  ← รวม frames จากไฟล์อื่น
edit> export out.json← export เฉพาะ gestures (ไม่มี raw)
edit> n 5 note text  ← ใส่ note ให้ frame #5
edit> undo           ← ย้อนกลับ
edit> info           ← ดู metadata & settings
edit> s              ← บันทึก
edit> sa new.json    ← บันทึกเป็นไฟล์ใหม่
edit> q              ← ออก
```

### 3. Play (เล่น)

```bash
# เล่นปกติ
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json

# เล่น 2x speed
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --speed 2.0

# เล่น loop ไม่หยุด
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --loop

# เล่น loop 5 รอบ
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --loop --loop-count 5

# เล่นเฉพาะ gesture (ข้าม raw touches)
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --replay-mode smart

# เล่นเฉพาะ raw touches
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --replay-mode raw

# Dry run (ดูว่าจะทำอะไร ไม่ได้ทำจริง)
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --dry-run

# เล่น + debug dashboard (http://localhost:8080)
docker compose exec controller \
  universal-player.py /work/my-flow.urf.json --debug-port 8080
```

### Replay Modes

| Mode | ทำอะไร | เหมาะกับ |
|------|--------|----------|
| `auto` (default) | ใช้ gesture ถ้ามี ไม่มีใช้ raw | ทั่วไป |
| `smart` | ใช้เฉพาะ gesture + element | ต้องการความเสถียร |
| `raw` | ใช้เฉพาะ raw touch events | ต้องการตามพิกัดเดิม |

---

## Universal Recording Format (.urf.json)

ไฟล์ `.urf.json` รองรับ frame types เหล่านี้:

| Type | คือ | ตัวอย่าง |
|------|-----|---------|
| `touch` | Raw finger touch (down/move/up) | ใช้นิ้วจริง |
| `gesture` | Collapsed gesture (tap/swipe/long_press) | สรุปจาก raw หรือใส่เอง |
| `element` | Tap on element by resource-id/text | เสถียรกว่า coordinate |
| `app` | Open / Close app | เปิด/ปิดแอป |
| `key` | Key event | BACK, HOME, etc. |
| `screenshot` | Take + send screenshot | ถ่ายหน้าจอ |
| `wait` | Pause | หน่วงเวลา |
| `shell` | Run ADB shell command | คำสั่งพิเศษ |
| `marker` | Bookmark / annotation | บันทึกจุดสังเกต |

### Mixed Flow Example

```json
{
  "frames": [
    {"id": 1, "t": 0,    "type": "app", "action": "open", "package": "com.example"},
    {"id": 2, "t": 3000, "type": "wait", "duration_ms": 3000},
    {"id": 3, "t": 6000, "type": "element", "action": "tap",
     "resource_id": "com.example:id/login", "timeout": 10},
    {"id": 4, "t": 8000, "type": "gesture", "action": "swipe",
     "x": 540, "y": 1800, "x2": 540, "y2": 600, "duration_ms": 300},
    {"id": 5, "t": 9000, "type": "screenshot", "stage": "result", "send": true},
    {"id": 6, "t": 10000, "type": "key", "keycode": 4, "key_name": "BACK"},
    {"id": 7, "t": 11000, "type": "app", "action": "close", "package": "com.example"}
  ]
}
```

---

## Error Handling & Obstruction Detection

เมื่อ element ถูกบัง (popup, dialog, notification):

1. **Auto-dismiss**: กดปิด dialog อัตโนมัติ (OK/CLOSE/CANCEL)
2. **Retry**: ลองใหม่ตาม max_retries
3. **Notify**: แจ้งเตือนพร้อม error screenshot
4. **Pause**: หยุดรอ user ดูผ่าน debug dashboard (http://localhost:8080)

ตั้งค่า error mode:
```
# ใน editor
edit> error retry    ← ลองใหม่อัตโนมัติ
edit> error skip     ← ข้าม frame ที่ error
edit> error pause    ← หยุดรอ user
edit> error stop     ← หยุดทั้งหมด
```

---

## Workflow ที่แนะนำ

### สำหรับงาน: เปิดแอป → เข้าหน้าต่างๆ → ถ่ายหน้าจอ → ส่ง → ปิด → วน

```bash
# 1. Record ครั้งแรก (เลือก annotated เพื่อความเสถียร)
universal-recorder.py --mode annotated -p com.myapp -o /work/myflow.urf.json

# 2. Edit ปรับเวลาและเพิ่ม screenshot
flow-editor.py /work/myflow.urf.json

# 3. ทดสอบ dry-run
universal-player.py /work/myflow.urf.json --dry-run

# 4. รันจริง + loop + debug
universal-player.py /work/myflow.urf.json --loop --debug-port 8080
```

### สำหรับงาน: อัดนิ้วจริง แล้วเล่นซ้ำ

```bash
# 1. Record raw + annotate
universal-recorder.py --mode raw --annotate -p com.myapp -o /work/raw.urf.json

# 2. Edit: collapse raw → gestures
flow-editor.py /work/raw.urf.json
  edit> collapse     ← ยุบ raw เป็น gesture
  edit> loop on      ← เปิด loop
  edit> s            ← save

# 3. Play
universal-player.py /work/raw.urf.json --loop
```

---

## Legacy Tools (ยังใช้ได้)

เครื่องมือเดิมยังทำงานได้ปกติ:

| Tool | หน้าที่ |
|------|---------|
| `touch-event-capture.py` | อัด raw touch (JSON/CSV) |
| `replay-log.py` | เล่น touch log / element log |
| `capture-ui-and-screen.py` | จับ UI dump + screenshot |
| `automation-engine.py` | เล่น YAML flow |
| `flow-recorder.py` | อัดแบบ YAML |

แนะนำให้ใช้ **Universal tools** ใหม่ เพราะรองรับทุกแบบในไฟล์เดียว
