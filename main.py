import sqlite3
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "skillswap.db"

app = FastAPI(title="Skill Swap API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            skill_offered TEXT NOT NULL,
            skill_wanted TEXT NOT NULL,
            bio TEXT DEFAULT '',
            token TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()

# ---------------- Models ----------------

class RegisterIn(BaseModel):
    name: str
    email: str
    password: str
    skill_offered: str
    skill_wanted: str
    bio: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


class MessageIn(BaseModel):
    receiver_id: int
    content: str


# ---------------- Auth helper ----------------

def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "Invalid token")
    return dict(row)


def public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "skill_offered": row["skill_offered"],
        "skill_wanted": row["skill_wanted"],
        "bio": row["bio"],
    }


# ---------------- WebSocket connection manager ----------------

class ConnectionManager:
    def __init__(self):
        # user_id -> list of websockets (allow multiple tabs)
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(user_id, []).append(ws)

    def disconnect(self, user_id: int, ws: WebSocket):
        conns = self.active.get(user_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns and user_id in self.active:
            del self.active[user_id]

    async def send_to_user(self, user_id: int, payload: dict):
        for ws in self.active.get(user_id, []):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                pass

    async def broadcast_all(self, payload: dict):
        for conns in self.active.values():
            for ws in conns:
                try:
                    await ws.send_text(json.dumps(payload))
                except Exception:
                    pass


manager = ConnectionManager()

# ---------------- Routes ----------------

@app.post("/api/register")
async def register(data: RegisterIn):
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (data.email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "Email already registered")

    token = secrets.token_hex(24)
    cur = conn.execute(
        """INSERT INTO users (name, email, password, skill_offered, skill_wanted, bio, token, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (data.name, data.email, data.password, data.skill_offered, data.skill_wanted,
         data.bio, token, datetime.utcnow().isoformat()),
    )
    conn.commit()
    user_id = cur.lastrowid
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    new_user = public_user(dict(row))
    await manager.broadcast_all({"type": "user_joined", "user": new_user})

    return {"token": token, "user": new_user}


@app.post("/api/login")
async def login(data: LoginIn):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? AND password = ?", (data.email, data.password)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(401, "Invalid credentials")

    token = secrets.token_hex(24)
    conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, row["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    conn.close()
    return {"token": token, "user": public_user(dict(row))}


@app.get("/api/users")
async def list_users(current=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM users WHERE id != ? ORDER BY created_at ASC", (current["id"],)).fetchall()
    conn.close()
    return [public_user(dict(r)) for r in rows]


@app.get("/api/me")
async def me(current=Depends(get_current_user)):
    return public_user(current)


@app.get("/api/messages/{other_id}")
async def get_messages(other_id: int, current=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
           ORDER BY created_at ASC""",
        (current["id"], other_id, other_id, current["id"]),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/messages")
async def send_message(data: MessageIn, current=Depends(get_current_user)):
    conn = get_db()
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO messages (sender_id, receiver_id, content, created_at) VALUES (?, ?, ?, ?)",
        (current["id"], data.receiver_id, data.content, now),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()

    payload = {
        "type": "message",
        "message": {
            "id": msg_id,
            "sender_id": current["id"],
            "receiver_id": data.receiver_id,
            "content": data.content,
            "created_at": now,
        },
    }
    await manager.send_to_user(data.receiver_id, payload)
    await manager.send_to_user(current["id"], payload)
    return payload["message"]


@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        await websocket.close(code=4001)
        return

    user_id = row["id"]
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive ping, ignore content
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
