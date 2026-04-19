# ptgen-go

已重构为 **Go 单二进制服务**，内置：

- Web 前端（`/`）
- 生成 API（`/api/generate`）
- 管理后台（`/admin`）
- JWT 鉴权管理接口（`/api/admin/*`）
- SQLite 持久化（用户、历史、缓存）

> 说明：服务层迁移到 Go，但**生成引擎默认调用 `ptgen.py`**，因此输出仍是“真实 ptgen”结果而非占位文本。

## 1. 启动

```bash
go run .
```

默认监听：`0.0.0.0:53000`（公网可访问，前提是安全组/防火墙放行端口）。

可配置环境变量：

- `PTGEN_HOST`（默认 `0.0.0.0`）
- `PTGEN_PORT`（默认 `53000`）
- `PTGEN_DB`（默认 `./data/ptgen.db`）
- `PTGEN_ADMIN_USER`（默认 `admin`）
- `PTGEN_ADMIN_PASSWORD`（默认 `admin123`）
- `PTGEN_JWT_SECRET`（生产环境务必修改）
- `PTGEN_PY_CMD`（默认 `python3 ptgen.py`，用于调用真实 ptgen 引擎）

示例：

```bash
PTGEN_ADMIN_PASSWORD='StrongPass' PTGEN_JWT_SECRET='replace-me' go run .
```

如果你的 Python 不是 `python3` 命令，可这样启动：

```bash
PTGEN_PY_CMD='python ptgen.py' go run .
```

## 2. 访问地址

- Web：`http://<服务器IP>:53000/`
- Admin：`http://<服务器IP>:53000/admin`
- API：`http://<服务器IP>:53000/api/generate`

## 3. API

### 3.1 生成接口

- `GET /api/generate?input=tt0133093`
- `POST /api/generate` JSON: `{"input":"tt0133093"}`

返回示例：

```json
{
  "ok": true,
  "input": "tt0133093",
  "result": "[img]...[/img] ...",
  "duration_ms": 12
}
```

### 3.2 管理后台 JWT 接口

1) 登录：`POST /api/admin/login`

```json
{ "username": "admin", "password": "admin123" }
```

返回：

```json
{ "ok": true, "token": "<jwt>" }
```

2) 携带请求头：`Authorization: Bearer <jwt>`

- `GET /api/admin/stats`
- `GET /api/admin/history?limit=50`
- `POST /api/admin/cache/clear`

## 4. 数据库结构（SQLite）

- `users`：后台用户
- `history`：调用历史（成功/失败、耗时、来源）
- `cache`：生成结果缓存（带 TTL）

## 5. 打包为单二进制

```bash
go build -o ptgen-go .
./ptgen-go
```

## 6. Docker 打包与运行

### 6.1 构建镜像

```bash
docker build -t ptgen-go:latest .
```

### 6.2 直接运行

```bash
docker run -d --name ptgen \
  -p 53000:53000 \
  -e PTGEN_ADMIN_USER=admin \
  -e PTGEN_ADMIN_PASSWORD=admin123 \
  -e PTGEN_JWT_SECRET=change-this-in-production \
  -v $(pwd)/data:/data \
  ptgen-go:latest
```

### 6.3 使用 docker compose

```bash
docker compose up -d --build
```

默认会映射端口 `53000`，并将数据库持久化到宿主机 `./data` 目录。
