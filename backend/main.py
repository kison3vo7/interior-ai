import os, uuid, sqlite3, base64, asyncio, time, json
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

import httpx, bcrypt, jwt
from PIL import Image
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

ARK_API_KEY  = os.getenv("ARK_API_KEY", "")
JWT_SECRET   = os.getenv("JWT_SECRET", "dev-secret")
ARK_IMAGE_MODEL = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128")
WECHAT_APPID = os.getenv("WECHAT_APPID", "")
WECHAT_MCHID = os.getenv("WECHAT_MCHID", "")
WECHAT_PRIVATE_KEY_PATH = os.getenv("WECHAT_PRIVATE_KEY_PATH", "")
WECHAT_CERT_SERIAL_NO = os.getenv("WECHAT_CERT_SERIAL_NO", "")
WECHAT_NOTIFY_URL = os.getenv("WECHAT_NOTIFY_URL", "")
ALIPAY_APPID = os.getenv("ALIPAY_APPID", "")
ALIPAY_PRIVATE_KEY_PATH = os.getenv("ALIPAY_PRIVATE_KEY_PATH", "")
ALIPAY_PUBLIC_KEY_PATH = os.getenv("ALIPAY_PUBLIC_KEY_PATH", "")
ALIPAY_PRIVATE_KEY = os.getenv("ALIPAY_PRIVATE_KEY", "")  # 密钥内容（优先于文件）
ALIPAY_PUBLIC_KEY = os.getenv("ALIPAY_PUBLIC_KEY", "")    # 公钥内容（优先于文件）
ALIPAY_PRIVATE_KEY_B64 = os.getenv("ALIPAY_PRIVATE_KEY_B64", "")  # base64编码（Render推荐用这个）
ALIPAY_PUBLIC_KEY_B64 = os.getenv("ALIPAY_PUBLIC_KEY_B64", "")    # base64编码（Render推荐用这个）
ALIPAY_NOTIFY_URL = os.getenv("ALIPAY_NOTIFY_URL", "")
DOMAIN = os.getenv("DOMAIN", "")
UPLOAD_DIR   = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR     = Path("data"); DATA_DIR.mkdir(exist_ok=True)
DB_PATH      = DATA_DIR / "app.db"

app = FastAPI(title="灵空间AI")
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

def _alipay_enabled() -> bool:
    return ALIPAY_APPID and (_alipay_priv_bytes() is not None) and (_alipay_pub_bytes() is not None)

def _resolve_path(filepath: str) -> Path:
    p = Path(filepath)
    if p.is_absolute():
        return p
    return (Path(__file__).resolve().parent / p).resolve()

def _alipay_priv_bytes() -> bytes | None:
    if ALIPAY_PRIVATE_KEY_B64:
        try:
            return base64.b64decode(ALIPAY_PRIVATE_KEY_B64)
        except Exception:
            pass
    if ALIPAY_PRIVATE_KEY:
        return ALIPAY_PRIVATE_KEY.replace("\\n", "\n").encode()
    if ALIPAY_PRIVATE_KEY_PATH:
        return _resolve_path(ALIPAY_PRIVATE_KEY_PATH).read_bytes()
    return None

def _alipay_pub_bytes() -> bytes | None:
    if ALIPAY_PUBLIC_KEY_B64:
        try:
            return base64.b64decode(ALIPAY_PUBLIC_KEY_B64)
        except Exception:
            pass
    if ALIPAY_PUBLIC_KEY:
        return ALIPAY_PUBLIC_KEY.replace("\\n", "\n").encode()
    if ALIPAY_PUBLIC_KEY_PATH:
        return _resolve_path(ALIPAY_PUBLIC_KEY_PATH).read_bytes()
    return None

def _alipay_sign(params: dict) -> str:
    raw = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v and k not in ("sign", "sign_type"))
    priv = serialization.load_pem_private_key(_alipay_priv_bytes(), password=None)
    return base64.b64encode(priv.sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()

async def create_alipay_order(order_id: str, plan: dict) -> dict:
    biz = json.dumps({
        "out_trade_no": order_id,
        "total_amount": f"{plan['price']:.2f}",
        "subject": plan["name"],
    }, ensure_ascii=False)
    params = {
        "app_id": ALIPAY_APPID, "method": "alipay.trade.precreate",
        "charset": "utf-8", "sign_type": "RSA2",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": ALIPAY_NOTIFY_URL or None,
        "biz_content": biz,
    }
    params = {k: v for k, v in params.items() if v is not None}
    params["sign"] = _alipay_sign(params)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://openapi.alipay.com/gateway.do", data=params)
    if not resp.is_success:
        raise RuntimeError(f"支付宝预下单失败: {resp.text}")
    data = resp.json()
    if data.get("alipay_trade_precreate_response", {}).get("code") != "10000":
        raise RuntimeError(f"支付宝预下单失败: {data}")
    qr_code = data["alipay_trade_precreate_response"]["qr_code"]
    return {"order_id": order_id, "amount": plan["price"], "name": plan["name"],
            "pay_url": qr_code, "provider": "alipay"}

def _wechat_enabled() -> bool:
    return all([WECHAT_APPID, WECHAT_MCHID, WECHAT_PRIVATE_KEY_PATH, WECHAT_CERT_SERIAL_NO, WECHAT_NOTIFY_URL])

def _load_wechat_private_key():
    key_path = Path(WECHAT_PRIVATE_KEY_PATH)
    if not key_path.is_absolute():
        key_path = Path(__file__).resolve().parent / key_path
    if not key_path.exists():
        raise RuntimeError(f"微信私钥文件不存在: {key_path}")
    return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

def _build_wechat_signature(method: str, canonical_url: str, body: str) -> tuple[str, str, str]:
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    message = f"{method}\n{canonical_url}\n{timestamp}\n{nonce}\n{body}\n"
    signature = _load_wechat_private_key().sign(
        message.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return timestamp, nonce, base64.b64encode(signature).decode()

async def create_wechat_native_order(order_id: str, plan: dict) -> dict:
    canonical_url = "/v3/pay/transactions/native"
    body_dict = {
        "appid": WECHAT_APPID,
        "mchid": WECHAT_MCHID,
        "description": plan["name"],
        "out_trade_no": order_id,
        "notify_url": WECHAT_NOTIFY_URL,
        "amount": {
            "total": plan["price"] * 100,
            "currency": "CNY",
        },
    }
    body = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":"))
    timestamp, nonce, signature = _build_wechat_signature("POST", canonical_url, body)
    authorization = (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{WECHAT_MCHID}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{WECHAT_CERT_SERIAL_NO}"'
    )
    headers = {
        "Authorization": authorization,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ling-space-ai/1.0",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"https://api.mch.weixin.qq.com{canonical_url}", headers=headers, content=body.encode())
    if not resp.is_success:
        raise RuntimeError(f"微信支付下单失败: {resp.text}")
    data = resp.json()
    return {
        "order_id": order_id,
        "amount": plan["price"],
        "name": plan["name"],
        "pay_url": data.get("code_url", ""),
        "provider": "wechat",
    }

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
        db.execute("INSERT INTO users(phone,password,credits,plan) VALUES(?,?,?,?)", (r.phone, hashed, 10, "free"))
        db.commit()
        uid = db.execute("SELECT id FROM users WHERE phone=?", (r.phone,)).fetchone()[0]
        token = jwt.encode({"sub": uid, "exp": datetime.utcnow()+timedelta(days=30)}, JWT_SECRET)
        return {"token": token, "credits": 10, "plan": "free"}
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
async def create_order(r: OrderReq, uid=Depends(current_uid)):
    plan = PLANS.get(r.plan_id)
    if not plan: raise HTTPException(400, "无效套餐")
    oid = f"LKJ{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
    db = get_db()
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
               (oid, uid, r.plan_id, plan["price"], plan["credits"], "pending", datetime.utcnow().isoformat()))
    db.commit()
    if _alipay_enabled():
        try:
            return await create_alipay_order(oid, plan)
        except Exception as e:
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            db.commit()
            raise HTTPException(500, str(e))
    if _wechat_enabled():
        try:
            return await create_wechat_native_order(oid, plan)
        except Exception as e:
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            db.commit()
            raise HTTPException(500, str(e))
    return {
        "order_id": oid,
        "amount": plan["price"],
        "name": plan["name"],
        "pay_url": f"https://pay.weixin.qq.com/mock/{oid}",
        "provider": "mock",
    }

@app.get("/api/payment/status/{order_id}")
def order_status(order_id: str, uid=Depends(current_uid)):
    row = get_db().execute("SELECT status FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
    if not row: raise HTTPException(404, "订单不存在")
    user_credits = get_db().execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()[0]
    return {"status": row[0], "credits": user_credits}

@app.post("/api/payment/callback/wechat")
async def wechat_cb(data: dict):
    oid = data.get("out_trade_no", "")
    db = get_db()
    row = db.execute("SELECT user_id,credits,status FROM orders WHERE id=?", (oid,)).fetchone()
    if not row or row[2] == "paid": return {"code": "SUCCESS"}
    db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    db.execute("UPDATE users SET credits=credits+? WHERE id=?", (row[1], row[0]))
    db.commit()
    return {"code": "SUCCESS"}

@app.post("/api/payment/callback/alipay")
async def alipay_cb(request: Request):
    """支付宝异步通知 — 验签后充值"""
    body = await request.body()
    params = dict(request.query_params)
    for pair in body.decode().split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = __import__("urllib.parse").parse.unquote(v)

    sign = params.pop("sign", "")
    sign_type = params.pop("sign_type", "RSA2")
    raw = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v and k != "sign")

    try:
        pub_key = serialization.load_pem_public_key(_alipay_pub_bytes())
        pub_key.verify(base64.b64decode(sign), raw.encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise HTTPException(400, "支付宝签名验证失败")

    trade_status = params.get("trade_status", "")
    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return "success"

    oid = params.get("out_trade_no", "")
    db = get_db()
    row = db.execute("SELECT user_id,credits,status FROM orders WHERE id=?", (oid,)).fetchone()
    if not row or row[2] == "paid":
        return "success"
    db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    db.execute("UPDATE users SET credits=credits+? WHERE id=?", (row[1], row[0]))
    db.commit()
    return "success"
