import os, uuid, sqlite3, base64, asyncio, time, json
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO
from html import escape

import httpx, bcrypt, jwt
from PIL import Image
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, BackgroundTasks, Request
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

ARK_API_KEY  = os.getenv("ARK_API_KEY", "")
JWT_SECRET   = os.getenv("JWT_SECRET", "dev-secret")
ARK_IMAGE_MODEL = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128")
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "")
ALIPAY_PRIVATE_KEY = os.getenv("ALIPAY_PRIVATE_KEY", "")
ALIPAY_PUBLIC_KEY = os.getenv("ALIPAY_PUBLIC_KEY", "")
ALIPAY_NOTIFY_URL = os.getenv("ALIPAY_NOTIFY_URL", "")
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "")
NEXT_PUBLIC_BASE_URL = os.getenv("NEXT_PUBLIC_BASE_URL", "")
DOMAIN = os.getenv("DOMAIN", "")
ALIPAY_GATEWAY = os.getenv("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do")
UPLOAD_DIR   = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR     = Path("data"); DATA_DIR.mkdir(exist_ok=True)
DB_PATH      = DATA_DIR / "app.db"
ROOT_DIR = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT_DIR / "index.html"

app = FastAPI(title="灵感空间AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

security = HTTPBearer()

# ─── DB ───────────────────────────────────────────────
def get_db():
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
    conn.commit()
    return conn

@app.get("/", response_class=HTMLResponse)
def home():
    if INDEX_HTML.exists():
        resp = FileResponse(INDEX_HTML)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return HTMLResponse("<h1>灵感空间AI</h1><p>Frontend not found.</p>", status_code=200)

def _site_base_url() -> str:
    for value in (PUBLIC_SITE_URL, NEXT_PUBLIC_BASE_URL):
        if value:
            return value.rstrip("/")
    if DOMAIN:
        return DOMAIN.rstrip("/") if DOMAIN.startswith("http") else f"https://{DOMAIN.strip('/')}"
    return "http://127.0.0.1:8000"

def _alipay_enabled() -> bool:
    return all([ALIPAY_APP_ID, ALIPAY_PRIVATE_KEY, ALIPAY_PUBLIC_KEY, ALIPAY_NOTIFY_URL])

def _normalize_pem(raw: str, kind: str) -> str:
    value = (raw or "").strip().strip('"').strip("'").replace("\\n", "\n")
    if "BEGIN" in value:
        return value
    header = "PRIVATE KEY" if kind == "private" else "PUBLIC KEY"
    return f"-----BEGIN {header}-----\n{value}\n-----END {header}-----"

def _load_alipay_private_key():
    return serialization.load_pem_private_key(
        _normalize_pem(ALIPAY_PRIVATE_KEY, "private").encode(),
        password=None,
    )

def _load_alipay_public_key():
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

def _mark_order_paid(order_id: str, total_amount: str | None = None, trade_no: str | None = None) -> bool:
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT user_id, credits, status, amount FROM orders WHERE id=?", (order_id,)).fetchone()
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
        db.execute("UPDATE orders SET status='paid' WHERE id=? AND status<>'paid'", (order_id,))
        db.execute("UPDATE users SET credits=credits+? WHERE id=?", (credits, user_id))
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

async def _query_alipay_trade(order_id: str) -> dict:
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": "alipay.trade.query",
        "charset": "UTF-8",
        "sign_type": "RSA2",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps({"out_trade_no": order_id}, ensure_ascii=False, separators=(",", ":")),
    }
    params["sign"] = _alipay_sign(params)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ALIPAY_GATEWAY, data=params)
    data = _parse_alipay_json(resp.content)
    return data.get("alipay_trade_query_response", {})

def _build_alipay_form(order_id: str, plan: dict, mobile: bool) -> str:
    method = "alipay.trade.wap.pay" if mobile else "alipay.trade.page.pay"
    biz_content = {
        "out_trade_no": order_id,
        "total_amount": f"{plan['price']:.2f}",
        "subject": plan["name"],
        "body": plan["name"],
        "product_code": "QUICK_WAP_WAY" if mobile else "FAST_INSTANT_TRADE_PAY",
    }
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": method,
        "charset": "UTF-8",
        "sign_type": "RSA2",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": ALIPAY_NOTIFY_URL,
        "return_url": _payment_return_url(order_id),
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
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

def current_uid(cred: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return jwt.decode(cred.credentials, JWT_SECRET, algorithms=["HS256"])["sub"]
    except Exception:
        raise HTTPException(401, "Token 无效")

# ─── AUTH ─────────────────────────────────────────────
class AuthReq(BaseModel):
    phone: str; password: str

@app.post("/api/auth/register")
def register(r: AuthReq):
    hashed = bcrypt.hashpw(r.password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        db.execute("INSERT INTO users(phone,password,credits,plan) VALUES(?,?,?,?)", (r.phone, hashed, 1, "free"))
        db.commit()
        uid = db.execute("SELECT id FROM users WHERE phone=?", (r.phone,)).fetchone()[0]
        token = jwt.encode({"sub": uid, "exp": datetime.utcnow()+timedelta(days=30)}, JWT_SECRET)
        return {"token": token, "credits": 1, "plan": "free"}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "手机号已注册")

@app.post("/api/auth/login")
def login(r: AuthReq):
    db = get_db()
    row = db.execute("SELECT id,password,credits,plan FROM users WHERE phone=?", (r.phone,)).fetchone()
    if not row or not bcrypt.checkpw(r.password.encode(), row[1].encode()):
        raise HTTPException(401, "手机号或密码错误")
    token = jwt.encode({"sub": row[0], "exp": datetime.utcnow()+timedelta(days=30)}, JWT_SECRET)
    return {"token": token, "credits": row[2], "plan": row[3]}

@app.get("/api/auth/me")
def me(uid=Depends(current_uid)):
    row = get_db().execute("SELECT phone,credits,plan FROM users WHERE id=?", (uid,)).fetchone()
    return {"phone": row[0], "credits": row[1], "plan": row[2]}

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
    # Seedream 5.0 supports direct image-to-image input; use it preferentially.
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
        return r2.json()["data"][0]["url"]

async def process_job(job_id: str, input_path: str, style: str, quality: str):
    db = get_db()
    try:
        db.execute("UPDATE jobs SET status='processing' WHERE id=?", (job_id,)); db.commit()
        url = await call_doubao(input_path, style, quality)
        db.execute("UPDATE jobs SET status='done',output_url=? WHERE id=?", (url, job_id))
    except Exception as e:
        db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (str(e), job_id))
    finally:
        db.commit()

@app.post("/api/generate/upload")
async def upload(file: UploadFile = File(...), uid=Depends(current_uid)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(400, "仅支持图片")
    ext = file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "jpg"
    path = UPLOAD_DIR / f"{uuid.uuid4()}.{ext}"
    path.write_bytes(await file.read())
    return {"file_id": path.name}

@app.post("/api/generate/{file_id}")
async def generate(file_id: str, req: GenReq, bg: BackgroundTasks, uid=Depends(current_uid)):
    db = get_db()
    row = db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row[0] < 1:
        raise HTTPException(402, "点数不足，请先充值")
    input_path = str(UPLOAD_DIR / file_id)
    if not Path(input_path).exists():
        raise HTTPException(404, "图片不存在")
    db.execute("UPDATE users SET credits=credits-1 WHERE id=?", (uid,)); db.commit()
    job_id = str(uuid.uuid4())
    db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
               (job_id, uid, req.style, input_path, None, "pending", None, datetime.utcnow().isoformat()))
    db.commit()
    bg.add_task(process_job, job_id, input_path, req.style, req.quality)
    return {"job_id": job_id, "status": "processing"}

@app.get("/api/generate/status/{job_id}")
def status(job_id: str, uid=Depends(current_uid)):
    row = get_db().execute("SELECT id,status,output_url,error FROM jobs WHERE id=? AND user_id=?",
                           (job_id, uid)).fetchone()
    if not row: raise HTTPException(404, "任务不存在")
    return {"job_id": row[0], "status": row[1], "output_url": row[2], "error": row[3]}

@app.get("/api/generate/history")
def history(uid=Depends(current_uid)):
    rows = get_db().execute("SELECT id,style,output_url,status,created_at FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (uid,)).fetchall()
    return [{"id":r[0],"style":r[1],"url":r[2],"status":r[3],"created_at":r[4]} for r in rows]

# ─── PAYMENT ──────────────────────────────────────────
PLANS = {"c10":{"price":30,"credits":10,"name":"10次点数包"},
         "c50":{"price":99,"credits":50,"name":"月度会员"},
         "c200":{"price":269,"credits":200,"name":"季度套餐"}}

class OrderReq(BaseModel):
    plan_id: str

@app.post("/api/payment/create")
async def create_order(r: OrderReq, request: Request, uid=Depends(current_uid)):
    plan = PLANS.get(r.plan_id)
    if not plan: raise HTTPException(400, "无效套餐")
    oid = f"LKJ{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
    db = get_db()
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
               (oid, uid, r.plan_id, plan["price"], plan["credits"], "pending", datetime.utcnow().isoformat()))
    db.commit()
    if _alipay_enabled():
        mobile = "mobile" in (request.headers.get("user-agent", "").lower())
        pay_url = f"{_site_base_url()}/api/payment/checkout/{oid}"
        return {
            "order_id": oid,
            "amount": plan["price"],
            "name": plan["name"],
            "pay_url": pay_url,
            "pay_method": "wap" if mobile else "page",
            "provider": "alipay",
            "return_url": _payment_return_url(oid),
        }
    return {
        "order_id": oid,
        "amount": plan["price"],
        "name": plan["name"],
        "pay_url": f"{_site_base_url()}/api/payment/mock/{oid}",
        "pay_method": "mock",
        "provider": "mock",
    }

@app.get("/api/payment/status/{order_id}")
async def order_status(order_id: str, uid=Depends(current_uid)):
    db = get_db()
    row = db.execute("SELECT status FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
    if not row: raise HTTPException(404, "订单不存在")
    status = row[0]
    trade_no = None
    if status != "paid" and _alipay_enabled():
        paid, trade_no = await _sync_alipay_order(order_id)
        if paid:
            status = "paid"
    user_credits = db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()[0]
    return {"status": status, "credits": user_credits, "trade_no": trade_no}

@app.get("/api/payment/checkout/{order_id}", response_class=HTMLResponse)
def alipay_checkout(order_id: str, request: Request):
    db = get_db()
    row = db.execute("SELECT plan_id, status FROM orders WHERE id=?", (order_id,)).fetchone()
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
    return HTMLResponse(_build_alipay_form(order_id, plan, mobile))

@app.get("/api/payment/mock/{order_id}", response_class=HTMLResponse)
def mock_checkout(order_id: str):
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
