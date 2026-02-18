# คู่มือควบคุมอุปกรณ์ Android (`control-device.ps1`)

สคริปต์ PowerShell `scripts-controller/control-device.ps1` ช่วยให้สั่งงานอุปกรณ์ Android ผ่าน `adb` ภายในคอนเทนเนอร์ `controller` ได้สะดวก โดยครอบ `docker compose exec` ให้เหลือเพียงคำสั่งเดียวสำหรับแตะหน้าจอ ปัดหน้าจอ และส่งปุ่มกด

> ⚠️ ต้องเตรียมระบบตามโปรเจกต์หลัก (`docker compose up` ให้บริการ `controller` ทำงาน) และให้ Docker พร้อมใช้งานบน Windows ที่ไดเร็กทอรี `D:\android-controller` ก่อนจึงจะเรียกใช้สคริปต์ได้

## วิธีเรียกใช้งานเบื้องต้น

```powershell
cd D:\android-controller\scripts-controller
.\control-device.ps1 -Action <Tap|Swipe|Key> [ออปชัน]
```

ถ้าต้องการดูวิธีใช้ทั้งหมดให้ระบุ `-Help`

```powershell
.\control-device.ps1 -Help
```

## อาร์กิวเมนต์หลัก

| อาร์กิวเมนต์ | คำอธิบาย |
| --- | --- |
| `-Action` | ระบุชนิดการควบคุม `Tap`, `Swipe`, หรือ `Key` (จำเป็น) |
| `-Serial` | ระบุ serial ของอุปกรณ์ (กรณีมีหลายเครื่อง) จาก `adb devices` |
| `-Duration` | ระยะเวลาการปัดหน้าจอ (มิลลิวินาที) ค่าเริ่มต้น 300 |
| `-VerboseLog` | แสดงคำสั่ง `docker compose` ที่สคริปต์เรียกใช้งาน |

### Action: Tap

ใช้แตะหน้าจอที่จุดพิกัด X,Y ที่ต้องการ

```powershell
.\control-device.ps1 -Action Tap -X 540 -Y 1600
```

| อาร์กิวเมนต์ | คำอธิบาย |
| --- | --- |
| `-X` | พิกัดแนวนอน (จำเป็น) |
| `-Y` | พิกัดแนวตั้ง (จำเป็น) |

### Action: Swipe

ใช้ปัดหน้าจอจากจุด `(X, Y)` ไป `(X2, Y2)` พร้อมกำหนดระยะเวลาการปัดได้

```powershell
.\control-device.ps1 -Action Swipe -X 200 -Y 1000 -X2 900 -Y2 1000 -Duration 300
```

| อาร์กิวเมนต์ | คำอธิบาย |
| --- | --- |
| `-X` `-Y` | จุดเริ่มต้น (จำเป็น) |
| `-X2` `-Y2` | จุดปลายทาง (จำเป็น) |
| `-Duration` | ระยะเวลาในมิลลิวินาที (ไม่ระบุก็ใช้ค่าเริ่มต้น 300) |

### Action: Key

ใช้ส่งปุ่มกด Android เช่น `HOME`, `BACK`, `RECENTS` หรือใช้รหัส keycode ตามเอกสาร Android ได้

```powershell
# ใช้ชื่อปุ่ม
.\control-device.ps1 -Action Key -KeyName HOME

# ใช้รหัส keycode
.\control-device.ps1 -Action Key -Keycode 3
```

| อาร์กิวเมนต์ | คำอธิบาย |
| --- | --- |
| `-KeyName` | ชื่อปุ่ม Android (เช่น HOME, BACK, RECENTS) |
| `-Keycode` | หมายเลข keycode ของ Android |

> ต้องระบุอย่างใดอย่างหนึ่ง หากส่งทั้งสองอย่างหรือไม่ส่งเลย สคริปต์จะเตือนใช้งานผิด

## ตัวอย่างการใช้งานพร้อม Serial

หากมีหลายอุปกรณ์เชื่อมต่อ สามารถระบุ serial เพื่อเล็งเครื่องที่ต้องการ

```powershell
.\control-device.ps1 -Action Tap -X 300 -Y 900 -Serial emulator-5554
.\control-device.ps1 -Action Key -KeyName BACK -Serial 10.1.1.242:5555
```

## การทำงานภายในสคริปต์

1. ตรวจสอบว่ามี `docker compose` หรือ `docker-compose` ใน PATH
2. รัน `docker compose exec -T controller adb ...` เพื่อส่งคำสั่งไปยังคอนเทนเนอร์
3. ส่งคำสั่ง `adb shell input ...` ตาม Action ที่เลือก
4. แสดงสถานะ `Command completed successfully.` เมื่อคำสั่งสำเร็จ

สคริปต์จะออกจากการทำงานพร้อมแสดงวิธีใช้อัตโนมัติเมื่อใส่อาร์กิวเมนต์ไม่ครบหรือไม่ถูกต้อง ช่วยลดข้อผิดพลาดเวลาควบคุมอุปกรณ์จาก Windows