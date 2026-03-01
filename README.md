# 📡 QuickCast — LAN Screen Sharing Tool

A fast, simple, internal screen-sharing tool for up to 4 users on the same Wi-Fi network.
Built with **FastAPI + WebSockets** (server) and **PySide6** (client).

---

## 📁 Project Structure

```
quickcast/
├── server/
│   └── main.py              ← FastAPI WebSocket server
└── client/
    ├── app.py               ← Main PySide6 GUI application
    ├── screen_sender.py     ← Screen capture + compression + sending
    ├── screen_receiver.py   ← Fullscreen frame viewer window
    ├── voice_module.py      ← Placeholder (future feature)
    ├── gesture_module.py    ← Placeholder (future feature)
    └── requirements.txt     ← Python dependencies
```

---

## ⚙️ Setup (do this once on every machine)

### 1. Install Python 3.11+
Download from https://python.org — make sure to check "Add to PATH" on Windows.

### 2. Create a virtual environment (recommended)
```bash
# Navigate to the quickcast folder
cd quickcast

# Create venv
python -m venv venv

# Activate it:
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r client/requirements.txt
```

---

## 🚀 Running the Server

The server runs on **one machine only** (pick any machine on your LAN — ideally one that stays on).

```bash
# From inside the quickcast/ folder:
cd server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

- `--host 0.0.0.0` makes the server reachable from other machines on the LAN
- `--reload` auto-restarts on code changes (remove in production)

**Find your server's LAN IP:**
- Windows: run `ipconfig` in Command Prompt → look for `IPv4 Address`
- macOS/Linux: run `ifconfig` or `ip addr` → look for `inet` under your Wi-Fi adapter

You'll see something like `192.168.1.105` — share this with other users.

**Verify the server is running:**
Open a browser and go to: `http://<server-ip>:8000/`
You should see: `{"status": "running", "online_users": [], "active_session": null}`

---

## 💻 Running the Client (on each user's machine)

```bash
# From inside the quickcast/ folder:
cd client
python app.py
```

The GUI window will open. Each user:
1. Types the **Server IP** (the LAN IP from above)
2. Enters their **Username** (e.g. Alice, Bob, Charlie, Dave)
3. Clicks **Connect to Workspace**

---

## 🖥️ How to Share Your Screen

1. Make sure at least 2 users are connected (you'll see them in the Online Users list)
2. **Click a user** in the Online Users list to select them
3. Click **"Send Screen →"**
4. The selected user will immediately see a fullscreen window with your screen
5. To stop: click **"Stop Sending"** (or the receiver clicks "Stop Receiving" in their fullscreen window)

---

## 🔧 Troubleshooting

| Problem | Solution |
|---|---|
| "Connection refused" | Make sure the server is running and the IP is correct |
| "Username already taken" | Choose a different username |
| Black screen on receiver | Check that the sender's monitor is not in power-save mode |
| Lag / choppy frames | You're on a slow Wi-Fi — try 5GHz band or Ethernet |
| Server not reachable from other PCs | Check Windows Firewall → allow port 8000 (or temporarily disable firewall for testing) |
| mss error on macOS | Go to System Settings → Privacy → Screen Recording → enable Terminal (or your Python app) |

---

## 🔥 Windows Firewall (if users can't reach the server)

Open PowerShell as Administrator and run:
```powershell
New-NetFirewallRule -DisplayName "QuickCast" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

---

## 📦 What Each File Does

| File | Role |
|---|---|
| `server/main.py` | WebSocket server — routes users, manages sessions, forwards frames |
| `client/app.py` | PySide6 main window — connection UI, user list, button controls |
| `client/screen_sender.py` | Background thread: captures screen → resizes → JPEG → sends bytes |
| `client/screen_receiver.py` | Fullscreen Qt window: decodes JPEG frames → displays in real-time |
| `client/voice_module.py` | Empty placeholder for future voice feature |
| `client/gesture_module.py` | Empty placeholder for future gesture feature |

---

## 🏗️ Architecture Overview

```
[User A: Sender]                    [Server]                   [User B: Receiver]
      │                                │                               │
      │── WS connect + join ──────────►│                               │
      │                                │◄── WS connect + join ─────────│
      │                                │──── user_list ───────────────►│
      │                                │──── user_list ───────────────►│
      │                                │                               │
      │── start_share(target=B) ──────►│──── share_started(receiver) ─►│
      │◄─ share_started(sender) ───────│      [Fullscreen opens on B]  │
      │                                │                               │
      │── [JPEG bytes] ───────────────►│──── [JPEG bytes] ────────────►│
      │── [JPEG bytes] ───────────────►│──── [JPEG bytes] ────────────►│
      │   (20 fps loop)                │     (forwarded instantly)     │
      │                                │                               │
      │── stop_share ─────────────────►│──── share_stopped ───────────►│
      │◄─ share_stopped ───────────────│      [Fullscreen closes on B] │
```

---

## ⚡ Performance Settings (in `screen_sender.py`)

You can tune these constants:
```python
SEND_WIDTH   = 1280   # Reduce to 960 or 800 for slower networks
SEND_HEIGHT  = 720    # Reduce to 540 for slower networks
JPEG_QUALITY = 65     # 50 = smaller files, 80 = better quality
TARGET_FPS   = 20     # Reduce to 15 for slower machines
```

---

## 🛣️ 3-Day Build Plan

| Day | Goal |
|---|---|
| Day 1 | Set up server, test WebSocket connections, verify user list sync |
| Day 2 | Build client GUI, implement send/receive flow, test on 2 machines |
| Day 3 | Test with 4 users, tune performance, fix edge cases |
