"""
screen_sender.py
================
Captures the screen using mss, compresses it with OpenCV (JPEG),
and sends the frame bytes over a WebSocket connection.

Quality settings:
- LAN mode:      1920x1080, 85% JPEG, 30fps  (crisp, high quality)
- Internet mode: 1280x720,  75% JPEG, 20fps  (sharp, Render handles it well)
"""

import threading
import time
import mss
import cv2
import numpy as np


class ScreenSender(threading.Thread):

    # ── LAN settings ──────────────────────────────────────────────────────────
    LAN_WIDTH   = 1920
    LAN_HEIGHT  = 1080
    LAN_QUALITY = 85
    LAN_FPS     = 30

    # ── Internet/Render settings ───────────────────────────────────────────────
    NET_WIDTH   = 1280
    NET_HEIGHT  = 720
    NET_QUALITY = 75
    NET_FPS     = 20

    def __init__(self, ws_send_callback, is_internet=False):
        super().__init__(daemon=True)
        self.ws_send_callback = ws_send_callback
        self._stop_event      = threading.Event()
        self.loop             = None

        if is_internet:
            self.SEND_WIDTH   = self.NET_WIDTH
            self.SEND_HEIGHT  = self.NET_HEIGHT
            self.JPEG_QUALITY = self.NET_QUALITY
            self.TARGET_FPS   = self.NET_FPS
        else:
            self.SEND_WIDTH   = self.LAN_WIDTH
            self.SEND_HEIGHT  = self.LAN_HEIGHT
            self.JPEG_QUALITY = self.LAN_QUALITY
            self.TARGET_FPS   = self.LAN_FPS

        self._last_send_time = 0

    def stop(self):
        self._stop_event.set()

    def run(self):
        frame_interval = 1.0 / self.TARGET_FPS

        with mss.mss() as sct:
            monitor = sct.monitors[1]

            while not self._stop_event.is_set():
                start = time.perf_counter()

                # ── Capture ───────────────────────────────────────────────────
                raw   = sct.grab(monitor)
                frame = np.array(raw)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                # ── Resize ────────────────────────────────────────────────────
                frame = cv2.resize(
                    frame,
                    (self.SEND_WIDTH, self.SEND_HEIGHT),
                    interpolation=cv2.INTER_AREA,  # INTER_AREA = best for downscaling
                )

                # ── JPEG encode ───────────────────────────────────────────────
                encode_params = [
                    cv2.IMWRITE_JPEG_QUALITY,    self.JPEG_QUALITY,
                    cv2.IMWRITE_JPEG_OPTIMIZE,   1,   # Optimize huffman table
                    cv2.IMWRITE_JPEG_PROGRESSIVE, 1,  # Progressive JPEG
                ]
                success, buffer = cv2.imencode(".jpg", frame, encode_params)
                if not success:
                    continue

                jpeg_bytes = buffer.tobytes()

                # ── Send via WebSocket ────────────────────────────────────────
                if self.loop and not self.loop.is_closed():
                    import asyncio
                    now = time.perf_counter()
                    if now - self._last_send_time >= frame_interval * 0.9:
                        asyncio.run_coroutine_threadsafe(
                            self.ws_send_callback(jpeg_bytes),
                            self.loop,
                        )
                        self._last_send_time = now

                # ── Rate limiting ─────────────────────────────────────────────
                elapsed    = time.perf_counter() - start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)