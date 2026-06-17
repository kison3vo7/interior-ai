# 灵感空间AI — 项目启动指南

## 目录结构
```
interior-ai/
├── index.html              # 前端（静态页面）
├── docker-compose.yml      # Docker 启动入口
├── .env.example            # 环境变量模板
└── backend/
    ├── main.py             # FastAPI 入口（实际豆包调用链）
    ├── .env                # 本地 / Docker 运行时环境变量
    ├── requirements.txt    # 依赖
    └── services/
        └── sd_service.py   # 豆包图片生成服务封装（备用实现）
```

## 快速启动

### 1. 安装依赖
```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量
```bash
cp ../.env.example .env
# 或手动编辑 backend/.env
```

至少需要配置：

```bash
ARK_API_KEY=your_ark_api_key_here
JWT_SECRET=change-this-to-a-long-random-string
```

### 3. 启动后端
```bash
uvicorn main:app --reload --port 8000
```
API 文档自动生成：http://localhost:8000/docs

### 4. 前端
直接用浏览器打开 `index.html` 即可预览完整 UI。
生产环境将前端托管到阿里云 OSS 静态网站托管。

### 5. Docker 启动
```bash
docker compose up --build
```

`docker-compose.yml` 会从 `backend/.env` 读取 `ARK_API_KEY` 和其他运行参数。

---

## 技术架构

```
前端 (index.html / Vue3)
        ↓ REST API
后端 FastAPI (Python)
  ├── SQLite → 生产换 PostgreSQL (阿里云RDS)
  ├── 本地存储 → 生产换 阿里云OSS
  └── 豆包生图调用:
      方案A: /images/edits（优先保留房间结构）
      方案B: /images/generations + reference image（回退方案）
```

## 当前图片生成链路

后端当前直接对接火山引擎 Ark：

- 接口域名：`https://ark.cn-beijing.volces.com`
- 优先接口：`/api/v3/images/edits`
- 回退接口：`/api/v3/images/generations`
- 使用模型：`doubao-seedream-4-0-250828`
- 鉴权方式：`Authorization: Bearer $ARK_API_KEY`

## 核心 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/register | 注册（送1次免费） |
| POST | /api/auth/login | 登录，返回JWT |
| POST | /api/generate/upload | 上传房间图片 |
| POST | /api/generate/{file_id} | 触发豆包生成，扣1点数 |
| GET  | /api/generate/status/{job_id} | 查询任务状态 |
| GET  | /api/generate/history | 历史记录 |
| POST | /api/payment/create | 创建支付订单 |
| GET  | /api/payment/checkout/{order_id} | 兼容旧链路的支付宝跳转页 |
| POST | /api/payment/callback/alipay | 支付宝回调充值 |

## 待接入（生产必做）
- [x] 支付宝纯扫码下单骨架（待填正式参数）
- [ ] 阿里云 OSS 图片存储
- [ ] Redis + Celery 异步任务队列（避免 HTTP 超时）
- [ ] 手机验证码登录（阿里云短信）
- [ ] Nginx 反向代理 + HTTPS

## 生产支付说明

当前后端已支持三种支付模式，优先级如下：

- 配置完整支付宝参数：优先调用 `alipay.trade.precreate`，桌面端展示支付宝二维码，手机端直接尝试拉起支付宝
- `precreate` 被支付宝侧拒绝且配置了手动收款码：展示手动二维码并进入人工审核到账流程
- 未配置支付宝参数：返回本地 `mock` 支付链接，仅开发调试使用

生产环境至少需要配置：

```bash
ALIPAY_APP_ID=2021006147626992
ALIPAY_PRIVATE_KEY=your_private_key
ALIPAY_PUBLIC_KEY=alipay_public_key
ALIPAY_NOTIFY_URL=https://interior-ai-aemn.onrender.com/api/payment/callback/alipay
DOMAIN=interior-ai-aemn.onrender.com
PUBLIC_SITE_URL=https://interior-ai-aemn.onrender.com
NEXT_PUBLIC_BASE_URL=https://interior-ai-aemn.onrender.com
```

当前线上已经验证通过的一组口径是：

```bash
PUBLIC_SITE_URL=https://interior-ai-aemn.onrender.com
ALIPAY_NOTIFY_URL=https://interior-ai-aemn.onrender.com/api/payment/callback/alipay
```

如果后面切正式自定义域名，再把这两个值一起替换成新域名，避免支付返回地址和异步回调地址不一致。

## 支付兜底

如果支付宝当面付 `precreate` 被拒绝，可以配置手动收款二维码作为兜底：

```bash
MANUAL_PAYMENT_QR_URL=https://你的支付宝收款码图片或链接
MANUAL_PAYMENT_LABEL=支付宝扫码转账
```

配置后，订单会展示收款码并进入人工审核流程，不会直接报 502。

## 线上部署

已提供 [render.yaml](./render.yaml) 作为 Render 部署骨架。

注意：

- 当前目录不是 Git 仓库，不能直接由我替你发到 Render
- Render Blueprint 需要 Git 远端仓库
- 支付宝私钥建议作为环境变量注入，或在部署平台使用 Secret 管理
