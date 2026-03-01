"""
app.py — QuickCast Client
==========================
Main PySide6 GUI window.
"""

import sys
import asyncio
import json
import threading
import os

import websockets
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QMessageBox, QGroupBox,
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont

from screen_sender import ScreenSender
from screen_receiver import ScreenReceiverWindow

# AI voice controller
try:
    from ai_mode import QuickCastAI
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False

# Auto updater
try:
    from updater import AutoUpdater
    UPDATER_AVAILABLE = True
except ImportError:
    UPDATER_AVAILABLE = False

# ── Default server address ─────────────────────────────────────────────────────
DEFAULT_SERVER_IP   = "https://quickcast-kfg0.onrender.com"
DEFAULT_SERVER_PORT = 8000

# ── Config file ────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "quickcast_config.json")

def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"username": "", "server_ip": DEFAULT_SERVER_IP}

def save_config(username: str, server_ip: str):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"username": username, "server_ip": server_ip}, f)
    except Exception:
        pass


# ── Qt signal bridge ───────────────────────────────────────────────────────────
class AppSignals(QObject):
    user_list_updated         = Signal(list)
    status_changed            = Signal(str)
    share_started_as_receiver = Signal(str)
    share_stopped             = Signal()
    error_received            = Signal(str)
    # AI signals
    ai_status_changed         = Signal(str)
    ai_trigger_send           = Signal(str)
    ai_trigger_stop           = Signal()
    ai_raise_window           = Signal()
    # Auto updater signal
    update_available          = Signal(str, str)  # version, download_url


# ── Main window ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuickCast — LAN Screen Sharing")
        self.setMinimumSize(420, 580)

        # State
        self._username      = ""
        self._ws            = None
        self._ws_loop       = None
        self._sender        = None
        self._receiver_window = None
        self._connected     = False
        self._is_internet   = False
        self._online_users  = []
        self.ai             = None

        # Signals
        self.signals = AppSignals()
        self.signals.user_list_updated.connect(self._update_user_list)
        self.signals.status_changed.connect(self._update_status)
        self.signals.share_started_as_receiver.connect(self._on_receiving_started)
        self.signals.share_stopped.connect(self._on_share_stopped)
        self.signals.error_received.connect(lambda msg: QMessageBox.warning(self, "Server Error", msg))

        # AI signals
        self.signals.ai_status_changed.connect(self._update_ai_status)
        self.signals.ai_trigger_send.connect(self.ai_send_screen)
        self.signals.ai_trigger_stop.connect(self.ai_stop_sharing)
        self.signals.ai_raise_window.connect(self._ai_raise_window)
        self.ai_status_signal       = self.signals.ai_status_changed
        self.ai_trigger_send_signal = self.signals.ai_trigger_send
        self.ai_trigger_stop_signal = self.signals.ai_trigger_stop
        self.ai_raise_signal        = self.signals.ai_raise_window

        # Updater signal
        self.signals.update_available.connect(self._on_update_available)
        self.update_available_signal = self.signals.update_available

        self._build_ui()
        self._load_saved_config()
        self._start_ai()
        self._start_updater()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("📡  QuickCast")
        title.setFont(QFont("Arial", 22, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        # Connection group
        conn_box = QGroupBox("Connection")
        conn_layout = QVBoxLayout(conn_box)

        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel("Server IP:"))
        self.ip_input = QLineEdit(DEFAULT_SERVER_IP)
        self.ip_input.setPlaceholderText("e.g. 192.168.1.100")
        ip_row.addWidget(self.ip_input)
        conn_layout.addLayout(ip_row)

        user_row = QHBoxLayout()
        user_row.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("e.g. Alice")
        user_row.addWidget(self.username_input)
        conn_layout.addLayout(user_row)

        self.connect_btn = QPushButton("🔌  Connect to Workspace")
        self.connect_btn.setFixedHeight(40)
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        conn_layout.addWidget(self.connect_btn)
        root.addWidget(conn_box)

        # Online users
        users_box = QGroupBox("Online Users  (click to select target)")
        users_layout = QVBoxLayout(users_box)
        self.user_list = QListWidget()
        self.user_list.setFixedHeight(130)
        users_layout.addWidget(self.user_list)
        root.addWidget(users_box)

        # Actions
        actions_box = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_box)

        self.send_btn = QPushButton("🖥️  Send Screen  →")
        self.send_btn.setFixedHeight(44)
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._on_send_screen)
        actions_layout.addWidget(self.send_btn)

        self.stop_send_btn = QPushButton("⏹  Stop Sending")
        self.stop_send_btn.setFixedHeight(44)
        self.stop_send_btn.setEnabled(False)
        self.stop_send_btn.clicked.connect(self._on_stop_sending)
        actions_layout.addWidget(self.stop_send_btn)
        root.addWidget(actions_box)

        # Status
        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #888; font-size: 13px;")
        root.addWidget(self.status_label)

        # Config hint
        self.config_hint = QLabel("")
        self.config_hint.setAlignment(Qt.AlignCenter)
        self.config_hint.setStyleSheet("color: #27ae60; font-size: 11px;")
        root.addWidget(self.config_hint)

        # AI status
        self.ai_status_label = QLabel("🎙️  AI: Loading…")
        self.ai_status_label.setAlignment(Qt.AlignCenter)
        self.ai_status_label.setStyleSheet("color: #8e44ad; font-size: 11px;")
        root.addWidget(self.ai_status_label)

        self._style_buttons()

    def _style_buttons(self):
        base = ("QPushButton {{ border-radius: 6px; font-size: 14px; color: white; background: {bg}; }}"
                "QPushButton:hover {{ background: {hover}; }}"
                "QPushButton:disabled {{ background: #555; color: #999; }}")
        self.connect_btn.setStyleSheet(base.format(bg="#2980b9", hover="#3498db"))
        self.send_btn.setStyleSheet(base.format(bg="#27ae60", hover="#2ecc71"))
        self.stop_send_btn.setStyleSheet(base.format(bg="#c0392b", hover="#e74c3c"))

    # ── Config ─────────────────────────────────────────────────────────────────
    def _load_saved_config(self):
        config = load_config()
        if config.get("username"):
            self.username_input.setText(config["username"])
            self.config_hint.setText(f"✅  Last session: {config['username']}  —  click Connect to rejoin")
        if config.get("server_ip"):
            self.ip_input.setText(config["server_ip"])

    # ── Connection ─────────────────────────────────────────────────────────────
    def _on_connect_clicked(self):
        username  = self.username_input.text().strip()
        server_ip = self.ip_input.text().strip()

        if not username:
            QMessageBox.warning(self, "Missing Username", "Please enter a username.")
            return
        if not server_ip:
            QMessageBox.warning(self, "Missing IP", "Please enter the server IP.")
            return

        self._username = username
        save_config(username, server_ip)
        self.config_hint.setText(f"✅  Saved as '{username}' — auto-fills next time")
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connecting…")
        self._update_status("Connecting…")

        threading.Thread(target=self._run_ws_loop, args=(server_ip, username), daemon=True).start()

    def _run_ws_loop(self, host: str, username: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        try:
            loop.run_until_complete(self._ws_client(host, username))
        except Exception as e:
            self.signals.status_changed.emit(f"Connection failed: {e}")
        finally:
            loop.close()

    async def _ws_client(self, host: str, username: str):
        host = host.strip().rstrip("/")
        host = host.replace("https://","").replace("http://","").replace("wss://","").replace("ws://","")

        if "ngrok" in host or ("." in host and not host.replace(".","").replace(":","").isdigit()):
            uri           = f"wss://{host}/ws"
            extra_headers = {"ngrok-skip-browser-warning": "true"}
            self._is_internet = True
        else:
            uri           = f"ws://{host}:{DEFAULT_SERVER_PORT}/ws"
            extra_headers = {}
            self._is_internet = False

        try:
            async with websockets.connect(
                uri,
                additional_headers = extra_headers,
                ping_interval      = 20,
                ping_timeout       = 30,
                close_timeout      = 10,
                max_size           = 10_000_000,
            ) as ws:
                self._ws = ws
                await ws.send(json.dumps({"type": "join", "username": username}))

                async for message in ws:
                    if isinstance(message, bytes):
                        if self._receiver_window:
                            self._receiver_window.push_frame(message)
                        continue

                    data     = json.loads(message)
                    msg_type = data.get("type")

                    if msg_type == "joined":
                        self._connected = True
                        self.signals.status_changed.emit(f"✅  Connected as {username}")
                    elif msg_type == "user_list":
                        self._online_users = data.get("users", [])
                        self.signals.user_list_updated.emit(data["users"])
                    elif msg_type == "share_started":
                        role = data.get("role")
                        if role == "sender":
                            self.signals.status_changed.emit(f"📤  Sending to {data['target']}")
                        elif role == "receiver":
                            self.signals.share_started_as_receiver.emit(data["sender"])
                    elif msg_type == "share_stopped":
                        self.signals.share_stopped.emit()
                    elif msg_type == "error":
                        self.signals.error_received.emit(data.get("message", "Unknown error"))

        except websockets.exceptions.ConnectionClosedError:
            self.signals.status_changed.emit("❌  Disconnected from server")
        except Exception as e:
            self.signals.status_changed.emit(f"❌  Error: {e}")
        finally:
            self._ws        = None
            self._connected = False

    # ── Screen sharing ─────────────────────────────────────────────────────────
    def _on_send_screen(self):
        selected = self.user_list.selectedItems()
        if not selected:
            QMessageBox.information(self, "Select a User", "Click a user in the Online Users list first.")
            return
        target = selected[0].text().replace("  (you)", "").strip()
        if target == self._username:
            QMessageBox.warning(self, "Invalid Target", "You cannot share your screen with yourself.")
            return
        self._send_json({"type": "start_share", "target": target})
        self._sender      = ScreenSender(self._ws.send, is_internet=self._is_internet)
        self._sender.loop = self._ws_loop
        self._sender.start()
        self.send_btn.setEnabled(False)
        self.stop_send_btn.setEnabled(True)
        self._update_status(f"📤  Sending screen to {target}…")

    def _on_stop_sending(self):
        self._send_json({"type": "stop_share"})
        self._stop_sender_thread()
        self.stop_send_btn.setEnabled(False)
        self.send_btn.setEnabled(True)
        self._update_status(f"✅  Connected as {self._username}")

    def _stop_sender_thread(self):
        if self._sender:
            self._sender.stop()
            self._sender = None

    # ── Receiver ───────────────────────────────────────────────────────────────
    def _on_receiving_started(self, sender_name: str):
        self._update_status(f"📥  Receiving from {sender_name}…")
        self._receiver_window = ScreenReceiverWindow(sender_name)
        self._receiver_window.stop_requested.connect(self._on_stop_receiving)
        self._receiver_window.show()

    def _on_stop_receiving(self):
        self._send_json({"type": "stop_share"})

    def _on_share_stopped(self):
        self._stop_sender_thread()
        if self._receiver_window:
            self._receiver_window.end_session()
            self._receiver_window = None
        self.send_btn.setEnabled(True)
        self.stop_send_btn.setEnabled(False)
        self._update_status(f"✅  Connected as {self._username}")

    # ── UI helpers ─────────────────────────────────────────────────────────────
    def _update_user_list(self, users: list):
        self._online_users = users
        self.user_list.clear()
        for user in users:
            item = QListWidgetItem(user)
            if user == self._username:
                item.setForeground(Qt.gray)
                item.setText(f"{user}  (you)")
            self.user_list.addItem(item)
        others = [u for u in users if u != self._username]
        self.send_btn.setEnabled(self._connected and len(others) > 0)
        self.connect_btn.setEnabled(not self._connected)
        if self._connected:
            self.connect_btn.setText("🔌  Connect to Workspace")

    def _update_status(self, text: str):
        self.status_label.setText(f"Status: {text}")

    def _send_json(self, payload: dict):
        if self._ws and self._ws_loop:
            asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(payload)), self._ws_loop)

    # ── AI ─────────────────────────────────────────────────────────────────────
    def _start_ai(self):
        if not AI_AVAILABLE:
            self._update_ai_status("⚠️  AI: Install vosk + sounddevice + pyttsx3 to enable voice")
            return
        try:
            self.ai = QuickCastAI(
                window           = self,
                send_screen      = self.ai_send_screen,
                stop_sharing     = self.ai_stop_sharing,
                get_online_users = self.get_online_users,
            )
            self.ai.start()
        except Exception as e:
            self._update_ai_status(f"⚠️  AI failed to start: {e}")

    def get_online_users(self) -> list:
        return [u for u in self._online_users if u != self._username]

    def ai_send_screen(self, target: str):
        if not self._connected or target not in self._online_users:
            return
        self._send_json({"type": "start_share", "target": target})
        self._sender      = ScreenSender(self._ws.send, is_internet=self._is_internet)
        self._sender.loop = self._ws_loop
        self._sender.start()
        self.send_btn.setEnabled(False)
        self.stop_send_btn.setEnabled(True)
        self._update_status(f"📤  AI sharing to {target}…")

    def ai_stop_sharing(self):
        self._send_json({"type": "stop_share"})
        self._stop_sender_thread()
        self.stop_send_btn.setEnabled(False)
        self.send_btn.setEnabled(True)
        self._update_status(f"✅  Connected as {self._username}")

    def _update_ai_status(self, text: str):
        self.ai_status_label.setText(text)

    def _ai_raise_window(self):
        self.raise_()
        self.activateWindow()
        self.showNormal()

    # ── Auto updater ───────────────────────────────────────────────────────────
    def _start_updater(self):
        if not UPDATER_AVAILABLE:
            return
        try:
            self._updater = AutoUpdater(window=self)
            self._updater.start()
        except Exception:
            pass

    def _on_update_available(self, version: str, download_url: str):
        """Show popup when new version found on GitHub."""
        reply = QMessageBox.question(
            self,
            "Update Available! 🎉",
            f"QuickCast v{version} is available!\n\n"
            f"Click Yes to download and install automatically.\n"
            f"The app will restart after updating.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.setEnabled(False)
            self._update_status("Downloading update…")
            threading.Thread(target=self._do_update, args=(download_url,), daemon=True).start()

    def _do_update(self, download_url: str):
        from updater import download_and_install
        success = download_and_install(download_url)
        if success:
            sys.exit(0)
        else:
            self.signals.status_changed.emit("❌  Update failed — try again later")
            self.setEnabled(True)

    # ── Close ──────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self.ai:
            self.ai.stop()
        self._stop_sender_thread()
        if self._ws and self._ws_loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._ws_loop)
        super().closeEvent(event)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())