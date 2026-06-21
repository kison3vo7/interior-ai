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
ADMIN_KEY=change-this-to-a-very-long-random-admin-key
```

### 3. 启动后端
```bash
uvicorn main:app --reload --port 8000
```
开发环境文档地址：http://localhost:8000/docs

说明：

- `APP_ENV=development` 时会开启 `/docs`
- `APP_ENV=production` 时会关闭 `/docs` 和 `/redoc`

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
      当前方案: /images/generations + image-to-image 参考图
      目标效果: 尽量保留原房间结构并做风格化改造
```

## 当前图片生成链路

后端当前直接对接火山引擎 Ark：

- 接口域名：`https://ark.cn-beijing.volces.com`
- 当前使用接口：`/api/v3/images/generations`
- 当前使用模型：`doubao-seedream-5-0-260128`
- 自动回退模型：`doubao-seedream-4-5-251128`
- 鉴权方式：`Authorization: Bearer $ARK_API_KEY`

当前后端策略：

- 默认优先走 `doubao-seedream-5-0-260128`
- 如果 5.0 触发 `SetLimitExceeded`、`服务暂停`、`安全体验模式` 等限制错误
- 自动回退到 `doubao-seedream-4-5-251128`
- 不改前端调用方式，不需要手动切模型

## 核心 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/register | 邮箱注册（送2次免费，密码至少8位） |
| POST | /api/auth/login | 登录，返回JWT |
| POST | /api/generate/upload | 上传房间图片 |
| POST | /api/generate/{file_id} | 触发豆包生成，扣1点数 |
| GET  | /api/generate/status/{job_id} | 查询任务状态 |
| GET  | /api/generate/history | 历史记录 |
| POST | /api/payment/create | 创建支付订单 |
| GET  | /api/payment/order/{order_id} | 查询支付订单详情 |
| GET  | /api/payment/status/{order_id} | 查询支付状态并同步到账 |
| POST | /api/payment/callback/creem | Creem Webhook 回调充值 |

## 待接入（生产必做）
- [x] Creem Checkout 支付骨架
- [ ] 阿里云 OSS 图片存储
- [ ] Redis + Celery 异步任务队列（避免 HTTP 超时）
- [ ] Nginx 反向代理 + HTTPS

## 生产支付说明

当前支付链已切到 Creem：

- 前端点击充值后，跳转 Creem Checkout 页面
- Creem 支付完成后回跳站内成功页
- 后端通过 `checkout.completed` webhook 给订单到账并发放点数

生产环境至少需要配置：

```bash
CREEM_API_KEY=your_creem_api_key
CREEM_WEBHOOK_SECRET=your_creem_webhook_secret
CREEM_API_BASE=https://api.creem.io/v1
CREEM_PRODUCT_C10=prod_xxx_for_10_credits
CREEM_PRODUCT_C50=prod_xxx_for_50_credits
CREEM_PRODUCT_C200=prod_xxx_for_200_credits
CREEM_PRODUCT_C500=prod_xxx_for_500_credits
PUBLIC_SITE_URL=https://lingganspace.work
NEXT_PUBLIC_BASE_URL=https://lingganspace.work
DOMAIN=lingganspace.work
ADMIN_KEY=change-this-to-a-very-long-random-admin-key
APP_ENV=production
```

同时在 Creem 后台把 webhook 地址设置为：

```bash
https://lingganspace.work/api/payment/callback/creem
```

如果后面切正式自定义域名，需要同步替换：

- `PUBLIC_SITE_URL`
- `NEXT_PUBLIC_BASE_URL`
- Creem webhook 地址

## 线上部署

已提供 [render.yaml](./render.yaml) 作为 Render 部署骨架。
同时补充了 [railway.json](./railway.json) 用于 Railway Docker 部署。

注意：

- 当前目录不是 Git 仓库，不能直接由我替你发到 Render
- Render Blueprint 需要 Git 远端仓库
- Creem API Key 和 Webhook Secret 建议作为环境变量注入
- Railway 也需要同步配置相同的 Creem 与站点域名环境变量
- 生产环境必须设置强随机 `ADMIN_KEY`
- 生产环境默认关闭 `/docs`，健康检查已改为 `/`
