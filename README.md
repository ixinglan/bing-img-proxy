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
curl -i http://localhost:18088/api/random          # 期望 302
curl -i http://localhost:18088/api/random?seed=abc # 期望 302，且 seed 相同则目标稳定不变
curl -i http://localhost:18088/api/bg/health  # {"status":"ok","image_count":N}
```

前端里这样用：

```html
<img src="https://your-domain.com/api/random?seed=abc123" alt="wallpaper" />
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

## 图片池接口（推荐用于「背景不白屏」场景）

`/api/bg/random` 是 **302 重定向**，浏览器每次整页导航都要重走一趟重定向再下载图片；
在 Hugo 这类多页站点上切换页面时，会出现「白屏 → 背景加载」的闪烁（且重定向响应带 `no-store`，
浏览器无法缓存，每次导航都重新下载）。

为此新增接口 **`GET /api/bg/image`**（默认路径，可用 `POOL_ROUTE_PATH` 改）：

- **直接返回图片字节**（同源，不再是 302），响应带
  `Cache-Control: public, max-age=86400, immutable`，浏览器会本地缓存；
- **URL 稳定**（前端按 `?seed=` 生成同一个 URL）→ 切换页面命中浏览器缓存、不再回源，**零白屏**；
- **服务端图片池**：预先保留若干张（默认 5 张）已下载好的图片，请求时**直接从本地磁盘取**，
  取走一张后**立刻在后台异步补一张**，始终保持 5 张就绪，避免实时回源 Bing 的等待。

「取 1 补 1」的效果：

```
第 1 次请求：从池中取 1 张秒回 → 剩 4 张 → 后台异步补 1 张 → 回到 5 张
第 2 次请求：取缓存 → 剩 4 张 → 再异步补 1 张 → 共 5 张
...以此类推
```

### 三个必须同时成立的设计

要让「切换页面零白屏」真正成立，下面三件事缺一不可：

**① 确定性映射 —— 同一 `seed` 永远解析到同一 `image_id`**

如果服务端对同一个 `seed` 每次随机返回不同的图，浏览器的 HTTP 缓存会失去意义
（缓存的是 URL→响应，会重新取到另一张图），甚至出现「刷新一次背景就变一次」。
因此 `seed → image_id` 用 `sha256(seed) % len(ids)` 做确定性映射；
不带 `seed` 时才随机，且此时响应标记为 `no-store`（避免把「随机」冻结成「固定」）。

**② 磁盘缓存有上限 —— LRU 淘汰，文件对数不超过 `CACHE_MAX_FILES`**

`CACHE_MAX_FILES` 是「磁盘最多留几份」的安全上限，**≠** 热池大小 `POOL_SIZE`。
按 mtime 由旧到新淘汰，但**永不删除**热池中就绪的图与最近返回过的图（它们随时可能被再次命中）。
该上限是**全局**的：多个 worker 共享同一目录，各自 `listdir` 计数，因此不会被 worker 数放大。

**③ 回源缩图 —— 默认追加 `w/h/c/rs` 参数**

Bing 的 `th` 接口支持按需缩放，不传参数时可能是 UHD 大图。实测（2026-09-16）：

| 图片 | 原图 | 缩到 1920×1080 | 差异 |
| --- | --- | --- | --- |
| `OHR.BoatsMalta_..._UHD.jpg` | 3,679,107 字节 / 12.87s | 318,167 字节 / 1.38s | 体积 11.6× / 耗时 9.3× |
| `OHR.HawaiiLava_..._UHD.jpg` | 3,796,366 字节 / 9.93s | 201,138 字节 / 1.60s | 体积 18.9× / 耗时 6.2× |

对本来就是 1920×1080 的 id 几乎无影响（338KB → 322KB），因此该参数「只赚不亏」。

> **注意**：缩图默认**只作用于新接口 `/api/bg/image`**；原 302 接口的跳转目标保持原样。
> 若你确认希望 302 也缩图，把 `RESIZE_ON_REDIRECT` 设为 `1`。

验证：

```bash
curl -i "http://localhost:18088/api/bg/image?seed=abc"   # 200 + image/webp|jpeg|avif + Cache-Control
curl -s "http://localhost:18088/api/bg/pool"             # 池/缓存状态，含 cache_files 与 cache_max_files
```

`/api/bg/pool` 返回示例：

```json
{"pool_size": 5, "ready": 5, "cache_files": 12, "cache_max_files": 20,
 "resize_params": "w=1920&h=1080&c=7&rs=1"}
```

图片缓存落在 `CACHE_DIR`（默认 `cache/`），文件名为 `<sha1(id)>.img` / `<sha1(id)>.type`；
多个 gunicorn worker 共享同一份磁盘缓存，容器重启后仍可复用
（建议把该目录挂载到宿主机，`docker-compose.yml` 已配好）。

新增环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `POOL_ROUTE_PATH` | `/api/bg/image` | 图片池接口路径 |
| `POOL_SIZE` | `5` | 服务端预热（常驻）的图片张数 |
| `CACHE_DIR` | `cache` | 图片磁盘缓存目录（容器内为 `/app/cache`） |
| `CACHE_MAX_FILES` | `POOL_SIZE*4`（即 `20`） | 磁盘缓存文件对数上限（LRU）；`0` 表示不限制 |
| `BING_IMAGE_PARAMS` | `w=1920&h=1080&c=7&rs=1` | 回源缩图参数；置空则取原图 |
| `RESIZE_ON_REDIRECT` | `0` | 是否让原 302 接口也缩图（默认不改动原接口） |
| `POOL_CACHE_MAX_AGE` | `86400` | 返回给浏览器的缓存秒数（1 天） |
| `DOWNLOAD_TIMEOUT` | `10` | 回源下载超时秒数 |
| `UPSTREAM_USER_AGENT` | 内置浏览器 UA | 回源时的 User-Agent |

> 与原接口的关系：`/api/bg/random` **完全保持不变**（仍 302、仍 `no-store`、跳转目标不带缩图参数）。新老接口并存。

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
