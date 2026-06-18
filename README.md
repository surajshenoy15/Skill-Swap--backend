# Skill Swap — Backend

FastAPI + SQLite (local file `skillswap.db`, auto-created).

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

API runs at http://localhost:8000
WebSocket at ws://localhost:8000/ws/{token}

## Endpoints

- POST /api/register  {name,email,password,skill_offered,skill_wanted,bio}
- POST /api/login      {email,password}
- GET  /api/me         (Bearer token)
- GET  /api/users      (Bearer token) — all other users
- GET  /api/messages/{other_id}  (Bearer token)
- POST /api/messages   {receiver_id, content}  (Bearer token)
- WS   /ws/{token}     — receives "user_joined" and "message" events live
