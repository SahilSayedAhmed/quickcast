"""
screen_receiver.py
==================
Decodes incoming JPEG frame bytes and renders them in a fullscreen PySide6 window.

The receiver window:
  - Opens fullscreen automatically when a share session starts
  - Shows each frame as it arrives (updated via Qt signals — thread-safe)
  - Closes automatically when the session ends
  - Has a "Stop" button so the viewer can end the session themselves
"""

import numpy as np
import cv2
from PySide6.QtWidgets import QMainWindow, QLabel, QPushButton, QVBoxLayout, QWidget
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QImage, QPixmap


# ── Signal bridge ──────────────────────────────────────────────────────────────
# Qt requires UI updates to happen on the main thread.
# We use a QObject with signals to safely pass frames from the WebSocket thread.

class ReceiverSignals(QObject):
    """Signals emitted by the receiver to update the Qt UI thread-safely."""
    new_frame = Signal(bytes)   # Raw JPEG bytes
    session_ended = Signal()    # Tell the window to close


# ── Fullscreen viewer window ───────────────────────────────────────────────────

class ScreenReceiverWindow(QMainWindow):
    """
    Fullscreen window that displays incoming screen frames.
    Created and shown when a share session begins (role = receiver).
    """

    # Signal emitted when the user clicks "Stop Receiving"
    stop_requested = Signal()

    def __init__(self, sender_name: str):
        super().__init__()
        self.sender_name = sender_name
        self.setWindowTitle(f"QuickCast — Receiving from {sender_name}")

        # ── Signals ────────────────────────────────────────────────────────────
        self.signals = ReceiverSignals()
        self.signals.new_frame.connect(self._on_new_frame)
        self.signals.session_ended.connect(self.close)

        # ── UI ─────────────────────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Frame display label — stretches to fill the window
        self.frame_label = QLabel("Waiting for frames…")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setStyleSheet("background: #000; color: #aaa; font-size: 18px;")
        self.frame_label.setScaledContents(False)  # We handle scaling manually for quality
        layout.addWidget(self.frame_label, stretch=1)

        # Stop button sits at the bottom
        self.stop_btn = QPushButton(f"⏹  Stop Receiving  (from {sender_name})")
        self.stop_btn.setFixedHeight(44)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-size: 14px; border: none; }"
            "QPushButton:hover { background: #e74c3c; }"
        )
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        layout.addWidget(self.stop_btn)

        # Go fullscreen immediately
        self.showFullScreen()

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_new_frame(self, jpeg_bytes: bytes):
        """Decode a JPEG frame and display it. Called on the main Qt thread."""
        # Decode JPEG → OpenCV array → convert color → QPixmap
        nparr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            return

        # OpenCV uses BGR; Qt needs RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape

        qt_image = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)

        # Scale to fit the label while keeping aspect ratio
        # Use SmoothTransformation for best quality rendering
        label_size = self.frame_label.size()
        scaled = pixmap.scaled(
            label_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.frame_label.setPixmap(scaled)

    def _on_stop_clicked(self):
        """User clicked 'Stop Receiving' — notify the main app."""
        self.stop_requested.emit()

    def push_frame(self, jpeg_bytes: bytes):
        """Called from the WebSocket thread — schedules a UI update safely."""
        self.signals.new_frame.emit(jpeg_bytes)

    def end_session(self):
        """Called when the server says the session is over."""
        self.signals.session_ended.emit()

    def keyPressEvent(self, event):
        """Allow ESC to exit fullscreen (falls back to windowed)."""
        if event.key() == Qt.Key_Escape:
            self.showNormal()
        else:
            super().keyPressEvent(event)