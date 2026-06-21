import os, uuid, sqlite3, base64, asyncio, time, json, re, hmac, hashlib
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO
from contextlib import contextmanager
import shutil

import httpx, bcrypt, jwt
import psycopg
from PIL import Image, ImageDraw
try:
    from pillow_heif import register_heif_opener
except Exception:  # pragma: no cover - optional runtime dependency
    register_heif_opener = None
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
load_dotenv()

if register_heif_opener:
    register_heif_opener()

APP_DATA_ROOT = Path(os.getenv("APP_DATA_ROOT", "/var/data")).resolve()
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", APP_DATA_ROOT / "uploads")).resolve()
DATA_ROOT = Path(os.getenv("DATA_DIR", APP_DATA_ROOT / "data")).resolve()
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
DATA_ROOT.mkdir(parents=True, exist_ok=True)

ARK_API_KEY  = os.getenv("ARK_API_KEY", "")
JWT_SECRET   = os.getenv("JWT_SECRET", "dev-secret")
ARK_IMAGE_MODEL = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128")
ARK_IMAGE_FALLBACK_MODEL = os.getenv("ARK_IMAGE_FALLBACK_MODEL", "doubao-seedream-4-5-251128")
CREEM_API_KEY = os.getenv("CREEM_API_KEY", "").strip()
CREEM_WEBHOOK_SECRET = os.getenv("CREEM_WEBHOOK_SECRET", "").strip()
CREEM_API_BASE = os.getenv("CREEM_API_BASE", "https://api.creem.io/v1").strip().rstrip("/")
CREEM_PRODUCT_C10 = os.getenv("CREEM_PRODUCT_C10", "").strip()
CREEM_PRODUCT_C50 = os.getenv("CREEM_PRODUCT_C50", "").strip()
CREEM_PRODUCT_C200 = os.getenv("CREEM_PRODUCT_C200", "").strip()
CREEM_PRODUCT_C500 = os.getenv("CREEM_PRODUCT_C500", "").strip()
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "")
NEXT_PUBLIC_BASE_URL = os.getenv("NEXT_PUBLIC_BASE_URL", "")
DOMAIN = os.getenv("DOMAIN", "")
UPLOAD_DIR   = UPLOAD_ROOT
DATA_DIR     = DATA_ROOT
DB_PATH      = DATA_DIR / "app.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ROOT_DIR = Path(__file__).resolve().parent.parent
BUNDLED_UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads"
INDEX_HTML = ROOT_DIR / "index.html"
TEST_ACCOUNT_EMAIL = "test@lingganspace.work"
TEST_ACCOUNT_MIN_CREDITS = 500
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).strip().lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"}
app = FastAPI(
    title="灵感空间AI",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _seed_bundled_uploads() -> None:
    if not BUNDLED_UPLOAD_ROOT.exists():
        return
    for source in BUNDLED_UPLOAD_ROOT.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(BUNDLED_UPLOAD_ROOT)
        target = UPLOAD_ROOT / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

_seed_bundled_uploads()

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_ROOT)), name="uploads")

security = HTTPBearer(auto_error=False)

ORDER_EXTRA_COLUMNS = {
    "provider": "TEXT DEFAULT 'creem'",
    "provider_checkout_id": "TEXT",
    "provider_order_id": "TEXT",
    "provider_product_id": "TEXT",
    "provider_checkout_url": "TEXT",
    "provider_request_id": "TEXT",
    "provider_customer_email": "TEXT",
    "paid_at": "TEXT",
    "admin_note": "TEXT",
}

# ─── DB ───────────────────────────────────────────────
USING_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

def _sqlite_param_sql(sql: str) -> str:
    return sql.replace("%s", "?") if not USING_POSTGRES else sql

def _pg_conn_kwargs() -> dict:
    return {"autocommit": False}

def _ensure_orders_schema(db) -> None:
    if USING_POSTGRES:
        cur = db.cursor()
        for name, spec in ORDER_EXTRA_COLUMNS.items():
            cur.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {name} {spec}")
        db.commit()
        return

    rows = db.execute("PRAGMA table_info(orders)").fetchall()
    existing = {row[1] for row in rows}
    for name, spec in ORDER_EXTRA_COLUMNS.items():
        if name not in existing:
            db.execute(f"ALTER TABLE orders ADD COLUMN {name} {spec}")
    db.commit()

def _ensure_aux_tables(db) -> None:
    if USING_POSTGRES:
        cur = db.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS auth_logs(
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            email TEXT,
            event TEXT,
            ip TEXT,
            user_agent TEXT,
            created_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS credit_logs(
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            email TEXT,
            change_amount INTEGER,
            before_credits INTEGER,
            after_credits INTEGER,
            action TEXT,
            reason TEXT,
            operator TEXT,
            related_order_id TEXT,
            created_at TEXT
        )""")
        db.commit()
        return

    db.execute("""CREATE TABLE IF NOT EXISTS auth_logs(
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        email TEXT,
        event TEXT,
        ip TEXT,
        user_agent TEXT,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS credit_logs(
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        email TEXT,
        change_amount INTEGER,
        before_credits INTEGER,
        after_credits INTEGER,
        action TEXT,
        reason TEXT,
        operator TEXT,
        related_order_id TEXT,
        created_at TEXT
    )""")
    db.commit()

def get_db():
    if USING_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, **_pg_conn_kwargs())
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE,
            password TEXT,
            credits INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free',
            created_at TEXT,
            last_login_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            style TEXT,
            input_path TEXT,
            output_url TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            plan_id TEXT,
            amount INTEGER,
            credits INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            provider TEXT DEFAULT 'creem',
            provider_checkout_id TEXT,
            provider_order_id TEXT,
            provider_product_id TEXT,
            provider_checkout_url TEXT,
            provider_request_id TEXT,
            provider_customer_email TEXT,
            paid_at TEXT,
            admin_note TEXT
        )""")
        conn.commit()
        _ensure_orders_schema(conn)
        _ensure_aux_tables(conn)
        return conn

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE, password TEXT,
        credits INTEGER DEFAULT 0, plan TEXT DEFAULT 'free',
        created_at TEXT, last_login_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, user_id INTEGER, style TEXT,
        input_path TEXT, output_url TEXT,
        status TEXT DEFAULT 'pending', error TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders(
        id TEXT PRIMARY KEY, user_id INTEGER, plan_id TEXT,
        amount INTEGER, credits INTEGER, status TEXT DEFAULT 'pending', created_at TEXT,
        provider TEXT DEFAULT 'creem', provider_checkout_id TEXT, provider_order_id TEXT,
        provider_product_id TEXT, provider_checkout_url TEXT, provider_request_id TEXT,
        provider_customer_email TEXT, paid_at TEXT, admin_note TEXT)""")
    conn.commit()
    _ensure_orders_schema(conn)
    _ensure_aux_tables(conn)
    return conn

def db_execute(db, sql: str, params=()):
    return db.execute(_sqlite_param_sql(sql), params)

def db_begin_immediate(db):
    if USING_POSTGRES:
        return
    db.execute("BEGIN IMMEDIATE")

def _ensure_test_account_credits(db, email: str) -> int | None:
    if email != TEST_ACCOUNT_EMAIL:
        return None
    row = db_execute(db, "SELECT credits FROM users WHERE email=%s", (email,)).fetchone()
    if not row:
        return None
    credits = int(row[0] or 0)
    if credits < TEST_ACCOUNT_MIN_CREDITS:
        db_execute(db, "UPDATE users SET credits=%s WHERE email=%s", (TEST_ACCOUNT_MIN_CREDITS, email))
        db.commit()
        return TEST_ACCOUNT_MIN_CREDITS
    return credits

def _request_ip(request: Request | None) -> str:
    if not request:
        return ""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else ""

def _log_auth_event(db, user_id: int, email: str, event: str, request: Request | None = None):
    db_execute(
        db,
        """INSERT INTO auth_logs(id,user_id,email,event,ip,user_agent,created_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s)""",
        (
            str(uuid.uuid4()),
            user_id,
            email,
            event,
            _request_ip(request),
            (request.headers.get("user-agent", "") if request else "")[:500],
            datetime.utcnow().isoformat(),
        ),
    )

def _log_credit_event(
    db,
    *,
    user_id: int,
    email: str,
    change_amount: int,
    before_credits: int,
    after_credits: int,
    action: str,
    reason: str = "",
    operator: str = "system",
    related_order_id: str | None = None,
):
    db_execute(
        db,
        """INSERT INTO credit_logs
           (id,user_id,email,change_amount,before_credits,after_credits,action,reason,operator,related_order_id,created_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            str(uuid.uuid4()),
            user_id,
            email,
            change_amount,
            before_credits,
            after_credits,
            action,
            reason,
            operator,
            related_order_id,
            datetime.utcnow().isoformat(),
        ),
    )

@app.get("/", response_class=HTMLResponse)
def home():
    if INDEX_HTML.exists():
        resp = FileResponse(INDEX_HTML)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return HTMLResponse("<h1>灵感空间AI</h1><p>Frontend not found.</p>", status_code=200)

@app.head("/")
def home_head():
    return Response(status_code=200)

@app.get("/favicon.ico")
def favicon():
    # Return a tiny empty icon instead of a 404 to keep browser console clean.
    return Response(content=b"", media_type="image/x-icon")

def _site_base_url() -> str:
    for value in (PUBLIC_SITE_URL, NEXT_PUBLIC_BASE_URL):
        if value:
            return value.rstrip("/")
    if DOMAIN:
        return DOMAIN.rstrip("/") if DOMAIN.startswith("http") else f"https://{DOMAIN.strip('/')}"
    return "http://127.0.0.1:8000"

def _payment_return_url(order_id: str) -> str:
    return f"{_site_base_url()}/?payment=return&order_id={order_id}"

def _creem_enabled() -> bool:
    return bool(
        CREEM_API_KEY
        and CREEM_WEBHOOK_SECRET
        and CREEM_PRODUCT_C10
        and CREEM_PRODUCT_C50
        and CREEM_PRODUCT_C200
        and CREEM_PRODUCT_C500
    )

def _creem_callback_url() -> str:
    return f"{_site_base_url()}/api/payment/callback/creem"

def _creem_plan_product_id(plan_id: str) -> str:
    mapping = {
        "c10": CREEM_PRODUCT_C10,
        "c50": CREEM_PRODUCT_C50,
        "c200": CREEM_PRODUCT_C200,
        "c500": CREEM_PRODUCT_C500,
    }
    return mapping.get(plan_id, "").strip()

def _payment_payload_base(order_id: str, plan: dict, status: str = "pending") -> dict:
    return {
        "order_id": order_id,
        "amount": plan["price"],
        "name": plan["name"],
        "status": status,
        "provider": "creem",
        "pay_method": "checkout_redirect",
        "pay_url": "",
        "checkout_url": "",
        "return_url": _payment_return_url(order_id),
    }

def _mark_order_paid(order_id: str, total_amount: str | None = None, trade_no: str | None = None) -> bool:
    db = get_db()
    try:
        db_begin_immediate(db)
        row = db_execute(
            db,
            """SELECT o.user_id, o.credits, o.status, o.amount, u.email, u.credits
               FROM orders o
               LEFT JOIN users u ON u.id=o.user_id
               WHERE o.id=%s""",
            (order_id,),
        ).fetchone()
        if not row:
            db.rollback()
            return False
        user_id, credits, status, amount, email, before_credits = row
        if status == "paid":
            db.rollback()
            return True
        if total_amount is not None:
            try:
                paid_amount = int(float(total_amount))
                expected_amount = int(amount)
                if paid_amount not in {expected_amount, expected_amount * 100}:
                    db.rollback()
                    raise RuntimeError("支付金额不一致")
            except ValueError:
                db.rollback()
                raise RuntimeError("支付金额解析失败")
        db_execute(
            db,
            "UPDATE orders SET status='paid', provider_order_id=COALESCE(provider_order_id, %s), paid_at=%s WHERE id=%s AND status<>'paid'",
            (trade_no, datetime.utcnow().isoformat(), order_id),
        )
        db_execute(db, "UPDATE users SET credits=credits+%s WHERE id=%s", (credits, user_id))
        _log_credit_event(
            db,
            user_id=user_id,
            email=email or "",
            change_amount=int(credits or 0),
            before_credits=int(before_credits or 0),
            after_credits=int(before_credits or 0) + int(credits or 0),
            action="payment_credit",
            reason="支付成功到账",
            operator="system",
            related_order_id=order_id,
        )
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

def _creem_headers() -> dict:
    return {
        "x-api-key": CREEM_API_KEY,
        "Content-Type": "application/json",
    }

async def _creem_create_checkout(order_id: str, user: tuple, plan_id: str, plan: dict) -> dict:
    if not _creem_enabled():
        raise HTTPException(503, "Creem 支付参数未配置完整")

    product_id = _creem_plan_product_id(plan_id)
    if not product_id:
        raise HTTPException(503, f"Creem 商品未配置：{plan_id}")

    user_id, email = user[0], user[1]
    customer_email = email
    payload = {
        "product_id": product_id,
        "request_id": order_id,
        "units": 1,
        "customer": {
            "email": customer_email,
        },
        "success_url": _payment_return_url(order_id),
        "metadata": {
            "internal_order_id": order_id,
            "user_id": str(user_id),
            "plan_id": plan_id,
            "credits": str(plan["credits"]),
            "email": email,
        },
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{CREEM_API_BASE}/checkouts",
            headers=_creem_headers(),
            json=payload,
        )
    if not resp.is_success:
        detail = resp.text.strip() or resp.reason_phrase
        print(f"[creem] create checkout failed order={order_id} status={resp.status_code} body={detail}", flush=True)
        raise HTTPException(502, f"Creem 创建支付失败：{detail}")
    return resp.json()

async def _build_order_payment_payload(order_id: str, plan: dict, force_refresh: bool = False) -> dict:
    db = get_db()
    row = db_execute(
        db,
        "SELECT user_id, plan_id, provider_checkout_id, provider_checkout_url, status FROM orders WHERE id=%s",
        (order_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    user_id, plan_id, checkout_id, checkout_url, status = row
    user_row = _require_user_row(db, user_id)
    payload = _payment_payload_base(order_id, plan, status=status or "pending")
    if checkout_url and status != "paid" and not force_refresh:
        payload.update({
            "pay_url": checkout_url,
            "checkout_url": checkout_url,
            "checkout_id": checkout_id,
            "provider_product_id": _creem_plan_product_id(plan_id),
            "webhook_url": _creem_callback_url(),
        })
        return payload

    try:
        checkout = await _creem_create_checkout(order_id, user_row, plan_id, plan)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[creem] create checkout exception order={order_id} err={type(exc).__name__}", flush=True)
        raise HTTPException(502, "Creem 创建支付失败")

    checkout_id = checkout.get("id", "")
    checkout_url = checkout.get("checkout_url") or checkout.get("url") or ""
    order_obj = checkout.get("order") or {}
    product_obj = checkout.get("product") or {}
    provider_order_id = order_obj.get("id") if isinstance(order_obj, dict) else str(order_obj or "")
    provider_product_id = (
        product_obj.get("id")
        if isinstance(product_obj, dict)
        else str(product_obj or "")
    ) or _creem_plan_product_id(plan_id)
    customer = checkout.get("customer") or {}
    customer_email = customer.get("email") or user_row[1]
    db_execute(
        db,
        """UPDATE orders
           SET provider=%s,
               provider_checkout_id=%s,
               provider_order_id=%s,
               provider_product_id=%s,
               provider_checkout_url=%s,
               provider_request_id=%s,
               provider_customer_email=%s
           WHERE id=%s""",
        ("creem", checkout_id, provider_order_id, provider_product_id, checkout_url, order_id, customer_email, order_id),
    )
    db.commit()
    payload.update({
        "pay_url": checkout_url,
        "checkout_url": checkout_url,
        "checkout_id": checkout_id,
        "provider_product_id": provider_product_id,
        "webhook_url": _creem_callback_url(),
    })
    return payload

def _creem_signature_valid(raw_body: bytes, signature: str | None) -> bool:
    if not CREEM_WEBHOOK_SECRET or not signature:
        return False
    digest = hmac.new(CREEM_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature.strip())

def _set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        key="lkj_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=_site_base_url().startswith("https://"),
        max_age=60 * 60 * 24 * 30,
        path="/",
    )

def _clear_auth_cookie(response: Response):
    response.delete_cookie("lkj_token", path="/")

def _build_auth_token(uid: int | str) -> str:
    return jwt.encode({"sub": str(uid), "exp": datetime.utcnow() + timedelta(days=30)}, JWT_SECRET, algorithm="HS256")

def current_uid(request: Request, cred: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = None
        if cred and cred.credentials:
            token = cred.credentials
        elif request.cookies.get("lkj_token"):
            token = request.cookies.get("lkj_token")
        if not token:
            raise HTTPException(401, "Not authenticated.")
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        sub = payload.get("sub")
        if sub is None:
            raise HTTPException(401, "Invalid token.")
        return int(sub)
    except Exception:
        raise HTTPException(401, "Invalid token.")

def _require_user_row(db, uid):
    row = db_execute(db, "SELECT id,email,credits,plan FROM users WHERE id=%s", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "Account not found. Please log in again.")
    return row

def _require_admin(request: Request):
    admin_key = os.getenv("ADMIN_KEY", "").strip()
    if not admin_key:
        raise HTTPException(503, "Admin key is not configured.")
    supplied = request.headers.get("x-admin-key", "").strip()
    if supplied != admin_key:
        raise HTTPException(401, "Admin access denied.")
    return True

# ─── AUTH ─────────────────────────────────────────────
class AuthReq(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
def register(r: AuthReq, response: Response, request: Request):
    if len(r.password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    hashed = bcrypt.hashpw(r.password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    email = r.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Please enter a valid email address.")
    initial_credits = TEST_ACCOUNT_MIN_CREDITS if email == TEST_ACCOUNT_EMAIL else 2
    try:
        now = datetime.utcnow().isoformat()
        db_execute(
            db,
            "INSERT INTO users(email,password,credits,plan,created_at,last_login_at) VALUES(%s,%s,%s,%s,%s,%s)",
            (email, hashed, initial_credits, "free", now, now),
        )
        db.commit()
        uid = db_execute(db, "SELECT id FROM users WHERE email=%s", (email,)).fetchone()[0]
        _log_auth_event(db, uid, email, "register", request)
        _log_credit_event(
            db,
            user_id=uid,
            email=email,
            change_amount=initial_credits,
            before_credits=0,
            after_credits=initial_credits,
            action="signup_bonus",
            reason="新用户注册赠送次数",
            operator="system",
        )
        db.commit()
        token = _build_auth_token(uid)
        _set_auth_cookie(response, token)
        return {"token": token, "credits": initial_credits, "plan": "free", "email": email}
    except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(400, "Email already registered.")

@app.post("/api/auth/login")
def login(r: AuthReq, response: Response, request: Request):
    db = get_db()
    email = r.email.strip().lower()
    row = db_execute(db, "SELECT id,password,credits,plan FROM users WHERE email=%s", (email,)).fetchone()
    if not row or not bcrypt.checkpw(r.password.encode(), row[1].encode()):
        raise HTTPException(401, "Invalid email or password.")
    credits = _ensure_test_account_credits(db, email)
    db_execute(db, "UPDATE users SET last_login_at=%s WHERE id=%s", (datetime.utcnow().isoformat(), row[0]))
    _log_auth_event(db, row[0], email, "login", request)
    db.commit()
    token = _build_auth_token(row[0])
    _set_auth_cookie(response, token)
    return {"token": token, "credits": credits if credits is not None else row[2], "plan": row[3], "email": email}

@app.post("/api/auth/logout")
def logout(response: Response):
    _clear_auth_cookie(response)
    return {"ok": True}

@app.get("/api/auth/me")
def me(uid=Depends(current_uid)):
    db = get_db()
    row = _require_user_row(db, uid)
    credits = _ensure_test_account_credits(db, row[1])
    return {"email": row[1], "credits": credits if credits is not None else row[2], "plan": row[3]}

# ─── GENERATE ─────────────────────────────────────────
STYLES = {
    "modern": "现代简约", "nordic": "北欧", "chinese": "新中式",
    "luxury": "轻奢", "industrial": "工业风", "american": "美式乡村",
    "cream": "奶油风", "wabisabi": "侘寂风", "wood": "原木风",
    "french": "现代法式", "midcentury": "中古风", "minimal": "极简风",
}

STYLE_DETAILS = {
    "modern": "现代简约风，浅色墙面、简洁线条家具、低饱和度配色、干净克制的收纳与灯光",
    "nordic": "北欧风格，原木材质、奶白与浅灰主色、自然采光感、温暖布艺与绿植点缀",
    "chinese": "新中式风格，深浅木结合、中式格栅或山水装饰、东方留白、沉稳而雅致",
    "luxury": "轻奢风格，金属与石材质感、精致灯具、细腻软装、克制高级的酒店式氛围",
    "industrial": "工业风格，水泥灰或微水泥墙面、黑色金属结构、皮革与木材搭配、硬朗灯光",
    "american": "美式乡村风格，温暖木色、复古布艺、柔和灯光、生活化陈设与舒适家庭氛围",
    "cream": "奶油风格，奶白与浅杏色为主、圆润线条、柔和灯光、细腻布艺与温柔治愈的居住氛围",
    "wabisabi": "侘寂风格，自然肌理、微水泥或灰泥墙面、亚麻棉麻布艺、手作陶器与克制留白，强调不完美的平静感",
    "wood": "原木风格，大面积自然木色、浅暖中性色、简洁收纳、柔和采光与自然治愈的日式居住感",
    "french": "现代法式风格，法式线条与石膏造型、优雅拱形元素、浅暖高级灰调、轻奢细节与浪漫精致感",
    "midcentury": "中古风格，胡桃木与复古木色家具、经典几何线条、复古灯具、低饱和暖色和有年代感的质感陈设",
    "minimal": "极简风格，极少装饰、纯净线条、统一材质、低噪音配色与通透留白，整体克制安静且功能明确",
}

class GenReq(BaseModel):
    style: str; quality: str = "hd"

def _image_base64(input_path: str) -> str:
    if not Path(input_path).exists():
        return ""
    with open(input_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def _image_data_uri(input_path: str) -> str:
    path = Path(input_path)
    if not path.exists():
        return ""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            target_long_side = 1536
            scale = target_long_side / max(width, height)
            if scale > 1:
                new_size = (int(width * scale), int(height * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=95, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        ext = path.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        return f"data:{mime};base64,{_image_base64(input_path)}"

def _normalize_uploaded_image(file: UploadFile, content: bytes) -> tuple[str, bytes]:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    try:
        with Image.open(BytesIO(content)) as img:
            img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=95, optimize=True)
            return ".jpg", buf.getvalue()
    except Exception:
        # Keep the original file only when PIL cannot decode it; later generation
        # will surface a clear backend error rather than a silent upload failure.
        ext = suffix if suffix else ".jpg"
        return ext, content

def _control_signal_data_uri(input_path: str) -> str:
    path = Path(input_path)
    if not path.exists():
        return ""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            target_long_side = 1536
            scale = target_long_side / max(width, height)
            if scale > 1:
                new_size = (int(width * scale), int(height * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            canvas = img.copy()
            draw = ImageDraw.Draw(canvas)
            w, h = canvas.size
            margin = max(10, int(min(w, h) * 0.03))
            frame_w = max(6, int(min(w, h) * 0.012))
            accent = (220, 38, 38)
            draw.rectangle([margin, margin, w - margin, h - margin], outline=accent, width=frame_w)

            # 视觉信号：四角箭头，强调保持原图边界与构图
            arrow = max(4, int(min(w, h) * 0.008))
            arms = [
                ((margin * 2, margin * 2), (margin * 2 + 40, margin * 2), (margin * 2 + 20, margin * 2 + 20)),
                ((w - margin * 2, margin * 2), (w - margin * 2 - 40, margin * 2), (w - margin * 2 - 20, margin * 2 + 20)),
                ((margin * 2, h - margin * 2), (margin * 2 + 40, h - margin * 2), (margin * 2 + 20, h - margin * 2 - 20)),
                ((w - margin * 2, h - margin * 2), (w - margin * 2 - 40, h - margin * 2), (w - margin * 2 - 20, h - margin * 2 - 20)),
            ]
            for p1, p2, p3 in arms:
                draw.line([p1, p2], fill=accent, width=arrow)
                draw.line([p2, p3], fill=accent, width=arrow)
                draw.line([p3, p1], fill=accent, width=arrow)

            note = "保留原图结构\n按上传图改造"
            draw.rounded_rectangle([margin * 2, margin * 2, margin * 2 + 360, margin * 2 + 128], radius=18, fill=(255, 255, 255))
            draw.text((margin * 2 + 18, margin * 2 + 16), note, fill=accent)

            buf = BytesIO()
            canvas.save(buf, format="JPEG", quality=95, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

def resolve_output_size(input_path: str, quality: str) -> str:
    default_size = "2048x2048" if quality == "hd" else "1920x1920"
    if not Path(input_path).exists():
        return default_size

    try:
        with Image.open(input_path) as img:
            width, height = img.size
    except Exception:
        return default_size

    if quality != "hd":
        return "2304x1536" if width >= height else "1536x2304"

    if width >= height * 1.25:
        return "2560x1440"
    if height >= width * 1.25:
        return "1440x2560"
    return "2048x2048"

def _room_prompt(style_name: str, style_detail: str, model_name: str | None = None) -> str:
    base_prompt = (
        f"这是一个室内图像编辑任务，请基于上传房间照片直接生成{style_name}装修效果图。"
        "把上传图当作底图而不是灵感图，必须尽最大程度保留原图的空间结构、墙体位置、门窗位置、天花板形状、地面边界、镜头机位、透视关系、房间比例和采光方向，"
        "不要重构成另一套房，不要移动窗户门洞，不要新增或删减房间，不要改变整体构图。"
        f"只允许在原空间内替换材质、墙面、地面、吊顶细节、家具、灯具、窗帘、装饰画和软装，整体风格要求：{style_detail}。"
        "输出必须像真实设计师在原图上做的装修改造图，保留原房间特征，细节自然，避免夸张结构变化。"
    )
    if model_name and "seedream-4-5" in model_name:
        return (
            base_prompt
            + " 这是严格的原图改造任务，不是重新生成新房间。必须把上传照片视为唯一房间底稿，"
            + "最终结果必须仍然是同一套房、同一个拍摄角度、同一套门窗墙顶地关系。"
            + " 禁止重画空间布局，禁止改变房间长宽比例，禁止更换窗户位置、门洞位置、梁柱位置、背景结构、视角高度与镜头焦段。"
            + " 如果某种装修风格与原始结构冲突，优先保留原始结构，只做软装、材质、配色和局部家具调整。"
            + " 宁可风格表达弱一点，也不能把原房间改成另一套房。结果必须看起来像在原始房间照片上完成翻新，而不是全新渲染图。"
        )
    return (
        base_prompt
    )

def build_doubao_payload(input_path: str, style: str, quality: str) -> dict:
    return build_doubao_payload_for_model(input_path, style, quality, ARK_IMAGE_MODEL)

def build_doubao_payload_for_model(input_path: str, style: str, quality: str, model_name: str) -> dict:
    style_name = STYLES.get(style, "现代简约")
    style_detail = STYLE_DETAILS.get(style, STYLE_DETAILS["modern"])
    size = resolve_output_size(input_path, quality)
    img_data_uri = _image_data_uri(input_path)
    control_data_uri = _control_signal_data_uri(input_path)
    if not img_data_uri:
        raise RuntimeError("The uploaded room image could not be read.")
    payload = {
        "model": model_name,
        "prompt": _room_prompt(style_name, style_detail, model_name),
        "n": 1,
        "size": size,
        "response_format": "url",
    }
    # Seedream 5.0 can take the uploaded room image directly, while the control
    # overlay reinforces the original framing and room boundaries.
    if "seedream-5-0" in model_name:
        payload["image"] = img_data_uri
        if control_data_uri:
            payload["reference_images"] = [control_data_uri]
        return payload

    payload["reference_images"] = [img_data_uri, control_data_uri] if control_data_uri else [img_data_uri]
    return payload

def _is_model_limit_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(token in lowered for token in [
        "setlimitexceeded",
        "服务暂停",
        "安全体验模式",
        "模型激活页面",
        "推理限制",
    ])

async def _call_doubao_with_model(input_path: str, style: str, quality: str, model_name: str) -> str:
    payload = build_doubao_payload_for_model(input_path, style, quality, model_name)
    headers = {"Authorization": f"Bearer {ARK_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        ref_count = len(payload.get("reference_images", []))
        print(f"[doubao] generating model={payload['model']} style={style} size={payload['size']} image={'image' in payload} reference_images={ref_count}")
        r2 = await client.post(
            "https://ark.cn-beijing.volces.com/api/v3/images/generations",
            headers=headers,
            json=payload,
        )
        if not r2.is_success:
            raise RuntimeError(f"豆包API错误: {r2.text}")
        data = r2.json()
        items = data.get("data") or []
        if not items or not items[0].get("url"):
            raise RuntimeError(f"豆包API返回异常: {json.dumps(data, ensure_ascii=False)}")
        return items[0]["url"]

async def call_doubao(input_path: str, style: str, quality: str) -> str:
    try:
        return await _call_doubao_with_model(input_path, style, quality, ARK_IMAGE_MODEL)
    except RuntimeError as exc:
        if not ARK_IMAGE_FALLBACK_MODEL or ARK_IMAGE_FALLBACK_MODEL == ARK_IMAGE_MODEL:
            raise
        if not _is_model_limit_error(str(exc)):
            raise
        print(
            f"[doubao] primary model limited, fallback to {ARK_IMAGE_FALLBACK_MODEL} "
            f"from {ARK_IMAGE_MODEL}"
        )
        return await _call_doubao_with_model(input_path, style, quality, ARK_IMAGE_FALLBACK_MODEL)

async def process_job(job_id: str, input_path: str, style: str, quality: str):
    db = get_db()
    try:
        db_execute(db, "UPDATE jobs SET status='processing' WHERE id=%s", (job_id,)); db.commit()
        url = await call_doubao(input_path, style, quality)
        db_execute(db, "UPDATE jobs SET status='done',output_url=%s WHERE id=%s", (url, job_id))
    except Exception as e:
        print(f"[job:{job_id}] failed style={style} quality={quality} input={input_path} error={e}")
        db_execute(db, "UPDATE jobs SET status='failed',error=%s WHERE id=%s", (str(e), job_id))
    finally:
        db.commit()

@app.post("/api/generate/upload")
async def upload(file: UploadFile = File(...), uid=Depends(current_uid)):
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
    content_type = (file.content_type or "").lower()
    if not (content_type.startswith("image/") or content_type in allowed_types):
        raise HTTPException(400, "仅支持图片")
    raw = await file.read()
    ext, normalized = _normalize_uploaded_image(file, raw)
    path = UPLOAD_DIR / f"{uuid.uuid4()}{ext}"
    path.write_bytes(normalized)
    return {"file_id": path.name}

@app.post("/api/generate/{file_id}")
async def generate(file_id: str, req: GenReq, bg: BackgroundTasks, uid=Depends(current_uid)):
    db = get_db()
    row = _require_user_row(db, uid)
    credits = row[2]
    if credits < 1:
        raise HTTPException(402, "Not enough credits. Please upgrade first.")
    input_path = str(UPLOAD_DIR / file_id)
    if not Path(input_path).exists():
        raise HTTPException(404, "图片不存在")
    db_execute(db, "UPDATE users SET credits=credits-1 WHERE id=%s", (uid,)); db.commit()
    job_id = str(uuid.uuid4())
    db_execute(db, "INSERT INTO jobs VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
               (job_id, uid, req.style, input_path, None, "pending", None, datetime.utcnow().isoformat()))
    db.commit()
    bg.add_task(process_job, job_id, input_path, req.style, req.quality)
    return {"job_id": job_id, "status": "processing"}

@app.get("/api/generate/status/{job_id}")
def status(job_id: str, uid=Depends(current_uid)):
    row = db_execute(get_db(), "SELECT id,status,output_url,error FROM jobs WHERE id=%s AND user_id=%s",
                     (job_id, uid)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    return {"job_id": row[0], "status": row[1], "output_url": row[2], "error": row[3]}

@app.get("/api/generate/history")
def history(uid=Depends(current_uid)):
    rows = db_execute(get_db(), "SELECT id,style,output_url,status,created_at FROM jobs WHERE user_id=%s ORDER BY created_at DESC LIMIT 30", (uid,)).fetchall()
    return [{"id":r[0], "style":r[1], "url":r[2], "status":r[3], "created_at":r[4]} for r in rows]

# ─── PAYMENT ──────────────────────────────────────────
PLANS = {
    "c10": {"price": 30, "credits": 13, "name": "Starter Pack"},
    "c50": {"price": 99, "credits": 78, "name": "Monthly"},
    "c200": {"price": 269, "credits": 260, "name": "Quarterly"},
    "c500": {"price": 999, "credits": 2600, "name": "Pro Annual"},
}

class OrderReq(BaseModel):
    plan_id: str

@app.post("/api/payment/create")
async def create_order(r: OrderReq, uid=Depends(current_uid)):
    plan = PLANS.get(r.plan_id)
    if not plan:
        raise HTTPException(400, "无效套餐")
    db = get_db()
    _require_user_row(db, uid)
    oid = f"LKJ{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
    db_execute(
        db,
        """INSERT INTO orders
           (id, user_id, plan_id, amount, credits, status, created_at, provider, provider_product_id, provider_request_id)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            oid,
            uid,
            r.plan_id,
            plan["price"],
            plan["credits"],
            "pending",
            datetime.utcnow().isoformat(),
            "creem",
            _creem_plan_product_id(r.plan_id),
            oid,
        ),
    )
    db.commit()
    return await _build_order_payment_payload(oid, plan)

@app.get("/api/payment/status/{order_id}")
async def order_status(order_id: str, uid=Depends(current_uid)):
    db = get_db()
    _require_user_row(db, uid)
    row = db_execute(
        db,
        "SELECT status, provider_order_id FROM orders WHERE id=%s AND user_id=%s",
        (order_id, uid),
    ).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    status, provider_order_id = row
    user_credits = db_execute(db, "SELECT credits FROM users WHERE id=%s", (uid,)).fetchone()[0]
    return {
        "status": status,
        "credits": user_credits,
        "trade_no": provider_order_id,
    }

@app.get("/api/payment/order/{order_id}")
async def order_detail(order_id: str, uid=Depends(current_uid)):
    db = get_db()
    _require_user_row(db, uid)
    row = db_execute(
        db,
        "SELECT plan_id, status, provider_checkout_id, provider_checkout_url, provider_product_id, provider_order_id FROM orders WHERE id=%s AND user_id=%s",
        (order_id, uid),
    ).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    plan_id, status, checkout_id, checkout_url, provider_product_id, provider_order_id = row
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(400, "无效套餐")
    if status == "paid":
        payload = _payment_payload_base(order_id, plan, status="paid")
    else:
        payload = await _build_order_payment_payload(order_id, plan, force_refresh=True)
        payload["status"] = status
    payload["checkout_id"] = checkout_id
    payload["checkout_url"] = checkout_url or payload.get("checkout_url", "")
    payload["pay_url"] = payload.get("checkout_url") or checkout_url or ""
    payload["provider_product_id"] = provider_product_id or _creem_plan_product_id(plan_id)
    payload["trade_no"] = provider_order_id
    payload["webhook_url"] = _creem_callback_url()
    return payload

@app.get("/api/billing/history")
def billing_history(uid=Depends(current_uid)):
    rows = db_execute(
        get_db(),
        """SELECT id, plan_id, amount, credits, status, created_at, provider, provider_order_id
           FROM orders
           WHERE user_id=%s
           ORDER BY created_at DESC
           LIMIT 50""",
        (uid,),
    ).fetchall()
    items = []
    for row in rows:
        plan_id = row[1]
        plan = PLANS.get(plan_id, {})
        items.append({
            "order_id": row[0],
            "plan_id": plan_id,
            "plan_name": plan.get("name", plan_id),
            "amount": row[2],
            "credits": row[3],
            "status": row[4],
            "created_at": row[5],
            "provider": row[6],
            "trade_no": row[7],
            "currency": "USD",
        })
    return items

@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request, _=Depends(_require_admin)):
    db = get_db()
    users = db_execute(
        db,
        "SELECT id,email,credits,plan,created_at,last_login_at FROM users ORDER BY id DESC LIMIT 200"
    ).fetchall()
    orders = db_execute(
        db,
        """SELECT o.id,o.user_id,u.email,o.plan_id,o.amount,o.credits,o.status,o.created_at,o.provider,o.provider_order_id
           FROM orders o
           LEFT JOIN users u ON u.id=o.user_id
           ORDER BY o.created_at DESC
           LIMIT 200"""
    ).fetchall()
    total_users = db_execute(db, "SELECT COUNT(*) FROM users").fetchone()[0]
    total_orders = db_execute(db, "SELECT COUNT(*) FROM orders").fetchone()[0]
    paid_orders = db_execute(db, "SELECT COUNT(*) FROM orders WHERE status=%s", ("paid",)).fetchone()[0]
    total_revenue = db_execute(db, "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status=%s", ("paid",)).fetchone()[0] or 0
    total_credits = db_execute(db, "SELECT COALESCE(SUM(credits),0) FROM users").fetchone()[0] or 0
    user_rows = [
        {
            "id": row[0],
            "email": row[1],
            "credits": row[2],
            "plan": row[3],
            "created_at": row[4],
            "last_login_at": row[5],
        }
        for row in users
    ]
    order_rows = [
        {
            "order_id": row[0],
            "user_id": row[1],
            "email": row[2],
            "plan_id": row[3],
            "plan_name": PLANS.get(row[3], {}).get("name", row[3]),
            "amount": row[4],
            "credits": row[5],
            "status": row[6],
            "created_at": row[7],
            "provider": row[8],
            "trade_no": row[9],
        }
        for row in orders
    ]
    return {
        "summary": {
            "total_users": total_users,
            "total_orders": total_orders,
            "paid_orders": paid_orders,
            "total_revenue": total_revenue,
            "total_credits": total_credits,
        },
        "users": user_rows,
        "orders": order_rows,
    }

class AdminCreditReq(BaseModel):
    email: str
    credits: int

class AdminGiftReq(BaseModel):
    email: str
    credits: int
    reason: str = ""

class AdminManualOrderReq(BaseModel):
    email: str
    plan_id: str
    amount: int | None = None
    credits: int | None = None
    status: str = "paid"
    note: str = ""

@app.post("/api/admin/users/credits")
def admin_update_credits(payload: AdminCreditReq, request: Request, _=Depends(_require_admin)):
    db = get_db()
    email = payload.email.strip().lower()
    row = db_execute(db, "SELECT id,credits FROM users WHERE email=%s", (email,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found.")
    before_credits = int(row[1] or 0)
    after_credits = int(payload.credits)
    db_execute(db, "UPDATE users SET credits=%s WHERE id=%s", (after_credits, row[0]))
    _log_credit_event(
        db,
        user_id=row[0],
        email=email,
        change_amount=after_credits - before_credits,
        before_credits=before_credits,
        after_credits=after_credits,
        action="admin_set_credits",
        reason="后台直接修改剩余次数",
        operator="admin",
    )
    db.commit()
    return {"ok": True, "email": email, "credits": payload.credits}

@app.post("/api/admin/users/gift")
def admin_gift_credits(payload: AdminGiftReq, request: Request, _=Depends(_require_admin)):
    db = get_db()
    email = payload.email.strip().lower()
    credits = int(payload.credits or 0)
    if credits <= 0:
        raise HTTPException(400, "Credits must be greater than 0.")
    row = db_execute(db, "SELECT id,credits FROM users WHERE email=%s", (email,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found.")
    before_credits = int(row[1] or 0)
    after_credits = before_credits + credits
    db_execute(db, "UPDATE users SET credits=%s WHERE id=%s", (after_credits, row[0]))
    _log_credit_event(
        db,
        user_id=row[0],
        email=email,
        change_amount=credits,
        before_credits=before_credits,
        after_credits=after_credits,
        action="admin_gift_credits",
        reason=(payload.reason or "后台赠送次数").strip(),
        operator="admin",
    )
    db.commit()
    return {"ok": True, "email": email, "credits": after_credits}

@app.post("/api/admin/orders/manual")
def admin_create_manual_order(payload: AdminManualOrderReq, request: Request, _=Depends(_require_admin)):
    db = get_db()
    email = payload.email.strip().lower()
    row = db_execute(db, "SELECT id,credits FROM users WHERE email=%s", (email,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found.")
    user_id, before_credits = row[0], int(row[1] or 0)
    plan = PLANS.get(payload.plan_id)
    if not plan:
        raise HTTPException(400, "Invalid plan.")
    amount = int(payload.amount if payload.amount is not None else plan["price"])
    credits = int(payload.credits if payload.credits is not None else plan["credits"])
    status = (payload.status or "paid").strip().lower()
    if status not in {"paid", "pending", "failed"}:
        raise HTTPException(400, "Invalid status.")
    order_id = f"MANUAL{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
    now = datetime.utcnow().isoformat()
    db_execute(
        db,
        """INSERT INTO orders
           (id,user_id,plan_id,amount,credits,status,created_at,provider,provider_request_id,provider_customer_email,paid_at,admin_note)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            order_id,
            user_id,
            payload.plan_id,
            amount,
            credits,
            status,
            now,
            "manual",
            order_id,
            email,
            now if status == "paid" else None,
            (payload.note or "").strip(),
        ),
    )
    after_credits = before_credits
    if status == "paid":
        after_credits = before_credits + credits
        db_execute(db, "UPDATE users SET credits=%s WHERE id=%s", (after_credits, user_id))
        _log_credit_event(
            db,
            user_id=user_id,
            email=email,
            change_amount=credits,
            before_credits=before_credits,
            after_credits=after_credits,
            action="manual_order_credit",
            reason=(payload.note or "后台手动补单").strip() or "后台手动补单",
            operator="admin",
            related_order_id=order_id,
        )
    db.commit()
    return {"ok": True, "order_id": order_id, "status": status, "email": email, "credits": after_credits}

@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, request: Request, _=Depends(_require_admin)):
    db = get_db()
    user = db_execute(
        db,
        "SELECT id,email,credits,plan,created_at,last_login_at FROM users WHERE id=%s",
        (user_id,),
    ).fetchone()
    if not user:
        raise HTTPException(404, "User not found.")
    orders = db_execute(
        db,
        """SELECT id,plan_id,amount,credits,status,created_at,provider,provider_order_id,admin_note
           FROM orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    auth_logs = db_execute(
        db,
        """SELECT id,event,ip,user_agent,created_at
           FROM auth_logs WHERE user_id=%s ORDER BY created_at DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    credit_logs = db_execute(
        db,
        """SELECT id,change_amount,before_credits,after_credits,action,reason,operator,related_order_id,created_at
           FROM credit_logs WHERE user_id=%s ORDER BY created_at DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    return {
        "user": {
            "id": user[0],
            "email": user[1],
            "credits": user[2],
            "plan": user[3],
            "created_at": user[4],
            "last_login_at": user[5],
        },
        "orders": [
            {
                "order_id": row[0],
                "plan_id": row[1],
                "plan_name": PLANS.get(row[1], {}).get("name", row[1]),
                "amount": row[2],
                "credits": row[3],
                "status": row[4],
                "created_at": row[5],
                "provider": row[6],
                "trade_no": row[7],
                "note": row[8],
            }
            for row in orders
        ],
        "auth_logs": [
            {
                "id": row[0],
                "event": row[1],
                "ip": row[2],
                "user_agent": row[3],
                "created_at": row[4],
            }
            for row in auth_logs
        ],
        "credit_logs": [
            {
                "id": row[0],
                "change_amount": row[1],
                "before_credits": row[2],
                "after_credits": row[3],
                "action": row[4],
                "reason": row[5],
                "operator": row[6],
                "related_order_id": row[7],
                "created_at": row[8],
            }
            for row in credit_logs
        ],
    }

@app.get("/api/admin/orders/export")
def admin_orders_export(request: Request, _=Depends(_require_admin)):
    db = get_db()
    rows = db_execute(
        db,
        """SELECT o.id,u.email,o.plan_id,o.amount,o.credits,o.status,o.provider,o.provider_order_id,o.created_at,o.paid_at,o.admin_note
           FROM orders o
           LEFT JOIN users u ON u.id=o.user_id
           ORDER BY o.created_at DESC
           LIMIT 1000"""
    ).fetchall()
    output = ["order_id,email,plan_id,plan_name,amount,credits,status,provider,trade_no,created_at,paid_at,admin_note"]
    for row in rows:
        values = [
            row[0],
            row[1] or "",
            row[2] or "",
            PLANS.get(row[2], {}).get("name", row[2] or ""),
            str(row[3] or 0),
            str(row[4] or 0),
            row[5] or "",
            row[6] or "",
            row[7] or "",
            row[8] or "",
            row[9] or "",
            row[10] or "",
        ]
        escaped = ['"' + str(v).replace('"', '""') + '"' for v in values]
        output.append(",".join(escaped))
    csv_content = "\ufeff" + "\n".join(output)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="linggan-orders-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.csv"'},
    )

@app.post("/api/payment/callback/creem")
async def creem_cb(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("creem-signature")
    if not _creem_signature_valid(raw_body, signature):
        return PlainTextResponse("invalid signature", status_code=400)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return PlainTextResponse("invalid payload", status_code=400)

    event_type = payload.get("eventType") or payload.get("type") or ""
    if event_type != "checkout.completed":
        return PlainTextResponse("ignored")

    obj = payload.get("object") or {}
    metadata = obj.get("metadata") or {}
    internal_order_id = (
        metadata.get("internal_order_id")
        or metadata.get("order_id")
        or obj.get("request_id")
        or payload.get("request_id")
    )
    if not internal_order_id:
        return PlainTextResponse("missing order id", status_code=400)

    order_info = obj.get("order") or {}
    total_amount = order_info.get("amount")
    provider_order_id = order_info.get("id") or obj.get("id")

    db = get_db()
    db_execute(
        db,
        """UPDATE orders
           SET provider=%s,
               provider_checkout_id=COALESCE(provider_checkout_id, %s),
               provider_order_id=COALESCE(provider_order_id, %s),
               provider_checkout_url=COALESCE(provider_checkout_url, %s)
           WHERE id=%s""",
        (
            "creem",
            obj.get("id"),
            provider_order_id,
            obj.get("checkout_url") or obj.get("url") or "",
            internal_order_id,
        ),
    )
    db.commit()

    try:
        _mark_order_paid(internal_order_id, total_amount, provider_order_id)
    except Exception:
        return PlainTextResponse("mark paid failed", status_code=400)
    return PlainTextResponse("ok")
