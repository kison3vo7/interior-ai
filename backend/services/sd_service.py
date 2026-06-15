import os, httpx, asyncio, base64

ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
MODEL = "doubao-seedream-4-0-250828"

STYLE_NAMES = {
    "modern":      "现代简约",
    "nordic":      "北欧",
    "chinese":     "新中式",
    "luxury":      "轻奢",
    "industrial":  "工业风",
    "american":    "美式乡村",
}

SIZE_MAP = {"standard": "512x512", "hd": "1024x1024", "4k": "1024x1024"}

async def run_sd(input_path: str, style: str, quality: str = "hd") -> str:
    style_name = STYLE_NAMES.get(style, "现代简约")
    prompt = (
        f"帮我把这个房间设计成{style_name}的装修风格，"
        "保留原有的空间结构和布局，更换家具、装饰品和配色方案，"
        "生成专业高清室内设计效果图，真实感强，光线自然"
    )

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": SIZE_MAP.get(quality, "1024x1024"),
        "response_format": "url",
    }

    # 将上传的房间图作为参考图传入（豆包支持 subject_reference_images）
    if input_path and os.path.exists(input_path):
        with open(input_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        payload["subject_reference_images"] = [
            {"type": "subject", "image_base64": img_b64}
        ]

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            ARK_ENDPOINT,
            headers={
                "Authorization": f"Bearer {ARK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not r.is_success:
            raise RuntimeError(f"豆包API错误 {r.status_code}: {r.text}")
        data = r.json()
        return data["data"][0]["url"]
