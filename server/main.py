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
# { sender_username -> receiver_username }
# Example: { "LYNX": "BOB", "CHARLIE": "DAVE" }
active_sessions: dict[str, str] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def broadcast_user_list():
    """Send the current online user list + active sessions to every connected client."""
    message = json.dumps({
        "type": "user_list",
        "users": list(connections.keys()),
        "sessions": active_sessions,   # e.g. {"LYNX": "BOB"} so UI can show who's busy
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
    for sender, receiver in active_sessions.items():
        if receiver == username:
            return sender
    return None


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    username: str | None = None

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

        # ── Step 2: Main message loop ─────────────────────────────────────────
        while True:
            message = await ws.receive()

            # ── Binary frame (JPEG screen data) ──────────────────────────────
            if message["type"] == "websocket.receive" and message.get("bytes"):
                frame_bytes = message["bytes"]

                # Forward frame only if this user is an active sender
                receiver_name = active_sessions.get(username)
                if receiver_name:
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
                    target = data.get("target")

                    if target not in connections:
                        await notify(username, {"type": "error", "message": f"User '{target}' is not online."})
                        continue

                    # Check if THIS user is already sending
                    if username in active_sessions:
                        await notify(username, {"type": "error", "message": "You are already sharing your screen."})
                        continue

                    # Check if TARGET is already receiving from someone else
                    existing_sender = find_session_as_receiver(target)
                    if existing_sender:
                        await notify(username, {"type": "error", "message": f"'{target}' is already receiving from '{existing_sender}'."})
                        continue

                    # Register the new session
                    active_sessions[username] = target
                    log.info(f"🖥️  Session started: {username} → {target}  |  all sessions: {active_sessions}")

                    await notify(username, {"type": "share_started", "role": "sender",   "target": target})
                    await notify(target,   {"type": "share_started", "role": "receiver", "sender": username})
                    await broadcast_user_list()  # Update everyone so they see who's busy

                # ── Stop a share session ──────────────────────────────────────
                elif msg_type == "stop_share":
                    await _end_session(username)

    except WebSocketDisconnect:
        log.info(f"🔌  {username} disconnected")
    except Exception as e:
        log.error(f"Unexpected error for {username}: {e}")
    finally:
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
        receiver = active_sessions.pop(username)
        log.info(f"⏹️  Session ended: {username} → {receiver}")
        await notify(username, {"type": "share_stopped"})
        await notify(receiver, {"type": "share_stopped"})
        if broadcast:
            await broadcast_user_list()
        return

    # Case 2: This user is the RECEIVER
    sender = find_session_as_receiver(username)
    if sender:
        active_sessions.pop(sender)
        log.info(f"⏹️  Session ended (receiver left): {sender} → {username}")
        await notify(sender,   {"type": "share_stopped"})
        await notify(username, {"type": "share_stopped"})
        if broadcast:
            await broadcast_user_list()


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {
        "status": "running",
        "online_users": list(connections.keys()),
        "active_sessions": active_sessions,   # e.g. {"LYNX": "BOB", "CHARLIE": "DAVE"}
    }