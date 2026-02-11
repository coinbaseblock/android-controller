# readme-step-by-step.md — Android Controller (ADB via Docker) for Galaxy A17

เอกสาร **Step‑by‑Step** สำหรับควบคุม Android (เช่น **Galaxy A17**) ผ่าน **Docker** โดยให้ **ADB Server** รันในคอนเทนเนอร์ และรองรับทั้ง **USB‑C** และ **Wi‑Fi (Wireless debugging)**  
โฟลเดอร์ทำงานอยู่ที่ **`D:\android-controller`** — เก็บข้อมูลบนไดรฟ์ **D:** ไม่แตะไดรฟ์ C

---

## 0) โครงสร้างโปรเจกต์
```
D:ndroid-controller├─ docker-compose.yml
├─ controller│  └─ Dockerfile
├─ adbkeys\        # เก็บคีย์ ADB (persist)
└─ data\           # พื้นที่งาน (APK / รูป / logs) => แม็พเข้า /work ในคอนเทนเนอร์
```

> ใน `docker-compose.yml` มี 2 services:
> - `adb-server`  : เปิด ADB server ในคอนเทนเนอร์ (รองรับ USB + Wi‑Fi)
> - `controller`  : ADB client ชี้ไปที่ `tcp:127.0.0.1:5037` ภายใน network เดียวกัน

---

## 1) ข้อกำหนดล่วงหน้า (Prerequisites)

- Windows 11 + WSL2 + Docker Desktop (เปิด **Use the WSL 2 based engine**)
- ติดตั้ง **usbipd‑win 5.x** (ไวยากรณ์ใหม่)  
  ตรวจสอบเวอร์ชัน:
  ```powershell
  usbipd --version
  ```
- บนโทรศัพท์ Android เปิด **Developer options** แล้วเปิดอย่างน้อย:
  - ✅ **USB debugging**
  - ✅ (ตัวเลือก) **Wireless debugging**
  - ✅ (แนะนำ) **Disable adb authorization timeout**
  - ✅ (แนะนำ) **Stay awake** และตั้ง **Default USB configuration = File transfer**

> **สำคัญ:** ทุกคำสั่ง `usbipd` ให้รันบน **Windows PowerShell (Run as Administrator)** เท่านั้น

---

## 2) สตาร์ทคอนเทนเนอร์
```powershell
cd D:ndroid-controller
docker compose up -d --build
docker compose logs -f adb-server
# ต้องเห็น: ADB server is running on 0.0.0.0:5037
```

---

## 3) โหมด USB‑C (เสถียรที่สุด)

> ใช้ไวยากรณ์ **usbipd‑win 5.x** (ไม่มีคำว่า `wsl` ตรงกลางอีกแล้ว)

1) เสียบสาย USB‑C กับ Galaxy A17 (ปลดล็อกจอ)
2) เปิด PowerShell **Run as Administrator** แล้วดู BUSID:
   ```powershell
   usbipd list
   # หาแถว Samsung; ตัวอย่าง: 4-4  04e8:6860  Galaxy A17 ...  STATE=Not shared/Shared
   ```
3) แชร์อุปกรณ์ (ถ้าเห็น Not shared):
   ```powershell
   usbipd bind --busid 4-4
   ```
4) แนบอุปกรณ์เข้า WSL ที่ Docker ใช้ (และตั้ง auto‑attach):
   ```powershell
   # เลือกวิธีใดวิธีหนึ่ง
   usbipd attach --wsl docker-desktop --busid 4-4 --auto-attach
   # หรือ
   usbipd attach --wsl --busid 4-4 --auto-attach
   ```
5) ตรวจสอบสถานะ:
   ```powershell
   usbipd list
   # ต้องเห็น BUSID = 4-4 เป็น STATE = Attached
   ```
6) ยืนยันว่า USB โผล่ถึงคอนเทนเนอร์ฝั่ง server:
   ```powershell
   docker compose exec adb-server bash -lc "lsusb | grep -i 04e8 || lsusb"
   # ควรเห็น 04e8:6860 (Samsung)
   ```
7) รีสตาร์ท ADB server (กันพลาด):
   ```powershell
   docker compose exec adb-server bash -lc "adb kill-server || true; adb start-server -a -H 0.0.0.0 -P 5037"
   ```
8) ขอรายการอุปกรณ์จากฝั่ง client:
   ```powershell
   docker compose exec controller bash -lc "adb devices -l"
   ```
   - **บนมือถือ** จะเด้งป๊อปอัป “Allow USB debugging?” → ติ๊ก **Always allow** → กด **Allow**
   - แล้วสั่งซ้ำ `adb devices -l` ควรเห็น `<serial>  device` ✅

> ถ้า `usbipd list` เด้งกลับเป็น **Not shared** หลังถอดสาย/รีบูต ให้ทำซ้ำข้อ 3–5 อีกครั้ง (แนะนำใช้ `--auto-attach`).

---

## 4) โหมด Wi‑Fi (Wireless debugging) — ไม่ต้องใช้ usbipd

1) มือถือ → Developer options → **Wireless debugging** → **Pair device with pairing code**  
   จด **IP**, **Pair port**, **Pairing code** และดู **IP address & Port** สำหรับ `adb connect` (อาจไม่ใช่ 5555)
2) เข้า shell ของ `controller`:
   ```powershell
   docker compose exec controller bash
   ```
3) จับคู่ + เชื่อมต่อ:
   ```bash
   adb pair <PHONE_IP>:<PAIR_PORT> <PAIRING_CODE>
   adb connect <PHONE_IP>:<ADB_PORT>   # ใช้พอร์ตที่หน้า Wireless debugging แสดง
   adb devices -l
   ```
เห็น `device` ก็ใช้งานได้ทันที

---

## 5) ตัวอย่างคำสั่งใช้งาน (รันจากใน `controller`)

> ไฟล์งานอยู่ที่ `/work` (แม็พกับ `D:ndroid-controller\data`)

```bash
# ข้อมูลเครื่อง
adb shell getprop ro.product.model
adb shell getprop ro.build.version.release

# แตะ/ปัดหน้าจอ
adb shell input tap 540 1200
adb shell input swipe 200 1200 200 200 300

# ติดตั้ง/ถอนแอป
adb install -r /work/app.apk
adb shell pm uninstall com.example.app

# จอภาพ/ไฟล์
adb shell screencap -p /sdcard/s.png && adb pull /sdcard/s.png /work/
adb pull /sdcard/Android/data/com.example.app/files/log.txt /work/

# logcat
adb logcat
```

> ถ้าเชื่อมหลายเครื่องพร้อมกัน ให้ระบุ target ด้วย `-s <serial|ip:port>`

---

## 6) Troubleshooting แบบเร็ว

- **`adb devices -l` ว่าง** แต่ `lsusb` เห็น 04e8:  
  → เช็กบนมือถือว่ามีป๊อปอัป **Allow USB debugging** ไหม, เปลี่ยน **Default USB configuration = File transfer**, ถอด‑เสียบสายใหม่, ทำข้อ 3–5 ซ้ำ

- **STATE เด้งเป็น Not shared**:  
  ```powershell
  usbipd detach --busid 4-4
  usbipd bind   --busid 4-4
  usbipd attach --wsl docker-desktop --busid 4-4 --auto-attach
  usbipd list
  ```

- **udevadm error: `Failed to send reload request`**:  
  ข้ามได้ เพราะเราไม่ได้พึ่ง `udevd` ในภาพนี้ (ใช้ root + map `/dev/bus/usb` ตรงอยู่แล้ว)

- **Wi‑Fi ขึ้น `offline`**:  
  `adb disconnect <ip:port>` แล้ว `adb connect` ใหม่ (มือถือ/พีซีต้องอยู่ Wi‑Fi เดียวกัน)

---

## 7) ปิดงาน / ล้างงาน

ปิดเฉพาะโปรเจกต์นี้:
```powershell
docker compose down
```

ถอด USB ออกจาก WSL:
```powershell
usbipd detach --busid 4-4
```

ล้างภาพที่ build จากโปรเจกต์นี้ + cache (ไม่กระทบ Docker อื่น):
```powershell
docker compose down --remove-orphans --rmi local
docker builder prune -f
```

ลบข้อมูลที่แม็พไว้ (ตัวเลือก):
```powershell
Remove-Item -Recurse -Force D:ndroid-controllerdbkeys
Remove-Item -Recurse -Force D:ndroid-controller\data
```

---

## 8) เช็คลิสต์ “ใช้งาน USB‑C สำเร็จ”
- [ ] `usbipd list` → Galaxy A17 = **Attached**
- [ ] `docker compose logs -f adb-server` → `ADB server is running on 0.0.0.0:5037`
- [ ] `docker compose exec adb-server bash -lc "lsusb | grep -i 04e8 || lsusb"` → เห็น `04e8:6860`
- [ ] โทรศัพท์เด้ง **Allow USB debugging** → กด **Allow**
- [ ] `docker compose exec controller bash -lc "adb devices -l"` → เห็น `device`

---

> เคล็ดลับ: เมื่อเปิดพอร์ต `5037:5037` แล้ว จะสามารถใช้ ADB จาก Windows ชี้ไปยัง ADB server ในคอนเทนเนอร์ได้ เช่น  
> ```powershell
> adb -H 127.0.0.1 -P 5037 devices -l
> ```

ขอให้สนุกกับการควบคุม Galaxy A17 ผ่าน Docker ครับ! 🚀
