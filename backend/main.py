import os, uuid, sqlite3, base64, asyncio, time, json, re
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO
from html import escape, unescape
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
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.backends import default_backend

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
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "")
ALIPAY_PRIVATE_KEY = os.getenv("ALIPAY_PRIVATE_KEY", "")
ALIPAY_PUBLIC_KEY = os.getenv("ALIPAY_PUBLIC_KEY", "")
ALIPAY_PRIVATE_KEY_PATH = os.getenv("ALIPAY_PRIVATE_KEY_PATH", "")
ALIPAY_PUBLIC_KEY_PATH = os.getenv("ALIPAY_PUBLIC_KEY_PATH", "")
ALIPAY_NOTIFY_URL = os.getenv("ALIPAY_NOTIFY_URL", "")
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "")
NEXT_PUBLIC_BASE_URL = os.getenv("NEXT_PUBLIC_BASE_URL", "")
DOMAIN = os.getenv("DOMAIN", "")
ALIPAY_GATEWAY = os.getenv("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do")
MANUAL_PAYMENT_QR_URL = os.getenv("MANUAL_PAYMENT_QR_URL", "")
MANUAL_PAYMENT_LABEL = os.getenv("MANUAL_PAYMENT_LABEL", "支付宝扫码转账")
UPLOAD_DIR   = UPLOAD_ROOT
DATA_DIR     = DATA_ROOT
DB_PATH      = DATA_DIR / "app.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ROOT_DIR = Path(__file__).resolve().parent.parent
BUNDLED_UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads"
INDEX_HTML = ROOT_DIR / "index.html"
PAYMENT_PROOF_DIR = UPLOAD_ROOT / "payment-proofs"
PAYMENT_PROOF_DIR.mkdir(parents=True, exist_ok=True)
PAYMENT_CONFIG_DIR = UPLOAD_ROOT / "payment-config"
PAYMENT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_MANUAL_PAYMENT_QR_FILE = PAYMENT_CONFIG_DIR / "alipay_qr.png"
ALIPAY_PRECREATE_ALLOWED = True
TEST_ACCOUNT_PHONE = "15251872890"
TEST_ACCOUNT_MIN_CREDITS = 500
app = FastAPI(title="灵感空间AI")
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

# ─── DB ───────────────────────────────────────────────
USING_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

def _sqlite_param_sql(sql: str) -> str:
    return sql.replace("%s", "?") if not USING_POSTGRES else sql

def _pg_conn_kwargs() -> dict:
    return {"autocommit": False}

def get_db():
    if USING_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, **_pg_conn_kwargs())
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE,
            password TEXT,
            credits INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free'
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
            created_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        cur.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_proof_url TEXT
        """)
        cur.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_note TEXT
        """)
        cur.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_submitted_at TEXT
        """)
        conn.commit()
        return conn

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE, password TEXT,
        credits INTEGER DEFAULT 0, plan TEXT DEFAULT 'free')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, user_id INTEGER, style TEXT,
        input_path TEXT, output_url TEXT,
        status TEXT DEFAULT 'pending', error TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders(
        id TEXT PRIMARY KEY, user_id INTEGER, plan_id TEXT,
        amount INTEGER, credits INTEGER, status TEXT DEFAULT 'pending', created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS app_settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "payment_proof_url" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_url TEXT")
    if "payment_note" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_note TEXT")
    if "payment_submitted_at" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_submitted_at TEXT")
    conn.commit()
    return conn

def db_execute(db, sql: str, params=()):
    return db.execute(_sqlite_param_sql(sql), params)

def db_begin_immediate(db):
    if USING_POSTGRES:
        return
    db.execute("BEGIN IMMEDIATE")

def _ensure_test_account_credits(db, phone: str) -> int | None:
    if phone != TEST_ACCOUNT_PHONE:
        return None
    row = db_execute(db, "SELECT credits FROM users WHERE phone=%s", (phone,)).fetchone()
    if not row:
        return None
    credits = int(row[0] or 0)
    if credits < TEST_ACCOUNT_MIN_CREDITS:
        db_execute(db, "UPDATE users SET credits=%s WHERE phone=%s", (TEST_ACCOUNT_MIN_CREDITS, phone))
        db.commit()
        return TEST_ACCOUNT_MIN_CREDITS
    return credits

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

def _alipay_enabled() -> bool:
    has_private = bool(ALIPAY_PRIVATE_KEY or ALIPAY_PRIVATE_KEY_PATH)
    has_public = bool(ALIPAY_PUBLIC_KEY or ALIPAY_PUBLIC_KEY_PATH)
    return all([ALIPAY_APP_ID, has_private, has_public, ALIPAY_NOTIFY_URL])

def _mock_payment_allowed() -> bool:
    base = _site_base_url().lower()
    if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
        return True
    env = os.getenv("APP_ENV", "").strip().lower()
    return env in {"dev", "development", "local"}

def _require_mock_payment_allowed() -> None:
    if not _mock_payment_allowed():
        raise HTTPException(404, "订单不存在")

def _normalize_pem(raw: str, kind: str) -> str:
    value = (raw or "").strip().strip('"').strip("'").replace("\\n", "\n")
    if "BEGIN" in value:
        return value
    header = "PRIVATE KEY" if kind == "private" else "PUBLIC KEY"
    return f"-----BEGIN {header}-----\n{value}\n-----END {header}-----"

def _resolve_secret_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    backend_dir = Path(__file__).resolve().parent
    candidates = [
        backend_dir / path,
        backend_dir / "secrets" / path.name,
        backend_dir.parent / path,
        backend_dir.parent / "secrets" / path.name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return backend_dir / path

def _load_alipay_private_key():
    if ALIPAY_PRIVATE_KEY_PATH:
        key_path = _resolve_secret_path(ALIPAY_PRIVATE_KEY_PATH)
        if not key_path.exists():
            raise RuntimeError(f"支付宝私钥文件不存在: {key_path}")
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    return serialization.load_pem_private_key(
        _normalize_pem(ALIPAY_PRIVATE_KEY, "private").encode(),
        password=None,
    )

def _load_alipay_public_key():
    if ALIPAY_PUBLIC_KEY_PATH:
        key_path = _resolve_secret_path(ALIPAY_PUBLIC_KEY_PATH)
        if not key_path.exists():
            raise RuntimeError(f"支付宝公钥文件不存在: {key_path}")
        return serialization.load_pem_public_key(key_path.read_bytes())
    return serialization.load_pem_public_key(
        _normalize_pem(ALIPAY_PUBLIC_KEY, "public").encode()
    )

def _alipay_sign(params: dict) -> str:
    content = "&".join(f"{k}={params[k]}" for k in sorted(params))
    signature = _load_alipay_private_key().sign(
        content.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()

def _alipay_verify(params: dict) -> bool:
    signature = params.get("sign", "")
    if not signature:
        return False
    content = "&".join(
        f"{k}={params[k]}"
        for k in sorted(k for k in params.keys() if k not in {"sign", "sign_type"})
    )
    try:
        _load_alipay_public_key().verify(
            base64.b64decode(signature),
            content.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False

def _parse_alipay_json(raw: bytes) -> dict:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    raise RuntimeError("支付宝响应解析失败")

def _payment_return_url(order_id: str) -> str:
    return f"{_site_base_url()}/?payment=return&order_id={order_id}"

def _is_mobile_request(request: Request) -> bool:
    ua = (request.headers.get("user-agent", "") or "").lower()
    return any(token in ua for token in ("mobile", "android", "iphone", "ipad", "ipod"))

def _mark_order_paid(order_id: str, total_amount: str | None = None, trade_no: str | None = None) -> bool:
    db = get_db()
    try:
        db_begin_immediate(db)
        row = db_execute(db, "SELECT user_id, credits, status, amount FROM orders WHERE id=%s", (order_id,)).fetchone()
        if not row:
            db.rollback()
            return False
        user_id, credits, status, amount = row
        if status == "paid":
            db.rollback()
            return True
        if total_amount is not None:
            try:
                if int(float(total_amount)) != int(amount):
                    db.rollback()
                    raise RuntimeError("支付金额不一致")
            except ValueError:
                db.rollback()
                raise RuntimeError("支付金额解析失败")
        db_execute(db, "UPDATE orders SET status='paid' WHERE id=%s AND status<>'paid'", (order_id,))
        db_execute(db, "UPDATE users SET credits=credits+%s WHERE id=%s", (credits, user_id))
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

def _get_setting(key: str) -> str:
    db = get_db()
    row = db_execute(db, "SELECT value FROM app_settings WHERE key=%s", (key,)).fetchone()
    return row[0] if row and row[0] is not None else ""

def _set_setting(key: str, value: str) -> None:
    db = get_db()
    if USING_POSTGRES:
        db_execute(
            db,
            """
            INSERT INTO app_settings(key, value) VALUES(%s, %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """,
            (key, value),
        )
    else:
        db_execute(
            db,
            "INSERT INTO app_settings(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    db.commit()

def _manual_payment_config() -> dict:
    config = {}
    raw = _get_setting("manual_payment_config")
    if raw:
        try:
            config = json.loads(raw)
        except Exception:
            config = {}
    default_qr_url = ""
    if DEFAULT_MANUAL_PAYMENT_QR_FILE.exists():
        default_qr_url = f"{_site_base_url()}/uploads/payment-config/{DEFAULT_MANUAL_PAYMENT_QR_FILE.name}"
    # Always prefer the bundled QR file we ship with the deployment so stale DB
    # config cannot keep serving an expired code after we replace the image.
    qr_url = (default_qr_url or MANUAL_PAYMENT_QR_URL or config.get("qr_url") or "").strip()
    label = (config.get("label") or MANUAL_PAYMENT_LABEL or "支付宝扫码转账").strip() or "支付宝扫码转账"
    return {
        "qr_url": qr_url,
        "label": label,
    }

def _manual_payment_enabled() -> bool:
    return bool(_manual_payment_config().get("qr_url"))

def _manual_payment_payload(order_id: str, plan: dict) -> dict:
    config = _manual_payment_config()
    manual_qr = config["qr_url"]
    manual_pay_url = _alipay_qr_display_url(manual_qr) or (manual_qr if manual_qr.startswith("https://") else "")
    return {
        "order_id": order_id,
        "amount": plan["price"],
        "name": plan["name"],
        "pay_url": manual_pay_url,
        "embed_pay_url": "",
        "pay_method": "manual_review",
        "provider": "manual_review",
        "return_url": _payment_return_url(order_id),
        "qr_code": manual_qr,
        "qr_image_url": _alipay_qr_display_url(manual_qr) or manual_qr,
        "display_mode": "manual_review",
        "manual_review": True,
        "manual_label": config["label"],
    }

def _mock_payment_payload(order_id: str, plan: dict) -> dict:
    return {
        "order_id": order_id,
        "amount": plan["price"],
        "name": plan["name"],
        "pay_url": f"{_site_base_url()}/api/payment/mock/{order_id}",
        "embed_pay_url": "",
        "pay_method": "mock",
        "provider": "mock",
        "return_url": _payment_return_url(order_id),
        "qr_code": None,
        "qr_image_url": None,
        "display_mode": "redirect",
        "manual_review": False,
        "manual_label": "",
    }

async def _alipay_api_call(method: str, biz_content: dict) -> dict:
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": method,
        "charset": "UTF-8",
        "sign_type": "RSA2",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=True, separators=(",", ":")),
    }
    params["sign"] = _alipay_sign(params)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ALIPAY_GATEWAY, data=params)
    return _parse_alipay_json(resp.content)

async def _query_alipay_trade(order_id: str) -> dict:
    data = await _alipay_api_call("alipay.trade.query", {"out_trade_no": order_id})
    return data.get("alipay_trade_query_response", {})

async def _precreate_alipay_trade(order_id: str, plan: dict) -> dict:
    biz_content = {
        "out_trade_no": order_id,
        "total_amount": f"{plan['price']:.2f}",
        "subject": plan["name"],
        "product_code": "FACE_TO_FACE_PAYMENT",
        "timeout_express": "15m",
    }
    data = await _alipay_api_call("alipay.trade.precreate", biz_content)
    return data.get("alipay_trade_precreate_response", {})

def _alipay_qr_display_url(qr_code: str | None) -> str | None:
    if not qr_code:
        return None
    qr_code = qr_code.strip()
    if re.match(r"^https://mobilecodec\.alipay\.com/show\.htm\?code=", qr_code, re.I):
        return qr_code
    if re.match(r"^https://qr\.alipay\.com/", qr_code, re.I):
        code = re.sub(r"^https://qr\.alipay\.com/", "", qr_code, flags=re.I).rstrip("/")
        return f"https://mobilecodec.alipay.com/show.htm?code={code}"
    return qr_code if qr_code.startswith("https://") else None

async def _build_order_payment_payload(order_id: str, plan: dict, mobile: bool) -> dict:
    if _alipay_enabled():
        payload = {
            "order_id": order_id,
            "amount": plan["price"],
            "name": plan["name"],
            "pay_url": "",
            "embed_pay_url": None,
            "pay_method": "precreate",
            "provider": "alipay",
            "return_url": _payment_return_url(order_id),
            "qr_code": None,
            "qr_image_url": None,
            "display_mode": "qr",
            "manual_review": False,
            "manual_label": "",
        }
        try:
            precreate = await _precreate_alipay_trade(order_id, plan)
            code = precreate.get("code")
            qr_code = precreate.get("qr_code") or precreate.get("qr_code_url")
            if code == "10000" and qr_code:
                pay_url = _alipay_qr_display_url(qr_code) or ""
                payload.update({
                    "pay_url": pay_url,
                    "qr_code": qr_code,
                    "qr_image_url": pay_url or None,
                })
                return payload
            reason = precreate.get("sub_msg") or precreate.get("msg") or precreate.get("sub_code") or code or "unknown"
            print(f"[alipay] precreate rejected order={order_id} reason={reason}", flush=True)
        except Exception as exc:
            print(f"[alipay] precreate exception order={order_id} err={type(exc).__name__}", flush=True)
        if _manual_payment_enabled():
            return _manual_payment_payload(order_id, plan)
        raise HTTPException(502, "支付宝当面付创建失败，请先检查应用签约状态或切换人工审核收款")
    if _manual_payment_enabled():
        return _manual_payment_payload(order_id, plan)
    return _mock_payment_payload(order_id, plan)

def _build_alipay_form(order_id: str, plan: dict, mobile: bool, embed_qr: bool = False) -> str:
    method = "alipay.trade.wap.pay" if mobile else "alipay.trade.page.pay"
    biz_content = {
        "out_trade_no": order_id,
        "total_amount": f"{plan['price']:.2f}",
        "subject": plan["name"],
        "product_code": "QUICK_WAP_WAY" if mobile else "FAST_INSTANT_TRADE_PAY",
        "timeout_express": "15m",
    }
    if not mobile and embed_qr:
        biz_content["qr_pay_mode"] = "4"
        biz_content["qrcode_width"] = 280
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": method,
        "charset": "UTF-8",
        "sign_type": "RSA2",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": ALIPAY_NOTIFY_URL,
        "return_url": _payment_return_url(order_id),
        "biz_content": json.dumps(biz_content, ensure_ascii=True, separators=(",", ":")),
    }
    params["sign"] = _alipay_sign(params)
    inputs = "".join(
        f'<input type="hidden" name="{escape(str(k))}" value="{escape(str(v))}">'
        for k, v in params.items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>跳转支付宝支付</title>
</head>
<body>
  <form id="alipay-form" action="{escape(ALIPAY_GATEWAY)}" method="post" accept-charset="UTF-8">
    {inputs}
  </form>
  <script>document.getElementById('alipay-form').submit();</script>
  <noscript>
    <p>请点击继续跳转支付宝支付。</p>
    <button type="submit" form="alipay-form">继续支付</button>
  </noscript>
</body>
</html>"""

async def _sync_alipay_order(order_id: str) -> tuple[bool, str | None]:
    if not _alipay_enabled():
        return False, None
    result = await _query_alipay_trade(order_id)
    trade_status = result.get("trade_status", "")
    if trade_status in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        _mark_order_paid(order_id, result.get("total_amount"), result.get("trade_no"))
        return True, result.get("trade_no")
    return False, result.get("trade_no")

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
            raise HTTPException(401, "未登录")
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        sub = payload.get("sub")
        if sub is None:
            raise HTTPException(401, "Token 无效")
        return int(sub)
    except Exception:
        raise HTTPException(401, "Token 无效")

def _require_user_row(db, uid):
    row = db_execute(db, "SELECT id,phone,credits,plan FROM users WHERE id=%s", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "账号不存在，请重新登录")
    return row

# ─── AUTH ─────────────────────────────────────────────
class AuthReq(BaseModel):
    phone: str; password: str

@app.post("/api/auth/register")
def register(r: AuthReq, response: Response):
    hashed = bcrypt.hashpw(r.password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    initial_credits = TEST_ACCOUNT_MIN_CREDITS if r.phone == TEST_ACCOUNT_PHONE else 1
    try:
        db_execute(db, "INSERT INTO users(phone,password,credits,plan) VALUES(%s,%s,%s,%s)", (r.phone, hashed, initial_credits, "free"))
        db.commit()
        uid = db_execute(db, "SELECT id FROM users WHERE phone=%s", (r.phone,)).fetchone()[0]
        token = _build_auth_token(uid)
        _set_auth_cookie(response, token)
        return {"token": token, "credits": initial_credits, "plan": "free"}
    except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(400, "手机号已注册")

@app.post("/api/auth/login")
def login(r: AuthReq, response: Response):
    db = get_db()
    row = db_execute(db, "SELECT id,password,credits,plan FROM users WHERE phone=%s", (r.phone,)).fetchone()
    if not row or not bcrypt.checkpw(r.password.encode(), row[1].encode()):
        raise HTTPException(401, "手机号或密码错误")
    credits = _ensure_test_account_credits(db, r.phone)
    token = _build_auth_token(row[0])
    _set_auth_cookie(response, token)
    return {"token": token, "credits": credits if credits is not None else row[2], "plan": row[3]}

@app.post("/api/auth/logout")
def logout(response: Response):
    _clear_auth_cookie(response)
    return {"ok": True}

@app.get("/api/auth/me")
def me(uid=Depends(current_uid)):
    db = get_db()
    row = _require_user_row(db, uid)
    credits = _ensure_test_account_credits(db, row[1])
    return {"phone": row[1], "credits": credits if credits is not None else row[2], "plan": row[3]}

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

def _room_prompt(style_name: str, style_detail: str) -> str:
    return (
        f"这是一个室内图像编辑任务，请基于上传房间照片直接生成{style_name}装修效果图。"
        "把上传图当作底图而不是灵感图，必须尽最大程度保留原图的空间结构、墙体位置、门窗位置、天花板形状、地面边界、镜头机位、透视关系、房间比例和采光方向，"
        "不要重构成另一套房，不要移动窗户门洞，不要新增或删减房间，不要改变整体构图。"
        f"只允许在原空间内替换材质、墙面、地面、吊顶细节、家具、灯具、窗帘、装饰画和软装，整体风格要求：{style_detail}。"
        "输出必须像真实设计师在原图上做的装修改造图，保留原房间特征，细节自然，避免夸张结构变化。"
    )

def build_doubao_payload(input_path: str, style: str, quality: str) -> dict:
    style_name = STYLES.get(style, "现代简约")
    style_detail = STYLE_DETAILS.get(style, STYLE_DETAILS["modern"])
    size = resolve_output_size(input_path, quality)
    img_data_uri = _image_data_uri(input_path)
    control_data_uri = _control_signal_data_uri(input_path)
    if not img_data_uri:
        raise RuntimeError("未读取到用户上传的原始房间图片，无法生成参考设计图")
    payload = {
        "model": ARK_IMAGE_MODEL,
        "prompt": _room_prompt(style_name, style_detail),
        "n": 1,
        "size": size,
        "response_format": "url",
    }
    # Seedream 5.0 can take the uploaded room image directly, while the control
    # overlay reinforces the original framing and room boundaries.
    if "seedream-5-0" in ARK_IMAGE_MODEL:
        payload["image"] = img_data_uri
        if control_data_uri:
            payload["reference_images"] = [control_data_uri]
        return payload

    payload["reference_images"] = [img_data_uri, control_data_uri] if control_data_uri else [img_data_uri]
    return payload

async def call_doubao(input_path: str, style: str, quality: str) -> str:
    payload = build_doubao_payload(input_path, style, quality)
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
        raise HTTPException(402, "点数不足，请先充值")
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
PLANS = {"c10":{"price":30,"credits":10,"name":"10次点数包"},
         "c50":{"price":99,"credits":50,"name":"月度会员"},
         "c200":{"price":269,"credits":200,"name":"季度会员"},
         "c500":{"price":999,"credits":500,"name":"企业会员"}}

class OrderReq(BaseModel):
    plan_id: str

class ManualReviewApproveReq(BaseModel):
    order_id: str

class ManualPaymentConfigReq(BaseModel):
    qr_url: str = ""
    label: str = "支付宝扫码转账"

@app.post("/api/payment/create")
async def create_order(r: OrderReq, request: Request, uid=Depends(current_uid)):
    plan = PLANS.get(r.plan_id)
    if not plan:
        raise HTTPException(400, "无效套餐")
    _require_user_row(get_db(), uid)
    oid = f"LKJ{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
    db = get_db()
    db_execute(db, "INSERT INTO orders (id, user_id, plan_id, amount, credits, status, created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
               (oid, uid, r.plan_id, plan["price"], plan["credits"], "pending", datetime.utcnow().isoformat()))
    db.commit()
    return await _build_order_payment_payload(oid, plan, _is_mobile_request(request))

@app.get("/api/payment/status/{order_id}")
async def order_status(order_id: str, uid=Depends(current_uid)):
    db = get_db()
    _require_user_row(db, uid)
    row = db_execute(
        db,
        "SELECT status, payment_proof_url, payment_submitted_at FROM orders WHERE id=%s AND user_id=%s",
        (order_id, uid),
    ).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    status, payment_proof_url, payment_submitted_at = row
    trade_no = None
    if status != "paid" and _alipay_enabled():
        paid, trade_no = await _sync_alipay_order(order_id)
        if paid:
            status = "paid"
    user_credits = db_execute(db, "SELECT credits FROM users WHERE id=%s", (uid,)).fetchone()[0]
    return {
        "status": status,
        "credits": user_credits,
        "trade_no": trade_no,
        "payment_proof_url": payment_proof_url,
        "payment_submitted_at": payment_submitted_at,
    }

@app.get("/api/payment/order/{order_id}")
async def order_detail(order_id: str, request: Request, uid=Depends(current_uid)):
    db = get_db()
    _require_user_row(db, uid)
    row = db_execute(
        db,
        "SELECT plan_id, status, payment_proof_url, payment_submitted_at FROM orders WHERE id=%s AND user_id=%s",
        (order_id, uid),
    ).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    plan_id, status, payment_proof_url, payment_submitted_at = row
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(400, "无效套餐")
    paid_trade_no = None
    if status != "paid" and _alipay_enabled():
        paid, paid_trade_no = await _sync_alipay_order(order_id)
        if paid:
            status = "paid"
    payload = await _build_order_payment_payload(order_id, plan, _is_mobile_request(request))
    payload.update({
        "status": status,
        "payment_proof_url": payment_proof_url,
        "payment_submitted_at": payment_submitted_at,
        "trade_no": paid_trade_no,
    })
    return payload

@app.get("/api/payment/checkout/{order_id}", response_class=HTMLResponse)
def alipay_checkout(order_id: str, request: Request):
    db = get_db()
    row = db_execute(db, "SELECT plan_id, status FROM orders WHERE id=%s", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    if row[1] == "paid":
        return HTMLResponse(
            f"<script>window.location.href='{escape(_payment_return_url(order_id))}&status=paid';</script>",
            status_code=200,
        )
    if not _alipay_enabled():
        raise HTTPException(500, "支付宝支付参数未配置")
    plan = PLANS.get(row[0])
    if not plan:
        raise HTTPException(400, "无效套餐")
    mobile = "mobile" in (request.headers.get("user-agent", "").lower())
    embed_qr = request.query_params.get("embed") == "1"
    return HTMLResponse(_build_alipay_form(order_id, plan, mobile, embed_qr=embed_qr))

@app.get("/api/payment/mock/{order_id}", response_class=HTMLResponse)
def mock_checkout(order_id: str):
    _require_mock_payment_allowed()
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>模拟支付</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f8fafc;padding:40px;color:#0f172a}}
    .card{{max-width:420px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px;text-align:center}}
    .btn{{display:inline-block;background:#2563eb;color:#fff;padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:700}}
  </style>
</head>
<body>
  <div class="card">
    <h2>开发模式模拟支付</h2>
    <p>当前未配置支付宝正式参数，点击下面按钮模拟支付成功。</p>
    <a class="btn" href="/api/payment/mock-success/{escape(order_id)}">模拟支付成功</a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)

@app.get("/api/payment/mock-success/{order_id}", response_class=HTMLResponse)
def mock_checkout_success(order_id: str):
    _require_mock_payment_allowed()
    _mark_order_paid(order_id)
    return HTMLResponse(
        f"<script>window.location.href='{escape(_payment_return_url(order_id))}&status=paid';</script>",
        status_code=200,
    )

@app.post("/api/payment/callback/alipay")
async def alipay_cb(request: Request):
    form = dict(await request.form())
    if not _alipay_enabled():
        return PlainTextResponse("fail")
    if not _alipay_verify(form):
        return PlainTextResponse("fail")
    oid = form.get("out_trade_no", "")
    trade_status = form.get("trade_status", "")
    if trade_status in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        try:
            _mark_order_paid(oid, form.get("total_amount"), form.get("trade_no"))
        except Exception:
            return PlainTextResponse("fail")
    return PlainTextResponse("success")

@app.post("/api/payment/proof/{order_id}")
async def upload_payment_proof(
    order_id: str,
    file: UploadFile = File(...),
    note: str = "",
    uid=Depends(current_uid),
):
    db = get_db()
    row = db_execute(db, "SELECT status FROM orders WHERE id=%s AND user_id=%s", (order_id, uid)).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    if row[0] == "paid":
        raise HTTPException(400, "该订单已支付完成")
    filename = f"{order_id}_{uuid.uuid4().hex}{Path(file.filename or '').suffix.lower() or '.jpg'}"
    target = PAYMENT_PROOF_DIR / filename
    content = await file.read()
    if not content:
        raise HTTPException(400, "付款截图不能为空")
    target.write_bytes(content)
    proof_url = f"/uploads/payment-proofs/{filename}"
    db_execute(
        db,
        "UPDATE orders SET status=%s, payment_proof_url=%s, payment_note=%s, payment_submitted_at=%s WHERE id=%s AND user_id=%s",
        ("awaiting_review", proof_url, note[:200], datetime.utcnow().isoformat(), order_id, uid),
    )
    db.commit()
    return {"ok": True, "status": "awaiting_review", "payment_proof_url": proof_url}

@app.get("/api/payment/review-list")
def payment_review_list(uid=Depends(current_uid)):
    db = get_db()
    user = _require_user_row(db, uid)
    if user[1] != TEST_ACCOUNT_PHONE:
        raise HTTPException(403, "无权限")
    rows = db_execute(
        db,
        """
        SELECT o.id, u.phone, o.plan_id, o.amount, o.credits, o.status, o.payment_proof_url, o.payment_note, o.payment_submitted_at
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        WHERE o.status='awaiting_review'
        ORDER BY o.payment_submitted_at DESC, o.created_at DESC
        LIMIT 100
        """,
    ).fetchall()
    return [
        {
            "order_id": r[0],
            "phone": r[1],
            "plan_id": r[2],
            "amount": r[3],
            "credits": r[4],
            "status": r[5],
            "payment_proof_url": r[6],
            "payment_note": r[7],
            "payment_submitted_at": r[8],
        }
        for r in rows
    ]

@app.get("/api/payment/manual-config")
def get_manual_payment_config(uid=Depends(current_uid)):
    db = get_db()
    user = _require_user_row(db, uid)
    if user[1] != TEST_ACCOUNT_PHONE:
        raise HTTPException(403, "无权限")
    return _manual_payment_config()

@app.post("/api/payment/manual-config")
def set_manual_payment_config(r: ManualPaymentConfigReq, uid=Depends(current_uid)):
    db = get_db()
    user = _require_user_row(db, uid)
    if user[1] != TEST_ACCOUNT_PHONE:
        raise HTTPException(403, "无权限")
    qr_url = (r.qr_url or "").strip()
    label = (r.label or "支付宝扫码转账").strip() or "支付宝扫码转账"
    _set_setting("manual_payment_config", json.dumps({"qr_url": qr_url, "label": label}, ensure_ascii=False))
    return {"ok": True, "qr_url": qr_url, "label": label}

@app.post("/api/payment/review/approve")
def approve_payment_review(r: ManualReviewApproveReq, uid=Depends(current_uid)):
    db = get_db()
    user = _require_user_row(db, uid)
    if user[1] != TEST_ACCOUNT_PHONE:
        raise HTTPException(403, "无权限")
    row = db_execute(db, "SELECT status FROM orders WHERE id=%s", (r.order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    if row[0] == "paid":
        return {"ok": True, "status": "paid"}
    if row[0] != "awaiting_review":
        raise HTTPException(400, "当前订单未提交审核")
    _mark_order_paid(r.order_id)
    return {"ok": True, "status": "paid"}
