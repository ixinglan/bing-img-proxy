# bing-img-proxy

一个 20KB 的图片代理服务：让你的网站**每次刷新都换一张 Bing 每日壁纸**，且不用把图片存到自己服务器。

## 为什么需要它

Bing 每日壁纸很好看，但直接用 `https://cn.bing.com/th?id=xxx` 会有几个麻烦：

- **CORS / 防盗链**：浏览器端直接引用，容易踩跨域和来源限制的坑；
- **图片 id 硬编码在前端**：想换一批图、加一批图，得改代码重新发布；
- **不随机**：一张图看久了想换，得手动找新 id；
- **没有来源控制**：图片链接被人扒走，谁的站点都能白嫖你的服务。

本项目就是一个中间层：前端只请求你自己的地址，随机拿一张图，剩下的它帮你搞定。

## 功能

- **随机重定向**：访问一个地址 → `302` 跳到一张随机 Bing 壁纸。
- **确定性选图**：带 `?seed=xxx` 时，同一 seed 永远返回同一张图（适合前端缓存背景，只在用户点「换一张」时才变）。
- **防缓存**：响应带 `Cache-Control: no-store`，保证每次请求都真的随机，不被浏览器/CDN 缓存掉。
- **来源白名单**：按 `Origin` / `Referer` 校验，支持 `*.example.com` 通配子域；留空则完全开放。
- **配置热加载**：改 id 列表 / 白名单文件后**无需重启**，按文件 mtime 自动生效。
- **图片 id 与代码解耦**：`config/image_ids.txt` 里一行一个，注释、自动去重都支持。

## 快速开始

```bash
git clone <repo> && cd bing-img-proxy

# 1. 填图片 id（获取方式见下方「配置」）
vim config/image_ids.txt

# 2. 填来源白名单，留空则为开放模式
vim config/origins.txt

# 3. 起服务
docker compose up -d --build
```

验证：

```bash
curl -i http://localhost:18088/          # 期望 302
curl -i http://localhost:18088/?seed=abc # 期望 302，且 seed 相同则目标稳定不变
curl -i http://localhost:18088/api/bg/health  # {"status":"ok","image_count":N}
```

前端里这样用：

```html
<img src="https://your-domain.com/?seed=abc123" alt="wallpaper" />
```

## 配置

### `config/image_ids.txt`

每行一个 Bing 图片 id —— 即 `https://cn.bing.com/th?id=<id>` 里 `<id>` 的部分。
最省事的获取方式：打开 `cn.bing.com`，在浏览器开发者工具 Network 里看背景图请求，把 `th?id=` 后面的字符串复制出来即可。

增量实现方案：配置 AI Agent 自动化任务，动态抓取最新壁纸 ID，实时、增量维护到该配置文件。
`#` 开头为注释行，自动去重，可随意增删。

### `config/origins.txt`

允许访问的来源白名单，每行一个完整 origin（含协议），例如 `https://your-site.com`。

- **留空 = 开放模式**，允许任何来源；
- 支持通配子域：`*.example.com` 匹配 `https://a.example.com`、`https://b.example.com`；
- 校验优先取 `Origin` 头，没有则回退 `Referer`。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ROUTE_PATH` | `/` | 对外随机图路径，可改成 `/random` 等 |
| `BING_BASE_URL` | `https://cn.bing.com/th?id=` | 重定向前缀 |
| `CONFIG_DIR` | `/app/config`（本地跑为 `config`） | 配置目录，一般无需改 |
| `IMAGE_IDS_FILE` | `<CONFIG_DIR>/image_ids.txt` | 图片 id 文件 |
| `ORIGINS_FILE` | `<CONFIG_DIR>/origins.txt` | 白名单文件 |

### 返回码

| 状态码 | 场景 |
| --- | --- |
| `302` | 正常，重定向到随机壁纸 |
| `403` | 来源不在白名单 |
| `503` | 图片 id 列表为空 |

## 构建镜像

提供两个脚本，都用 `docker buildx`，无需手动处理跨架构问题：

```bash
./build.sh            # linux/amd64（默认，适合 x86 云服务器）
./build-arm64.sh      # linux/arm64（适合 ARM 云服务器）
```

常用参数：`-t v1.0.0` 指定 tag、`-n myrepo/name` 指定镜像名、`--no-cache` 清缓存、
`--push` 推送到远端仓库。也支持多架构 manifest：

```bash
./build.sh -p linux/amd64,linux/arm64 --push
```

> 若目标机器是 ARM64，记得把 `docker-compose.yml` 里的 `platform` 改成 `linux/arm64`，
> 否则会拿到 amd64 镜像。

## 不使用 Docker 运行

```bash
uv sync
uv run gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:18088
```

或不使用 uv：`pip install -r requirements.txt` 后执行同样的 gunicorn 命令。
（本地直接跑时 `CONFIG_DIR` 默认为 `config`，无需额外配置。）

```bash
# 升级全部依赖
uv lock --upgrade

# 导出 requirements.txt 给不用 uv 的场景
uv export --frozen --no-dev -o requirements.txt
```

## 技术细节

- **技术栈**：FastAPI + gunicorn/uvicorn worker，依赖用 [uv](https://github.com/astral-sh/uv) 管理（`pyproject.toml` 为唯一源）。
- **依赖清单**：`pyproject.toml` / `uv.lock` 是权威来源；`requirements.txt` 由 `uv export --frozen --no-dev -o requirements.txt` 生成，供 pip 兜底。
- **热加载实现**：每次读取配置时比对文件 `mtime`，变化才重新解析，无锁无后台线程。
- **目录结构**：

```
bing-img-proxy/
├── app.py                 # 主程序（配置加载 + 路由 + 来源校验）
├── config/
│   ├── image_ids.txt      # 图片 id，每行一个
│   └── origins.txt        # 来源白名单
├── build.sh               # 构建 amd64 镜像
├── build-arm64.sh         # 构建 arm64 镜像
├── pyproject.toml         # 依赖清单（uv）
├── uv.lock                # 锁文件，需提交
├── requirements.txt       # pip 兜底
├── Dockerfile
├── docker-compose.yml
└── README.md
```

> 生产环境使用 `gunicorn + uvicorn worker` 启动（非开发服务器）；镜像中配置目录以只读方式挂载，改配置不需要重建镜像。
