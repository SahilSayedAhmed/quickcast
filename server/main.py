"""
QuickCast - Screen Sharing Server (Multi-Session Edition)
==========================================================
FastAPI + WebSocket server for LAN screen sharing.
Supports multiple simultaneous sessions (e.g. LYNX→BOB and CHARLIE→DAVE at the same time).

How it works:
- Each client connects via WebSocket and sends a JSON "join" message with their username.
- The server keeps a registry of { username -> websocket } in memory.
- Sessions are stored in a dictionary: { sender_username -> receiver_username }
- Multiple sessions can run independently at the same time.
- When a sender disconnects or stops, only their session is affected — others continue.
"""

import asyncio
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("quickcast")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="QuickCast Server — Multi-Session")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── In-memory state ────────────────────────────────────────────────────────────
# { username -> WebSocket }
connections: dict[str, WebSocket] = {}

# Multiple sessions supported:
# { sender_username -> [receiver1, receiver2, ...] }
# Example: { "LYNX": ["BOB", "DAVE"], "CHARLIE": ["ALICE"] }
active_sessions: dict[str, list] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def broadcast_user_list():
    """Send the current online user list + active sessions to every connected client."""
    message = json.dumps({
        "type": "user_list",
        "users": list(connections.keys()),
        "sessions": active_sessions,
    })
    for ws in connections.values():
        try:
            await ws.send_text(message)
        except Exception:
            pass


async def notify(username: str, payload: dict):
    """Send a JSON message to a specific user (if online)."""
    ws = connections.get(username)
    if ws:
        await ws.send_text(json.dumps(payload))


def find_session_as_receiver(username: str) -> str | None:
    """Return the sender's username if this user is currently a receiver, else None."""
    for sender, receivers in active_sessions.items():
        if username in receivers:
            return sender
    return None


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    username: str | None = None
    # Send periodic pings to keep connection alive during idle periods
    async def keepalive():
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send_text('{"type":"ping"}')
            except Exception:
                break

    try:
        # ── Step 1: Wait for the "join" message ───────────────────────────────
        raw  = await ws.receive_text()
        data = json.loads(raw)

        if data.get("type") != "join" or not data.get("username", "").strip():
            await ws.send_text(json.dumps({"type": "error", "message": "First message must be a join with a username."}))
            await ws.close()
            return

        username = data["username"].strip()

        if username in connections:
            await ws.send_text(json.dumps({"type": "error", "message": f"Username '{username}' is already taken."}))
            await ws.close()
            return

        # Register the user
        connections[username] = ws
        log.info(f"✅  {username} joined  |  online: {list(connections.keys())}")

        await ws.send_text(json.dumps({"type": "joined", "username": username}))
        await broadcast_user_list()

        # Start keepalive task for this connection
        ping_task = asyncio.create_task(keepalive())

        # ── Step 2: Main message loop ─────────────────────────────────────────
        while True:
            message = await ws.receive()

            # ── Binary frame (JPEG screen data) ──────────────────────────────
            if message["type"] == "websocket.receive" and message.get("bytes"):
                frame_bytes = message["bytes"]

                # Forward frame only if this user is an active sender
                receivers = active_sessions.get(username, [])
                for receiver_name in receivers:
                    receiver_ws = connections.get(receiver_name)
                    if receiver_ws:
                        try:
                            await receiver_ws.send_bytes(frame_bytes)
                        except Exception as e:
                            log.warning(f"Failed to forward frame {username}→{receiver_name}: {e}")

            # ── Text control messages ─────────────────────────────────────────
            elif message["type"] == "websocket.receive" and message.get("text"):
                data     = json.loads(message["text"])
                msg_type = data.get("type")

                # ── Start a new share session ─────────────────────────────────
                if msg_type == "start_share":
                    # Support both single target (legacy) and multiple targets
                    target  = data.get("target")
                    targets = data.get("targets", [])
                    if target and target not in targets:
                        targets.append(target)

                    if not targets:
                        await notify(username, {"type": "error", "message": "No target specified."})
                        continue

                    # Check if THIS user is already sending
                    if username in active_sessions:
                        await notify(username, {"type": "error", "message": "You are already sharing your screen."})
                        continue

                    # Validate all targets
                    valid_targets = []
                    for t in targets:
                        if t not in connections:
                            await notify(username, {"type": "error", "message": f"User '{t}' is not online."})
                            continue
                        existing_sender = find_session_as_receiver(t)
                        if existing_sender:
                            await notify(username, {"type": "error", "message": f"'{t}' is already receiving from '{existing_sender}'."})
                            continue
                        valid_targets.append(t)

                    if not valid_targets:
                        continue

                    # Register the new session with all valid targets
                    active_sessions[username] = valid_targets
                    log.info(f"🖥️  Session started: {username} → {valid_targets}")

                    await notify(username, {"type": "share_started", "role": "sender", "targets": valid_targets})
                    for t in valid_targets:
                        await notify(t, {"type": "share_started", "role": "receiver", "sender": username})
                    await broadcast_user_list()

                # ── Stop a share session ──────────────────────────────────────
                elif msg_type == "stop_share":
                    await _end_session(username)

    except WebSocketDisconnect:
        log.info(f"🔌  {username} disconnected")
    except Exception as e:
        log.error(f"Unexpected error for {username}: {e}")
    finally:
        try:
            ping_task.cancel()
        except Exception:
            pass
        if username and username in connections:
            del connections[username]

            # Clean up any session this user was part of
            await _end_session(username, broadcast=False)

            await broadcast_user_list()
            log.info(f"🧹  Cleaned up {username}  |  online: {list(connections.keys())}")


async def _end_session(username: str, broadcast: bool = True):
    """
    End the session where username is either sender or receiver.
    Notifies both parties and removes the session from active_sessions.
    """
    # Case 1: This user is the SENDER
    if username in active_sessions:
        receivers = active_sessions.pop(username)
        log.info(f"⏹️  Session ended: {username} → {receivers}")
        await notify(username, {"type": "share_stopped"})
        for r in receivers:
            await notify(r, {"type": "share_stopped"})
        if broadcast:
            await broadcast_user_list()
        return

    # Case 2: This user is the RECEIVER
    sender = find_session_as_receiver(username)
    if sender:
        receivers = active_sessions.get(sender, [])
        receivers = [r for r in receivers if r != username]
        if receivers:
            active_sessions[sender] = receivers  # remove just this receiver
        else:
            active_sessions.pop(sender)          # no receivers left, end session
        log.info(f"⏹️  Receiver left: {sender} → {username}")
        await notify(sender,   {"type": "share_stopped"})
        await notify(username, {"type": "share_stopped"})
        if broadcast:
            await broadcast_user_list()


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/")
@app.head("/")
async def health():
    return {
        "status": "running",
        "online_users": list(connections.keys()),
        "active_sessions": active_sessions,
    }