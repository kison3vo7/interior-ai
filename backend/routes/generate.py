from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from routes.auth import current_user, db
from services.sd_service import run_sd
import uuid, os
from datetime import datetime

router = APIRouter()
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

STYLES = {
    "modern":      "modern minimalist interior design, clean lines, white walls, wooden floor, bright natural light, photorealistic",
    "nordic":      "scandinavian nordic interior, light wood, cozy textures, plants, warm lighting, photorealistic",
    "chinese":     "new chinese interior style, dark rosewood furniture, ink landscape painting, oriental elegance, photorealistic",
    "luxury":      "light luxury interior, marble floor, gold metal accents, velvet sofa, crystal chandelier, photorealistic",
    "industrial":  "industrial loft interior, exposed brick wall, black metal pipes, concrete ceiling, Edison bulbs, photorealistic",
    "american":    "american farmhouse interior, shiplap walls, vintage decor, wooden beams, warm earthy tones, photorealistic",
}

NEG_PROMPT = "ugly, blurry, distorted, unrealistic, low quality, cartoon, sketch, people, person"

class GenReq(BaseModel):
    style: str
    quality: str = "hd"  # standard | hd | 4k

def _db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, user_id INTEGER, style TEXT,
        input_path TEXT, output_url TEXT,
        status TEXT DEFAULT 'pending',
        error TEXT, created_at TEXT)""")
    conn.commit()
    return conn

async def _process_job(job_id: str, input_path: str, style: str, quality: str):
    conn = _db()
    try:
        conn.execute("UPDATE jobs SET status='processing' WHERE id=?", (job_id,))
        conn.commit()
        output_url = await run_sd(input_path, style, quality)
        conn.execute("UPDATE jobs SET status='done', output_url=? WHERE id=?", (output_url, job_id))
    except Exception as e:
        conn.execute("UPDATE jobs SET status='failed', error=? WHERE id=?", (str(e), job_id))
    finally:
        conn.commit()

@router.post("/upload")
async def upload(file: UploadFile = File(...), uid=Depends(current_user)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(400, "仅支持图片文件")
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    filename = f"{uuid.uuid4()}.{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(await file.read())
    return {"file_id": filename}

@router.post("/{file_id}")
async def generate(file_id: str, req: GenReq, background_tasks: BackgroundTasks, uid=Depends(current_user)):
    conn = _db()
    row = conn.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row[0] < 1:
        raise HTTPException(402, "点数不足，请先充值")

    # Handle sample image
    if file_id == "sample_room.jpg":
        input_path = "sample_room.jpg"
        if not os.path.exists(input_path):
            # Create a placeholder sample
            input_path = os.path.join(UPLOAD_DIR, "sample_room.jpg")
    else:
        input_path = os.path.join(UPLOAD_DIR, file_id)

    if not os.path.exists(input_path):
        raise HTTPException(404, "图片文件不存在，请重新上传")

    conn.execute("UPDATE users SET credits=credits-1 WHERE id=?", (uid,))
    conn.commit()

    job_id = str(uuid.uuid4())
    style_prompt = STYLES.get(req.style, STYLES["modern"])

    conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                 (job_id, uid, req.style, input_path, None, "pending", None,
                  datetime.utcnow().isoformat()))
    conn.commit()

    background_tasks.add_task(_process_job, job_id, input_path, req.style, req.quality)

    return {"job_id": job_id, "status": "processing"}

@router.get("/status/{job_id}")
async def status(job_id: str, uid=Depends(current_user)):
    row = _db().execute(
        "SELECT id, status, output_url, error FROM jobs WHERE id=? AND user_id=?",
        (job_id, uid)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    return {"job_id": row[0], "status": row[1], "output_url": row[2], "error": row[3]}

@router.get("/history")
def history(uid=Depends(current_user)):
    rows = _db().execute(
        "SELECT id,style,output_url,status,created_at FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
        (uid,)).fetchall()
    return [{"id": r[0], "style": r[1], "url": r[2], "status": r[3], "created_at": r[4]} for r in rows]
