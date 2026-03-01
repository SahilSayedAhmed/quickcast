"""
screen_sender.py
================
Captures the screen using mss, compresses it with OpenCV (JPEG),
and sends the frame bytes over a WebSocket connection.

Automatically adjusts quality and FPS based on connection type:
- LAN mode: High quality (1280x720, 65% JPEG, 20fps)
- Internet/ngrok mode: Lower quality (960x540, 45% JPEG, 10fps) to prevent freezing
"""

import threading
import time
import mss
import cv2
import numpy as np


class ScreenSender(threading.Thread):

    # ── LAN settings (fast local network) ─────────────────────────────────────
    LAN_WIDTH   = 1280
    LAN_HEIGHT  = 720
    LAN_QUALITY = 65
    LAN_FPS     = 20

    # ── Internet/ngrok settings (slower connection) ────────────────────────────
    # Lower res + quality = less data = no freezing/buffering
    NET_WIDTH   = 960
    NET_HEIGHT  = 540
    NET_QUALITY = 45
    NET_FPS     = 10

    def __init__(self, ws_send_callback, is_internet=False):
        """
        Parameters
        ----------
        ws_send_callback : coroutine function
            Async function that sends bytes over WebSocket.
        is_internet : bool
            True if connecting via ngrok/internet → use lower quality settings.
            False if on LAN → use higher quality settings.
        """
        super().__init__(daemon=True)
        self.ws_send_callback = ws_send_callback
        self._stop_event = threading.Event()
        self.loop = None

        # Pick settings based on connection type
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

        # Track last sent time to skip frames if sending is falling behind
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
                    interpolation=cv2.INTER_LINEAR,
                )

                # ── JPEG encode ───────────────────────────────────────────────
                encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]
                success, buffer = cv2.imencode(".jpg", frame, encode_params)
                if not success:
                    continue

                jpeg_bytes = buffer.tobytes()

                # ── Send via WebSocket ────────────────────────────────────────
                # Skip frame if previous send hasn't finished yet (prevents buffering)
                if self.loop and not self.loop.is_closed():
                    import asyncio
                    now = time.perf_counter()
                    # Only send if enough time has passed since last send
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