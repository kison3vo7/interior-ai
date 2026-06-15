from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt, bcrypt, sqlite3, os

router = APIRouter()
security = HTTPBearer()
SECRET = os.getenv("JWT_SECRET", "change-me-in-prod")

# --- 简单 SQLite（生产换 PostgreSQL） ---
def db():
    c = sqlite3.connect("app.db")
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE, password TEXT,
        credits INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'free',
        created_at TEXT)""")
    c.commit()
    return c

class AuthReq(BaseModel):
    phone: str
    password: str

def make_token(user_id: int) -> str:
    return jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(days=30)}, SECRET)

def current_user(cred: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])
        return payload["sub"]
    except Exception:
        raise HTTPException(401, "Token 无效或已过期")

@router.post("/register")
def register(req: AuthReq):
    hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    try:
        conn = db()
        conn.execute("INSERT INTO users(phone,password,created_at) VALUES(?,?,?)",
                     (req.phone, hashed, datetime.utcnow().isoformat()))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE phone=?", (req.phone,)).fetchone()[0]
        return {"token": make_token(uid), "credits": 10}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "手机号已注册")

@router.post("/login")
def login(req: AuthReq):
    conn = db()
    row = conn.execute("SELECT id,password,credits,plan FROM users WHERE phone=?",
                       (req.phone,)).fetchone()
    if not row or not bcrypt.checkpw(req.password.encode(), row[1].encode()):
        raise HTTPException(401, "手机号或密码错误")
    return {"token": make_token(row[0]), "credits": row[2], "plan": row[3]}

@router.get("/me")
def me(uid=Depends(current_user)):
    row = db().execute("SELECT phone,credits,plan FROM users WHERE id=?", (uid,)).fetchone()
    return {"phone": row[0], "credits": row[1], "plan": row[2]}
