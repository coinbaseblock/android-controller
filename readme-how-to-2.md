# readme-how-to-2.md — Android Controller (ADB via Docker) for Galaxy A17  
ควบคุมเครื่อง Android ผ่าน Docker, ใช้ได้ทั้ง **USB** และ **Wi-Fi**, เซฟสกรีนช็อตลง `D:\android-controller\data`

> โปรเจกต์: `D:\android-controller`  
> Services: `adb-server` (ADB daemon) + `controller` (ADB client, ชี้ไป `tcp:127.0.0.1:5037`)  
> โฟลเดอร์ `data` ถูกแม็พเป็น `/work` ในคอนเทนเนอร์

---

## 0) ก่อนเริ่ม
- Windows 11 + WSL2 + Docker Desktop (WSL2 engine)
- ติดตั้ง **usbipd-win 5.x** (ไวยากรณ์ใหม่ ไม่มี `usbipd wsl list`)
  ```powershell
  usbipd --version
  ```
- บนมือถือ (แนะนำเปิดครบ):
  - Developer options → **USB debugging**
  - (แนะนำ) **Wireless debugging**
  - (แนะนำ) **Disable adb authorization timeout**
  - **Default USB configuration = File transfer (MTP)**
  - ปลดล็อกจอไว้ตอนเสียบสาย

> หมายเหตุ: คำสั่ง `usbipd` ให้รัน PowerShell แบบ **Run as Administrator**

---

## 1) สตาร์ท/หยุด Docker (จำให้ขึ้นใจ)
```powershell
cd D:\android-controller
docker compose up -d --build      # สตาร์ท
docker compose logs -f adb-server # ควรเห็น: ADB server is running on 0.0.0.0:5037
```

หยุดงาน:
```powershell
cd D:\android-controller
docker compose down
```

---

## 2) โหมด A — USB (เสถียรสุด)
### 2.1 แชร์ + แนบ USB เข้า WSL (ที่ Docker ใช้)
```powershell
usbipd list
# หา BUSID ของ Samsung (เช่น 4-4, VID:PID = 04e8:6860)

usbipd detach --busid 4-4
usbipd bind   --busid 4-4
usbipd attach --wsl --busid 4-4 --auto-attach
usbipd list   # ต้องเห็น STATE = Attached
```

### 2.2 เช็คในคอนเทนเนอร์
```powershell
docker compose exec adb-server bash -lc "lsusb | grep -i 04e8 || lsusb"
# ควรเห็น 04e8:6860 (Samsung)

docker compose exec adb-server bash -lc "adb kill-server || true; adb start-server -a -H 0.0.0.0 -P 5037"
```

### 2.3 **กด Allow บนมือถือ**
เสียบสายแล้วมือถือจะเด้ง **“Allow USB debugging?”** → ติ๊ก **Always allow** → **Allow**

ตรวจจากฝั่ง client:
```powershell
docker compose exec controller bash -lc "adb devices -l"
# เห็น <serial>  device  = พร้อมใช้งาน
```

---

## 3) โหมด B — Wi-Fi (Wireless debugging)
1) มือถือ → Developer options → **Wireless debugging** → **Pair device with pairing code**  
   จด: **PHONE_IP**, **PAIR_PORT**, **PAIRING_CODE**, และ **ADB_PORT** (พอร์ต connect จริง อาจไม่ใช่ 5555)

2) สั่งจากฝั่ง client:
```powershell
# ตัวอย่างจริง (เปลี่ยนเป็นค่าของคุณเอง):
# PHONE_IP=10.1.1.242  PAIR_PORT=41519  PAIRING_CODE=320302  ADB_PORT=36831

docker compose exec controller bash -lc "adb pair 10.1.1.242:41519 320302"
docker compose exec controller bash -lc "adb connect 10.1.1.242:36831"
docker compose exec controller bash -lc "adb devices -l"
# เห็น 10.1.1.242:36831  device  = พร้อมใช้งาน
```
> ถ้าเจอ `protocol fault` หรือ `connection refused` ให้กด Pair ใหม่บนมือถือ (เลขคู่/พอร์ตเปลี่ยนทุกครั้ง)

---

## 4) เซฟสกรีนช็อตลง `D:\android-controller\data`
> โฟลเดอร์ `data` = `/work` ในคอนเทนเนอร์

### 4.1 วิธีเร็ว (stream ออกมา)
- **USB (เครื่องเดียว):**
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
docker compose exec -T controller sh -lc "adb -d exec-out screencap -p" > "D:\android-controller\data\screen-$ts.png"
start D:\android-controller\data
```
- **Wi-Fi (ระบุเครื่อง):**
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
docker compose exec -T controller sh -lc "adb -s 10.1.1.242:36831 exec-out screencap -p" > "D:\android-controller\data\screen-$ts.png"
start D:\android-controller\data
```

### 4.2 วิธีสำรอง (ดันไฟล์ลง /sdcard แล้ว pull)
```powershell
docker compose exec controller bash -lc ^
  'TS=$(date +%Y%m%d-%H%M%S); \
   adb -s 10.1.1.242:36831 shell screencap -p /sdcard/s.png && \
   adb -s 10.1.1.242:36831 pull /sdcard/s.png /work/screen-$TS.png'
start D:\android-controller\data
```

---

## 5) ทริก/แก้ปัญหาไว
- `adb devices -l` ว่าง แต่ `lsusb` เห็น 04e8:
  - มือถือยังไม่ **Allow USB debugging**
  - ตั้ง **USB = File transfer (MTP)**, ปลด-เสียบสายใหม่, จอปลดล็อก
  - ลอง **Developer options → Revoke USB debugging authorizations** แล้วเสียบใหม่
  - เปลี่ยนสาย/พอร์ต (ต้องเป็นสายถ่ายโอนข้อมูลจริง)
- `usbipd attach` ติด ๆ ดับ ๆ:
  ```powershell
  usbipd detach --busid 4-4
  usbipd bind   --busid 4-4
  usbipd attach --wsl --busid 4-4 --auto-attach
  usbipd list
  ```
- รีเฟรช ADB ทั้งฝั่ง server/client:
  ```powershell
  docker compose exec adb-server bash -lc "adb kill-server || true; adb start-server -a -H 0.0.0.0 -P 5037"
  docker compose exec controller  bash -lc "adb kill-server || true; sleep 1; adb devices -l"
  ```
- ใช้ ADB จาก Windows ยิงเข้า ADB server ในคอนเทนเนอร์:
  ```powershell
  adb -H 127.0.0.1 -P 5037 devices -l
  ```
- Wi-Fi โชว์ `offline`:
  - `adb disconnect <ip:port>` → `adb connect <ip:port>` ใหม่
  - มือถือ/พีซี ต้องอยู่ Wi-Fi เดียวกัน และเปิดหน้าจอ Wireless debugging ไว้ตอน pair/connect

### `docker compose ps` ชื่อไม่ตรงกับ service และ `CREATED` ไม่เปลี่ยน ต้องทำยังไง?
- `docker compose config --services` จะแสดง **ชื่อ service** เช่น `adb-server`, `controller`
- `docker compose ps` คอลัมน์ `NAME` จะแสดง **container name ที่รันจริง** (อาจถูกตั้งด้วย `container_name`) จึงเห็นเป็น `android-controller` ได้ ถือว่าปกติ
- ถ้า `docker compose up -d --build` แล้ว `CREATED` ยังเป็นหลายวันก่อน แปลว่าคอนเทนเนอร์เดิมยังถูก reuse อยู่ (ยังไม่ได้ recreate)

บังคับ recreate เฉพาะ `controller` (ปลอดภัยและชัวร์สุด):
```powershell
cd D:\android-controller
docker compose rm -sf controller
docker compose up -d --build --force-recreate --no-deps controller
docker compose ps
```

บังคับ recreate ทั้ง stack:
```powershell
cd D:\android-controller
docker compose down
docker compose up -d --build --force-recreate
docker compose ps
```

กรณีต้อง rebuild แบบไม่ใช้ cache:
```powershell
cd D:\android-controller
docker compose build --no-cache controller
docker compose rm -sf controller
docker compose up -d --force-recreate --no-deps controller
docker compose ps
```

หมายเหตุ ADB:
- recreate แค่ `controller` มัก **ไม่ต้อง pair ใหม่** เพราะ `adb-server` ยังอยู่
- ถ้า recreate `adb-server` หรือใช้ `down -v` มีโอกาสต้อง `adb pair` / `adb connect` ใหม่


### หลัง reboot แล้วอยากให้ต่อ Wi-Fi ADB อัตโนมัติ
สาเหตุที่ต้องต่อใหม่บ่อย ๆ: `adb connect` แบบ Wireless debugging จะผูกกับ `IP:PORT` ชั่วคราว ซึ่ง **port อาจเปลี่ยนหลัง reboot** เลยใช้ค่าเดิมไม่ได้ทุกครั้ง

วิธีที่แนะนำ: ใช้สคริปต์ startup ให้ `docker compose up` แล้ววนเรียก mDNS reconnect อัตโนมัติ
```powershell
cd D:\android-controller
.\scripts\startup-auto-connect.ps1 -Build -RetryForever
```

ตัวเลือกที่ใช้บ่อย:
```powershell
# จำกัด device เฉพาะเครื่องนี้ (IP หรือ IP:PORT)
.\scripts\startup-auto-connect.ps1 -Device 192.168.1.102 -RetryForever

# พยายาม 30 ครั้ง ครั้งละ 5 วินาที แล้วหยุด
.\scripts\startup-auto-connect.ps1 -MaxRetries 30 -RetryDelaySeconds 5
```

ให้รันอัตโนมัติหลังเข้า Windows (Task Scheduler):
```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\android-controller\scripts\startup-auto-connect.ps1" -RetryForever'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'AndroidControllerAutoConnect' -Action $action -Trigger $trigger -Description 'Start docker compose and auto reconnect wireless ADB' -User $env:USERNAME
```

หมายเหตุ:
- ครั้งแรกยังต้อง `adb pair` ให้สำเร็จก่อน (คีย์ถูกเก็บใน `D:/android-controller/adbkeys`)
- เปิด **Wireless debugging** บนมือถือไว้ตอนเครื่องเริ่มใหม่ เพื่อให้ mDNS หาเจอและ reconnect ได้

---

## 6) ปิดงาน/ล้างงาน
หยุดโปรเจกต์:
```powershell
cd D:\android-controller
docker compose down
```
ถอด USB ออกจาก WSL:
```powershell
usbipd detach --busid 4-4
```
เก็บกวาด (ตัวเลือก):
```powershell
docker compose down --remove-orphans --rmi local
docker builder prune -f
# ลบข้อมูลถาวรของโปรเจกต์ (ระวัง!)
# Remove-Item -Recurse -Force D:\android-controller\adbkeys
# Remove-Item -Recurse -Force D:\android-controller\data
```

---

## 7) เช็คลิสต์สำเร็จ (USB)
- [ ] `usbipd list` → Galaxy A17 = **Attached**
- [ ] `docker compose logs -f adb-server` → `ADB server is running on 0.0.0.0:5037`
- [ ] `lsusb` ใน `adb-server` เห็น `04e8:6860`
- [ ] มือถือกด **Allow USB debugging** (ติ๊ก **Always allow**)
- [ ] `adb devices -l` ใน `controller` เห็น `device`
- [ ] สกรีนช็อตถูกสร้างที่ `D:\android-controller\data\screen-*.png`

---

### สองคำสั่งที่อยากให้ส่งมาดูเวลาติดปัญหา
```powershell
usbipd list
docker compose exec controller bash -lc "adb devices -l"
```

— จบ —
