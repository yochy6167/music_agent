# התקנת סוכן מוזיקה על Raspberry Pi 4

מדריך מהיר: חבר חשמל + רשת → התקן OS פעם אחת → פרוס מהמחשב → בכל הפעלה מחדש הסוכן עולה לבד ומודיע לשרת על מכשיר חדש (עד שמשייכים לסניף בפאנל).

---

## מה קורה אחרי ההתקנה

1. **בהפעלה ראשונה** — הסוכן קורא את מספר הסידורי של ה-Pi (`/proc/cpuinfo`), יוצר רשומה ב-`config.json` עם `branch_id: 0` וללא `device_token`.
2. **רישום לשרת** — שולח `POST /api/v1/devices/register` עם `hardware_id`, `device_name` (שם המכונה), `branch_id: 0`.
3. **מצב ממתין** — השרת מחזיר `status: "pending"` עד שמנהל משייך את המכשיר לסניף בפאנל הניהול.
4. **אחרי שיוך** — הסוכן מקבל `device_token`, שומר ב-`config.json`, ומתחבר ב-WebSocket לנגן פלייליסטים.
5. **הפעלות הבאות** — `systemd` מריץ את השירות אוטומטית אחרי רשת.

---

## חלק א׳ — הכנת כרטיס SD (פעם אחת, מהמחשב)

### 1. תוכנה

התקן [Raspberry Pi Imager](https://www.raspberrypi.com/software/) ב-Windows.

### 2. מערכת הפעלה

בחר:

- **Raspberry Pi OS (64-bit)** — מומלץ **Lite** (ללא שולחן עבודה, יותר יציב לנגן).
- אחסון: כרטיס ה-SD שלך.

לחץ על **⚙️ הגדרות מתקדמות** (או Ctrl+Shift+X):

| הגדרה | המלצה |
|--------|--------|
| Hostname | `music-agent-01` (או שם ייחודי לכל מכשיר) |
| Enable SSH | ✓ Use password authentication (או מפתח SSH) |
| Username / Password | למשל `pi` + סיסמה חזקה |
| Configure wireless LAN | רק אם **אין** כבל Ethernet — מלא SSID וסיסמה |
| Set locale | `Asia/Jerusalem`, timezone `Asia/Jerusalem` |
| Eject when finished | ✓ |

**מומלץ:** חיבור **Ethernet** לראוטר — פשוט יותר, יציב יותר לסטרימינג.

### 3. כתיבה והפעלה

Write → הכנס SD ל-Pi → חבר Ethernet (או Wi‑Fi) → חשמל.

המתן ~2 דקות להפעלה ראשונה.

---

## חלק ב׳ — גילוי המכשיר ברשת (מהמחשב / WSL)

### Windows + WSL

```bash
# בדיקה שהמכשיר מגיב (החלף בשם שהגדרת)
ping -c 3 music-agent-01.local

# התחברות SSH
ssh pi@music-agent-01.local
```

אם `.local` לא עובד:

- בדוק בראוטר רשימת DHCP (חפש `music-agent-01` או `raspberrypi`).
- או סרוק: `sudo apt install arp-scan && sudo arp-scan --localnet`

### העתקת מפתח SSH (אופציונלי, נוח לפריסות חוזרות)

מהמחשב:

```bash
ssh-copy-id pi@music-agent-01.local
```

---

## חלק ג׳ — פריסת הסוכן (מהמחשב, פקודה אחת)

מתיקיית הפרויקט ב-WSL/Linux:

```bash
cd /home/yochanan/music_agent
chmod +x scripts/deploy-from-pc.sh
./scripts/deploy-from-pc.sh pi@music-agent-01.local
```

הסקריפט:

- מעתיק את הקוד (ללא `.venv`)
- יוצר `config.json` מ-`config.json.example` אם חסר
- מריץ `setup.sh` על ה-Pi (תלויות, venv, שירות `systemd`)

### פריסה ידנית (אלטרנטיבה)

על ה-Pi:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone yochy6167/music_agent
cd ~/music_agent
cp config.json.example config.json
# ערוך config.json אם כתובות השרת שונות
bash setup.sh
```

---

## חלק ד׳ — שיוך לסניף בפאנל

1. ודא שהסוכן רץ: `sudo systemctl status music_agent`
2. בפאנל הניהול — מכשירים ממתינים / רישום חדש.
3. שייך את המכשיר (לפי `hardware_id` / שם) לסניף הרצוי.
4. תוך עד ~30 שניות הסוכן יקבל token ויתחבר (לוג: `Device registration` → הפסקת `pending`).

צפייה בלוגים:

```bash
journalctl -u music_agent -f
```

---

## חלק ה׳ — בדיקות ותחזוקה

| פעולה | פקודה |
|--------|--------|
| סטטוס | `sudo systemctl status music_agent` |
| הפעלה מחדש | `sudo systemctl restart music_agent` |
| לוגים | `journalctl -u music_agent -f` |
| עדכון קוד | `./scripts/deploy-from-pc.sh pi@music-agent-01.local` |
| גרסת סוכן | בשורה הראשונה בלוג: `--- Version 2.0.0 ---` |

### אודיו (חיבור רמקולים)

ב-Raspberry Pi OS חדש השמע מנוהל דרך **PulseAudio/PipeWire** (לא `raspi-config`).

`setup.sh` מגדיר אוטומטית:

- עוצמת מערכת **100%** לפני כל הפעלה של הסוכן (`scripts/set-system-volume.sh`)
- `loginctl enable-linger` כדי ש-PipeWire יעלה גם ב-headless

```bash
# בדיקת עוצמת מערכת
pactl get-sink-volume @DEFAULT_SINK@

# בדיקת עוצמת ALSA (לעיתים PCM נפרד)
amixer -c 0 sget 'PCM'
```

**שתי שכבות עוצמה:** מערכת (`pactl`) + נגן VLC (מהדשבורד, ברירת מחדל 50%). אם 50% בדשבורד נשמע חלש — העלה בדשבורד ל-80–100%.

### HDMI למגבר בסניף (בלי פקודות ידניות)

`setup.sh` מכין אוטומטית:

1. **`hdmi_force_hotplug=1`** ב-`/boot/firmware/config.txt` — HDMI פעיל גם בלי מסך (מגבר דלוק לפני ה-Pi).
2. **`AUDIO_PREFER=auto`** — לפני כל הפעלה של הסוכן:
   - אם יש sink של **HDMI** → בוחר אותו + 100% עוצמה
   - אחרת → **שקע אוזניות** (בית / בדיקות)

**בסניף:** חבר HDMI למגבר, הדלק מגבר ואז Pi (או reboot). אין צורך ב-`pactl` ידני.

אם תמיד HDMI בלבד (בלי אוזניות): אחרי `setup.sh` אפשר לערוך:
`sudo systemctl edit music_agent` → `Environment=AUDIO_PREFER=hdmi`

המשתמש `pi` נוסף לקבוצות `audio` ו-`video` ב-`setup.sh`.

### כתובות שרת

ב-`config.json`:

```json
{
  "api_url": "https://sev.neeman-music.online",
  "ws_url": "wss://ws.neeman-music.online"
}
```

מערך `devices` נוצר אוטומטית בהרצה ראשונה — **אין צורך** למלא אותו ידנית לפני הרישום.

---

## חלק ו׳ — גישה מרחוק מכל מקום (Tailscale)

בעיה נפוצה: כדי להתחבר ל-Pi בסניף (SSH/לוגים/בדיקות) צריך להיות מחובר לאותה רשת Wi-Fi/Ethernet פיזית שלו. **Tailscale** פותר את זה — VPN רשתי חינמי שנותן ל-Pi כתובת IP קבועה שנגישה מכל מקום בעולם, בלי לפתוח פורטים בראוטר ובלי לגעת בקוד של `music_agent`.

צריך להתקין את זה **פעם אחת בלבד** בזמן שאתה כבר על אותה רשת של הסניף (או פיזית מול ה-Pi) — מאז זה עובד לצמיתות מכל מקום, כולל אחרי `reboot`.

### 1. התקנה על ה-Pi (בסניף, פעם אחת)

בזמן שאתה מחובר לאותו Wi-Fi/רשת של הסניף:

```bash
ssh pi@music-agent-01.local
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

הפקודה תדפיס קישור להתחברות (`https://login.tailscale.com/a/...`) — פתח אותו בדפדפן בכל מכשיר (גם בנייד) והתחבר עם חשבון Google/Microsoft/GitHub. ה-Pi ירשם אוטומטית ל-tailnet שלך.

הדגל `--ssh` מפעיל את שרת ה-SSH המובנה של Tailscale — כך אין צורך לנהל מפתחות SSH בנפרד; ההרשאה מתבססת על ההתחברות שלך ל-Tailscale.

### 2. התקנה במחשב שלך (Windows)

הורד והתקן את [Tailscale ל-Windows](https://tailscale.com/download/windows), והתחבר **עם אותו חשבון** שהשתמשת בו ב-Pi.

### 3. חיבור מכל מקום

ברגע ששני המכשירים מחוברים לאותו tailnet:

```bash
# מציאת כתובת ה-Tailscale של ה-Pi (הרץ פעם אחת על ה-Pi)
tailscale ip -4

# מהמחשב שלך, מכל רשת (גם ביתית, גם סלולרית) — לפי IP:
ssh pi@100.x.x.x

# או לפי hostname, אם הפעלת MagicDNS בפאנל הניהול של Tailscale:
ssh pi@music-agent-01
```

את `journalctl -u music_agent -f` ופקודות הבדיקה מהחלק הקודם אפשר להריץ בדיוק כרגיל אחרי שמחוברים כך.

### 4. שים לב — מכשיר ללא השגחה

מכיוון שה-Pi לא נגיש פיזית באופן שוטף:

- בפאנל הניהול של Tailscale ([login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines)) — כדאי לכבות **Key Expiry** עבור מכשיר ה-Pi (לחיצה על שלוש הנקודות ליד המכשיר → Disable key expiry), אחרת אחרי כמה חודשים הוא ידרוש אימות מחדש ותאבד גישה עד שתגיע פיזית.
- Tailscale רץ כשירות `systemd` (`tailscaled`) שעולה אוטומטית ב-boot — אין צורך בהגדרה נוספת, כולל אחרי הפעלה מחדש של ה-Pi.
- לבדיקת סטטוס בכל שלב: `tailscale status`.

---

## מכשירים נוספים

לכל Pi:

1. Hostname ייחודי ב-Imager (`music-agent-02`, …).
2. פריסה: `./scripts/deploy-from-pc.sh pi@music-agent-02.local`
3. שיוך נפרד בפאנל.

---

## פתרון בעיות

| בעיה | פתרון |
|------|--------|
| SSH לא מתחבר | ודא SSH מופעל ב-Imager; נסה IP מ-הראוטר |
| `pending` לא נגמר | שייך מכשיר בפאנל; בדוק `api_url` נגיש מה-Pi: `curl -I https://sev.neeman-music.online` |
| השירות נופל | `journalctl -u music_agent -n 50` |
| אין סידורי Pi בלוג | `cat /proc/cpuinfo \| grep Serial` — אם `00000000`, עדכן EEPROM/firmware |
| VLC / נגינה | `sudo apt install vlc libvlc-dev` ואז `bash setup.sh` שוב |
| Tailscale לא מתחבר / IP לא עונה | `sudo tailscale status` על ה-Pi; ודא שהמכשיר לא "Expired" בפאנל הניהול (כבה Key Expiry) |

---

## ארכיטקטורה (תמצית)

```
[Pi] main.py → register (pending) → [API]
                    ↓ (אחרי שיוך)
              device_token + WebSocket → [WS] → MusicPlayer (VLC)
[systemd] music_agent.service → Restart=always, After=network-online
```
