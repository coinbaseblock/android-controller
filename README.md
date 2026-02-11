# Android Controller (ADB via Docker) — Galaxy A17

ควบคุม Android (เช่น **Galaxy A17**) ผ่าน **Docker** โดยให้ **ADB Server รันในคอนเทนเนอร์** และใช้งานได้ทั้ง **USB-C** และ **Wi-Fi (Wireless debugging)**
รองรับ **บันทึก (Record)**, **เล่นซ้ำ (Replay)**, และ **แก้ไขสคริปต์ (Edit)** ในรูปแบบ Universal Recording Format (`.urf.json`)

> ไฟล์คีย์/งานทั้งหมดเก็บที่ **D:\android-controller** ไม่แตะไดรฟ์ C

---

## สารบัญ

1. [โครงสร้างโปรเจกต์](#โครงสร้างโปรเจกต์)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Step 1: สตาร์ท Docker](#step-1-สตาร์ท-docker)
5. [Step 2: เชื่อมต่ออุปกรณ์](#step-2-เชื่อมต่ออุปกรณ์)
6. [Step 3: บันทึก (Record)](#step-3-บันทึก-record)
7. [Step 4: แก้ไขสคริปต์ (Edit)](#step-4-แก้ไขสคริปต์-edit)
8. [Step 5: เล่นซ้ำ (Replay)](#step-5-เล่นซ้ำ-replay)
9. [คำสั่งใช้งานอื่น ๆ](#คำสั่งใช้งานอื่น-ๆ)
10. [Troubleshooting](#troubleshooting)

---

## โครงสร้างโปรเจกต์

```
D:\android-controller\
├── docker-compose.yml
├── controller\
│   ├── Dockerfile
│   ├── universal_recorder.py    # อัดได้ 2 แบบ (raw + annotated)
│   ├── universal_player.py      # เล่นได้ทั้ง raw + script ผสมกัน
│   ├── flow_editor.py           # แก้ไขผ่าน CLI
│   ├── flow_editor_web.py       # แก้ไขผ่าน Web UI (port 9090)
│   ├── universal_format.py      # Format กลาง (.urf.json)
│   ├── element_interactor.py    # ทำงานกับ UI elements
│   ├── automation_engine.py     # Automation engine
│   ├── debug_server.py          # Debug dashboard (port 8080)
│   └── ...
├── scripts\                     # PowerShell scripts (รันจาก Windows)
│   ├── capture-screenshot.ps1
│   ├── connect-wireless.ps1
│   ├── delete-old-images.ps1
│   └── awake-device.ps1
├── scripts-controller\
│   └── control-device.ps1       # สั่ง tap/swipe/key จาก Windows
├── adbkeys\                     # เก็บคีย์ ADB (persist)
├── data\                        # พื้นที่งาน → /work ในคอนเทนเนอร์
└── img\                         # screenshots → /img ในคอนเทนเนอร์
```

> `controller` (ADB client) ชี้ไปหา `adb-server` ภายใน network เดียวกัน (ผ่าน `network_mode: service:adb-server`) จึง **ไม่พึ่ง ADB บน Windows**

---

## Prerequisites

- Windows 11 + WSL2 + Docker Desktop (เปิด `Use the WSL 2 based engine`)
- ติดตั้ง **usbipd-win** (ใช้เฉพาะโหมด USB):
  ```powershell
  usbipd --version
  ```
- โทรศัพท์ Android เปิด **Developer options** พร้อม:
  - **USB debugging**
  - **Wireless debugging**
  - (แนะนำ) **Disable adb authorization timeout**
  - (แนะนำ) **Stay awake / หน้าจอติดขณะชาร์จ**

---

## Quick Start

```powershell
# 1) สตาร์ทคอนเทนเนอร์
cd D:\android-controller
docker compose up -d --build

# 2) เชื่อมต่อ Wi-Fi (เปลี่ยน IP/port/code ตามจริง)
docker compose exec controller bash -lc "adb pair 172.20.10.9:46723 171719"
docker compose exec controller bash -lc "adb connect 172.20.10.9:34557"
docker compose exec controller bash -lc "adb devices -l"

# 3) อัดสคริปต์
docker compose exec controller universal-recorder.py --mode raw -p com.example.app -o /work/demo.urf.json

# 4) แก้ไขสคริปต์ (Web Editor)
docker compose exec controller flow-editor-web.py --port 9090 --dir /work
# เปิด http://localhost:9090

# 5) เล่นซ้ำ
docker compose exec controller universal-player.py /work/demo.urf.json --speed 1.5
```

---

## Step 1: สตาร์ท Docker

```powershell
cd D:\android-controller
docker compose up -d --build
```

ระบบจะรัน 2 services:
| Service | หน้าที่ |
|---------|---------|
| `adb-server` | ADB server (รองรับ USB + Wi-Fi), เปิด port 5037, 8080, 9090 |
| `controller` | ADB client สั่งงาน, ใช้ network เดียวกับ adb-server |

ตรวจสอบ:
```powershell
docker compose logs -f adb-server
# ควรเห็น: ADB server is running on 0.0.0.0:5037
```

---

## Step 2: เชื่อมต่ออุปกรณ์

### วิธี A: Wi-Fi (Wireless debugging) — แนะนำ

1. บนโทรศัพท์: Developer options > **Wireless debugging** > **Pair device with pairing code**
   - จด **IP**, **Pairing port**, **Pairing code**
   - จด **IP address & Port** สำหรับ `adb connect` (ต่างจาก pairing port)

2. สั่ง pair + connect:
```powershell
# Pair (ใส่ค่าจริงของคุณ)
docker compose exec controller bash -lc "adb pair 172.20.10.9:46723 171719"
# ได้: Successfully paired to 172.20.10.9:46723

# Connect
docker compose exec controller bash -lc "adb connect 172.20.10.9:34557"
# ได้: connected to 172.20.10.9:34557

# ตรวจสอบ
docker compose exec controller bash -lc "adb devices -l"
# เห็น device = พร้อมใช้งาน
```

หรือใช้สคริปต์อัตโนมัติ:
```powershell
.\scripts\connect-wireless.ps1
.\scripts\connect-wireless.ps1 -PairingAddress 172.20.10.9:46723 -PairingCode 171719
```

### วิธี B: USB-C (เสถียรสุด)

1. เสียบสาย USB-C กับโทรศัพท์ (ปลดล็อกจอไว้)
2. PowerShell (Admin):
```powershell
usbipd list
# หา BUSID ของ Samsung (เช่น 1-3)
usbipd bind --busid 1-3
usbipd attach --wsl --busid 1-3 --auto-attach
```
3. ตรวจสอบ:
```powershell
docker compose exec controller bash -lc "adb devices -l"
# มือถือเด้ง RSA popup → ติ๊ก Always allow → Allow
```

---

## Step 3: บันทึก (Record)

ระบบรองรับ **2 โหมดการบันทึก** ที่สามารถ **ผสม (Mixed)** กันได้ในไฟล์เดียว:

### โหมด 1: RAW — บันทึกการแตะจริง

บันทึกทุก touch event (down/move/up) เหมือนใช้นิ้วจริง:
```powershell
docker compose exec controller universal-recorder.py --mode raw -p com.example.app -o /work/demo.urf.json
```
- ใช้นิ้วแตะ/ปัดบนมือถือตามปกติ
- กด **Ctrl+C** เพื่อหยุดบันทึก
- ไฟล์จะถูกสร้างที่ `/work/demo.urf.json` (= `D:\android-controller\data\demo.urf.json`)

### โหมด 2: RAW + Auto-Annotate

เหมือน raw แต่ระบบจะหา element ที่ถูกกดให้อัตโนมัติ (เสถียรกว่าเมื่อ replay):
```powershell
docker compose exec controller universal-recorder.py --mode raw --annotate -p com.example.app -o /work/demo.urf.json
```

### โหมด 3: ANNOTATED — เลือก element ทีละตัว

แบบ interactive, เลือก element จาก UI tree:
```powershell
docker compose exec controller universal-recorder.py --mode annotated -p com.example.app -o /work/demo.urf.json
```
คำสั่งในโหมด annotated:
| คำสั่ง | หน้าที่ |
|--------|---------|
| `t` | ดึง UI tree แล้วเลือก element จากรายการ |
| `tap 540 800` | แตะพิกัดตรง ๆ |
| `s home` | ถ่ายหน้าจอ (screenshot) |
| `w 2000` | รอ 2 วินาที |
| `k BACK` | กดปุ่ม BACK |
| `done` | จบการบันทึก |

### ตารางสรุป flags ของ universal-recorder.py

| Flag | ค่า | หน้าที่ |
|------|-----|---------|
| `--mode` / `-m` | `raw` หรือ `annotated` | โหมดบันทึก |
| `--package` / `-p` | `com.example.app` | Package name ของแอป |
| `--output` / `-o` | path | ที่เก็บไฟล์ (default: `/work/recording.urf.json`) |
| `-s` / `--serial` | serial/IP:port | ระบุอุปกรณ์ |
| `--annotate` / `-a` | (flag) | เปิด auto-annotate ใน raw mode |
| `--name` / `-n` | text | ตั้งชื่อ recording |

---

## Step 4: แก้ไขสคริปต์ (Edit)

ไฟล์ `.urf.json` ที่อัดมาสามารถแก้ไขได้ 2 วิธี:

### วิธี A: Web UI Editor (แนะนำ) — port 9090

```powershell
docker compose exec controller flow-editor-web.py --port 9090 --dir /work
```
เปิดเบราว์เซอร์: **http://localhost:9090**

**Layout ของ Web Editor:**
```
+----------+----------------------------------+-------------------+
| FILES    | TIMELINE                         | FRAME DETAIL      |
| -----    | [All Frames][Script Only][Raw]   | ---------         |
| flow1    | [+ Add][Insert][Dup][Delete]     | ID: 1             |
| flow2    |                                  | Time: 0 ms        |
|          | #1 [0.0s] APP   open: myapp      | Type: [gesture v] |
| META     | #2 [3.0s] WAIT  3000ms           | Action: [tap v]   |
| -----    | #3 [6.0s] ELEMENT tap: Login     | X: 540  Y: 1200  |
| Name:    | #4 [7.0s] SCREENSHOT home        | Element: ____     |
| Package: | #5 [8.0s] GESTURE tap (540,1200) | Note: ____        |
| Device:  | #6 [10.0s] GESTURE swipe         |                   |
|          | #7 [12.0s] KEY   BACK            |                   |
| SETTINGS |                                  |                   |
| -----    | (drag & drop to reorder)         |                   |
| Speed:   |                                  |                   |
| Loop:    +----------------------------------+                   |
| OnError: | BULK: [Collapse][Compact]        |                   |
| Retries: |       [Scale Time][Shift Time]   |                   |
+----------+----------------------------------+-------------------+
```

**ฟีเจอร์หลัก:**

| ฟีเจอร์ | รายละเอียด |
|---------|------------|
| 3 View Modes | **All Frames** (raw + script), **Script Only**, **Raw Only** |
| Frame สี-coded | touch=ส้ม, gesture=เขียว, element=น้ำเงิน, app=ม่วง, key=ฟ้า, screenshot=ชมพู, wait=เทา, shell=แดง |
| Drag & drop | ลากเรียงลำดับ frames |
| Edit ทุก field | คลิก frame > แก้ type, timing, coords, element, note ในแถบขวา |
| Add/Insert | เพิ่ม frame ใหม่ 9 ประเภท: gesture, element, app, screenshot, key, wait, swipe, shell, marker |
| Screenshot | แทรก screenshot เป็น frame ในไทม์ไลน์ เพื่ออ้างอิงภาพระหว่างแก้ไข |
| Toggle | เปิด/ปิด frame (disabled = ข้ามตอน replay) |
| Bulk: Collapse | ยุบ raw touches ให้เป็น gestures |
| Bulk: Scale/Shift | ปรับเวลาทุก frame พร้อมกัน |
| Save/Export | บันทึก, Save As, Export เฉพาะ script-only |

**การใช้งาน Screenshot frame (Flow Editor Web):**
- กด **Add** หรือ **Insert** แล้วเลือก **Screenshot** เพื่อสร้าง frame ใหม่
- คลิก frame ที่สร้างขึ้น แล้วกำหนด **Stage** (ชื่อจุดที่ถ่าย) และ **Send** (Yes/No) ในแถบขวา

### วิธี B: CLI Editor

```powershell
docker compose exec controller flow-editor.py /work/demo.urf.json
```

คำสั่งในโหมด CLI:
```
l [N]           - แสดง frames (ล่าสุด N, default 30)
d <#id>         - ดูรายละเอียด frame
del <#id>       - ลบ frame
dis <#id>       - ปิดใช้งาน frame (ข้ามตอน replay)
en <#id>        - เปิดใช้งาน frame
i <pos> <type>  - แทรก frame (tap/swipe/wait/screenshot/app/key/shell)
m <#id> <pos>   - ย้าย frame ไปตำแหน่ง
t <#id> <+/-ms> - ปรับเวลา (เช่น t 5 +500)
t all <scale>   - ปรับเวลาทุก frame (เช่น t all 2.0 = ช้า 2x)
t shift <ms>    - เลื่อนเวลาทุก frame
n <#id> <text>  - ใส่โน้ต
speed <val>     - ตั้งความเร็ว (เช่น speed 1.5)
loop [on|off|N] - ตั้ง loop
error <mode>    - ตั้งโหมด error (retry/skip/pause/stop)
collapse        - ยุบ raw touches > gestures
compact         - ลบ disabled frames + renumber
merge <file>    - รวมไฟล์อื่นเข้ามา
export <file>   - Export เฉพาะ gestures
s               - บันทึก
sa <file>       - Save as
undo            - ย้อนกลับ
q               - ออก
```

---

## Step 5: เล่นซ้ำ (Replay)

### เล่นพื้นฐาน
```powershell
docker compose exec controller universal-player.py /work/demo.urf.json
```

### เล่นด้วยตัวเลือกต่าง ๆ
```powershell
# เร็ว 2 เท่า + วนลูป
docker compose exec controller universal-player.py /work/demo.urf.json --speed 2.0 --loop

# วนลูป 5 รอบ
docker compose exec controller universal-player.py /work/demo.urf.json --loop-count 5

# เล่นเฉพาะ gesture/element (ข้าม raw touches, เสถียรกว่า)
docker compose exec controller universal-player.py /work/demo.urf.json --replay-mode smart

# เล่นเฉพาะ raw touches (ตามพิกัดเดิมเป๊ะ)
docker compose exec controller universal-player.py /work/demo.urf.json --replay-mode raw

# Dry run (แสดงสิ่งที่จะทำ ไม่ได้ทำจริง)
docker compose exec controller universal-player.py /work/demo.urf.json --dry-run

# เปิด debug dashboard ที่ port 8080
docker compose exec controller universal-player.py /work/demo.urf.json --debug-port 8080
```

### ตารางสรุป flags ของ universal-player.py

| Flag | ค่า | หน้าที่ |
|------|-----|---------|
| `recording` | path | ไฟล์ .urf.json ที่จะเล่น |
| `--replay-mode` | `auto` / `smart` / `raw` | โหมดเล่น (default: auto) |
| `--speed` | float | ความเร็ว (2.0 = เร็ว 2x, 0.5 = ช้า 2x) |
| `--loop` | (flag) | เปิดวนลูป |
| `--no-loop` | (flag) | ปิดวนลูป |
| `--loop-count` | int | วนลูปกี่รอบ |
| `--dry-run` | (flag) | แสดงอย่างเดียว ไม่ทำจริง |
| `--debug-port` | int | เปิด debug dashboard |
| `-s` / `--serial` | serial/IP:port | ระบุอุปกรณ์ |

### 3 โหมดเล่น:
| โหมด | อธิบาย |
|------|--------|
| `auto` (default) | ใช้ gesture ถ้ามี, ไม่มีใช้ raw |
| `smart` | ใช้เฉพาะ gesture + element (เสถียรที่สุด ข้ามความละเอียดจอ) |
| `raw` | ใช้เฉพาะ raw touch events (ตามพิกัดเดิม) |

---

## คำสั่งใช้งานอื่น ๆ

### สั่งงานจาก Windows (ไม่ต้องเข้า container)

```powershell
# จับภาพหน้าจอ
.\scripts\capture-screenshot.ps1

# ลบรูปเก่าในโฟลเดอร์ img (ค่าเริ่มต้นเก่ากว่า 7 วัน)
.\scripts\delete-old-images.ps1
.\scripts\delete-old-images.ps1 -OlderThanDays 3 -Force

# ปลุกหน้าจอ
.\scripts\awake-device.ps1

# สั่ง tap/swipe/key
.\scripts-controller\control-device.ps1 -Action Key -KeyName HOME
.\scripts-controller\control-device.ps1 -Action Tap -X 540 -Y 1200
.\scripts-controller\control-device.ps1 -Action Swipe -X 200 -Y 1200 -X2 900 -Y2 200 -Duration 300
```

### คำสั่ง ADB พื้นฐาน (รันในคอนเทนเนอร์)

```powershell
docker compose exec controller bash    # เข้าเชลล์
```
```bash
# ข้อมูลเครื่อง
adb shell getprop ro.product.model
adb shell getprop ro.build.version.release

# แตะ/ปัด
adb shell input tap 540 1200
adb shell input swipe 200 1200 200 200 300

# ติดตั้ง/ถอนแอป
adb install -r /work/app.apk
adb shell pm uninstall com.example.app

# จอภาพ
adb shell screencap -p /sdcard/s.png && adb pull /sdcard/s.png /work/
```

### Dump UI + Screenshot

```bash
capture-ui-and-screen.py -g login-screen -s 172.20.10.9:34557
# ไฟล์: /work/ui-dumps/<timestamp>-login-screen.xml + .png
```

### วาด marker จุดกดบนสกรีนช็อต

```bash
overlay-touches.py /work/touch-events.json /work/ui-dumps/20240901-120000-login-screen.png
```

---

## สรุปเครื่องมือทั้งหมด

| เครื่องมือ | คำสั่ง | หน้าที่ |
|-----------|--------|---------|
| **Universal Recorder** | `universal-recorder.py` | อัด 2 โหมด: raw (แตะจริง) + annotated (เลือก element) |
| **Universal Player** | `universal-player.py` | เล่นไฟล์ .urf.json ทั้ง raw + script ผสมกัน |
| **Flow Editor (Web)** | `flow-editor-web.py` | แก้ไขสคริปต์ด้วย Web UI (drag & drop, visual) |
| **Flow Editor (CLI)** | `flow-editor.py` | แก้ไขสคริปต์ด้วย command line |
| **Format Library** | `universal_format.py` | Format กลาง .urf.json |
| **Capture UI+Screen** | `capture-ui-and-screen.py` | ดึง UI dump + screenshot |
| **Touch Capture** | `touch-event-capture.py` | จับ touch events เป็น JSON/CSV |
| **Overlay Touches** | `overlay-touches.py` | วาดจุดกดลงบนสกรีนช็อต |
| **Replay Log** | `replay-log.py` | เล่น log เดิม (JSON/CSV) |
| **Control Device** | `control-device.ps1` | สั่ง tap/swipe/key จาก Windows |
| **Screenshot** | `capture-screenshot.ps1` | จับภาพจาก Windows |
| **Delete Old Images** | `delete-old-images.ps1` | ลบรูปเก่าในโฟลเดอร์ `img` ตามจำนวนวัน |
| **Connect Wireless** | `connect-wireless.ps1` | เชื่อมต่อ Wi-Fi อัตโนมัติ |
| **Awake Device** | `awake-device.ps1` | ปลุกหน้าจอ |

---

## Ports ที่เปิด

| Port | ใช้งาน |
|------|--------|
| 5037 | ADB server |
| 8080 | Debug dashboard (automation engine / player) |
| 9090 | Visual Flow Editor (Web UI) |

---

## การหยุดงาน/ปิดระบบ

```powershell
cd D:\android-controller
docker compose down
```

ล้างโปรเจกต์:
```powershell
docker compose down --remove-orphans --rmi local
docker builder prune -f
```

---

## Troubleshooting

- **`device unauthorized`** — ถอด-เสียบสายใหม่, ปลดล็อกจอ, ตอบ **Allow** ที่ RSA popup (ติ๊ก Always allow)
- **`offline` (Wi-Fi)** — `adb disconnect <IP:PORT>` แล้ว `adb connect` ใหม่, ตรวจว่าอยู่ Wi-Fi เดียวกัน
- **ไม่เห็นอุปกรณ์ (USB)** — เช็กว่า `usbipd attach` สำเร็จ, ลองรีโหลด:
  ```powershell
  docker compose exec adb-server bash -lc "adb kill-server || true; adb start-server -a -H 0.0.0.0 -P 5037"
  ```
- **ADB pairing ล้มเหลว** — เปิดหน้า Wireless debugging ใหม่ > Pair device อีกครั้ง (port/code เปลี่ยนทุกครั้ง)
- **หลายเครื่องพร้อมกัน** — ใช้ `-s <serial|ip:port>` กับทุกคำสั่ง
- **ไม่ใช่ต้องแก้ที่ Docker volume เหรอ?** — ใช่, เริ่มจากยืนยัน bind mount (`D:/android-controller/img:/img`) ก่อน แต่จากเคสที่เจอบ่อยคือ mount ถูกต้องแล้ว ไฟล์มีในคอนเทนเนอร์ แต่ Windows ยังไม่เห็นทันที:
  ```powershell
  docker compose config | Select-String "D:/android-controller/img:/img"
  docker inspect android-controller --format "{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}"
  dir D:\android-controller\img\captures
  ```
  > แนะนำใช้ `dir`/`Get-ChildItem` ตรวจไฟล์จริง เพราะ `tree /f` มักใช้ดูโครงสร้างโฟลเดอร์มากกว่าและอาจทำให้เข้าใจว่าไม่มีไฟล์

  ถ้า inspect แล้ว mapping ถูกต้องแต่ยังไม่เห็นไฟล์ ให้ใช้ `docker compose cp` เป็นทางลัดได้เลย แล้วค่อยเช็ก Docker Desktop > Settings > File sharing/WSL integration ของไดรฟ์ `D:`.
- **มีไฟล์ใน `/img/captures` แต่หาไม่เจอบน Windows** — ทำตามลำดับนี้
  1) ตรวจว่ามี volume mapping จริง:
     ```powershell
     docker compose config --services
     docker compose config | Select-String "D:/android-controller/img:/img"
     ```
  2) ตรวจจากฝั่งคอนเทนเนอร์:
     ```powershell
     docker compose exec controller ls -la /img/captures
     ```
  3) เปิดโฟลเดอร์ฝั่ง Windows โดยตรง:
     ```powershell
     explorer D:\android-controller\img\captures
     ```
  4) ถ้ายังไม่เห็นไฟล์ ให้คัดลอกออกจากคอนเทนเนอร์ตรง ๆ:
     ```powershell
     docker compose cp controller:/img/captures/. D:/android-controller/img/captures/
     ```
     หรือใช้สคริปต์ช่วย sync อัตโนมัติ:
     ```powershell
     .\scripts\sync-captures.ps1
     ```
     ถ้าต้องการลบไฟล์เก่าก่อน sync:
     ```powershell
     .\scripts\sync-captures.ps1 -DeleteOld
     ```
  5) ถ้าต้องการโฟลเดอร์ส่งต่อ (เช่น `/img/sent`) ให้สร้างเพิ่ม:
     ```powershell
     mkdir D:\android-controller\img\sent
     ```

---

## ข้อควรระวังด้านความปลอดภัย

- เลือกใช้เฉพาะเครือข่ายที่ไว้ใจได้ (โดยเฉพาะโหมด Wi-Fi)
- ปิด **Wireless debugging** เมื่อไม่ใช้งาน
- เก็บโฟลเดอร์ `adbkeys` ให้ปลอดภัย เพราะมี private keys สำหรับการเชื่อมต่อ ADB

---

## ใบอนุญาต

MIT (แก้ไขใช้ภายในองค์กร/โครงการได้อิสระ)
