# bing-img-proxy

轻量 HTTP 服务：从配置文件读取 Bing 图片 id 列表 → 随机挑一个 → `302` 重定向到
`https://cn.bing.com/th?id=<id>`，并对请求来源（`Origin` / `Referer`）做白名单校验。

技术栈：**FastAPI** + **gunicorn/uvicorn**，依赖用 **uv** 管理，用 **Docker Compose** 运行。

## 目录结构

```
bing-img-proxy/
├── app.py                 # 主程序
├── config/
│   ├── image_ids.txt      # 图片 id，每行一个（# 开头为注释）
│   └── origins.txt         # 来源白名单，每行一个（留空=开放模式）
├── pyproject.toml         # 依赖清单（uv 管理，唯一源）
├── uv.lock                # 锁文件（可复现）
├── requirements.txt       # 由 uv export 生成，供 pip 兜底使用
├── .python-version        # 固定 Python 3.12
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 依赖管理（uv）

本项目用 [uv](https://github.com/astral-sh/uv) 管理依赖，`pyproject.toml` 是唯一源。

```bash
# 首次安装（生成 .venv 并安装依赖）
uv sync

# 加一个新依赖（运行期）
uv add fastapi

# 加开发依赖（如测试）
uv add --dev pytest

# 升级全部依赖
uv lock --upgrade

# 导出 requirements.txt 给不用 uv 的场景
uv export --frozen --no-dev -o requirements.txt

# 运行命令（自动进入 .venv）
uv run gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080
```

> `uv.lock` 必须提交，保证团队/CI/Docker 构建结果一致。`.venv` 已被 `.dockerignore` 忽略。

## 配置说明

### config/image_ids.txt
每行一个 Bing 图片 id（即 `https://cn.bing.com/th?id=<id>` 中 `<id>` 的部分）。
`#` 开头的行为注释，会被忽略；自动去重；文件改动后无需重启（按 mtime 热重载）。

### config/origins.txt
允许访问的来源白名单，每行一个，写完整 origin（含协议），例如 `https://your-site.com`。
- **留空（无内容）= 开放模式，允许任何来源**
- 支持通配子域：`*.example.com` 匹配 `https://a.example.com`、`https://b.example.com`
- 校验优先取 `Origin` 请求头，回退取 `Referer`

### 环境变量（docker-compose.yml 中配置）
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ROUTE_PATH` | `/` | 对外随机图路径，可改成 `/random` 等 |
| `BING_BASE_URL` | `https://cn.bing.com/th?id=` | 重定向前缀 |
| `CONFIG_DIR` | `/app/config` | 配置目录（一般情况无需改） |
| `IMAGE_IDS_FILE` | `<CONFIG_DIR>/image_ids.txt` | 图片 id 文件 |
| `ORIGINS_FILE` | `<CONFIG_DIR>/origins.txt` | 白名单文件 |

## 运行

```bash
cd bing-img-proxy
docker compose up -d --build
```

访问：

- `http://localhost:8080/` —— 随机重定向到一张 Bing 壁纸（受来源白名单限制）
- `http://localhost:8080/health` —— 健康检查，返回 `{"status":"ok","image_count":N}`

改了 `config/` 下的文件后无需重建镜像，容器会自动热加载（靠文件 mtime 检测）。

## 行为说明

- 重定向使用 `302`，并带 `Cache-Control: no-store`，保证每次请求都随机、不被缓存。
- 图片 id 列表为空 → 返回 `503`。
- 来源不在白名单 → 返回 `403`。
- 生产用 `gunicorn + uvicorn worker` 启动（非开发服务器）。

## 本地直接用 Python 跑（不走 Docker）

```bash
uv sync                       # 安装依赖到 .venv
uv run gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080
```

或不使用 uv：

```bash
pip install -r requirements.txt
gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080
```
