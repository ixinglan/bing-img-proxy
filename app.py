"""
bing-img-proxy
读取配置文件中的 Bing 图片 id，随机挑一个，302 重定向到：
    <BING_BASE_URL> + <id>
并对请求来源（Origin / Referer）做白名单校验。

另提供一个「图片池」接口（默认 /api/bg/image）：
不做 302 跳转，而是把 Bing 图片的字节直接返回给浏览器，并带强缓存头
（Cache-Control: public, max-age=...)；服务端预先维护一小批「已下载好」的图片，
请求时直接从本地磁盘取，取走后立刻在后台线程异步补货，始终保持若干张就绪，
从而规避「实时回源 Bing 造成的等待」，并让浏览器对图片做本地缓存、实现切换零白屏。

配置（均可用环境变量覆盖）：
    CONFIG_DIR          配置目录，容器默认 /app/config
    IMAGE_IDS_FILE      图片 id 文件，默认 <CONFIG_DIR>/image_ids.txt
    ORIGINS_FILE        来源白名单文件，默认 <CONFIG_DIR>/origins.txt
    BING_BASE_URL       回源前缀，默认 https://cn.bing.com/th?id=
    ROUTE_PATH          对外「随机重定向」路径，默认 /

    POOL_ROUTE_PATH     对外「图片池」路径，默认 /api/bg/image
    POOL_SIZE           服务端预留的就绪图片数量，默认 5
    CACHE_DIR           图片磁盘缓存目录，默认 cache
    POOL_CACHE_MAX_AGE  返回给浏览器的缓存秒数，默认 86400（1 天）
    DOWNLOAD_TIMEOUT    回源下载超时秒数，默认 10
    UPSTREAM_USER_AGENT 回源时的 User-Agent
"""
import os
import hashlib
import random
import secrets
import threading
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, FileResponse

# ---------- 配置 ----------
CONFIG_DIR = os.getenv("CONFIG_DIR", "config")
IMAGE_IDS_FILE = os.getenv("IMAGE_IDS_FILE", os.path.join(CONFIG_DIR, "image_ids.txt"))
ORIGINS_FILE = os.getenv("ORIGINS_FILE", os.path.join(CONFIG_DIR, "origins.txt"))
BING_BASE_URL = os.getenv("BING_BASE_URL", "https://cn.bing.com/th?id=").rstrip()

# 处理 ROUTE_PATH 环境变量，确保路径格式规范
_route = os.getenv("ROUTE_PATH", "/") or "/"
if not _route.startswith("/"):
    _route = "/" + _route
ROUTE_PATH = _route.rstrip("/") if _route != "/" else "/"

# ---------- 图片池相关配置 ----------
POOL_SIZE = max(1, int(os.getenv("POOL_SIZE", "5")))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")
POOL_CACHE_MAX_AGE = int(os.getenv("POOL_CACHE_MAX_AGE", "86400"))
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "10"))
UPSTREAM_USER_AGENT = os.getenv(
    "UPSTREAM_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# 处理 POOL_ROUTE_PATH 环境变量，确保路径格式规范
_pool_route = os.getenv("POOL_ROUTE_PATH", "/api/bg/image") or "/api/bg/image"
if not _pool_route.startswith("/"):
    _pool_route = "/" + _pool_route
POOL_ROUTE_PATH = _pool_route.rstrip("/") or "/api/bg/image"


# ---------- 回源下载 ----------
def download_image(img_id: str) -> tuple[bytes, str] | None:
    """回源下载一张 Bing 图片。

    成功返回 (图片字节, content-type)；任何失败（超时/网络异常/上游非 2xx/空响应）
    都返回 None，交由调用方决定是否重试。只用标准库 urllib，避免引入额外依赖。
    """
    url = f"{BING_BASE_URL}{img_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UPSTREAM_USER_AGENT,
            "Referer": "https://cn.bing.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            data = resp.read()
            if not data:
                return None
            ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            return data, (ctype or "image/jpeg")
    except Exception:
        return None


# ---------- 配置加载（mtime 变更自动热重载） ----------
class ConfigLoader:
    def __init__(self, image_path: str, origin_path: str):
        self.image_path = image_path
        self.origin_path = origin_path
        self._image_ids: list[str] = []
        self._origins: list[str] = []
        self._image_mtime = -1
        self._origin_mtime = -1
        self.reload_if_changed()

    @staticmethod
    def _read_lines(path: str) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [
                    ln.strip()
                    for ln in f
                    if ln.strip() and not ln.strip().startswith("#")
                ]
        except FileNotFoundError:
            return []

    def reload_if_changed(self) -> None:
        for path, attr in ((self.image_path, "image"), (self.origin_path, "origin")):
            try:
                mtime = os.path.getmtime(path)
            except FileNotFoundError:
                mtime = -1
            if mtime != getattr(self, f"_{attr}_mtime"):
                setattr(self, f"_{attr}_mtime", mtime)
                lines = self._read_lines(path)
                if attr == "image":
                    seen: set[str] = set()
                    self._image_ids = [x for x in lines if not (x in seen or seen.add(x))]
                else:
                    self._origins = lines

    @property
    def image_ids(self) -> list[str]:
        self.reload_if_changed()
        return self._image_ids

    @property
    def origins(self) -> list[str]:
        self.reload_if_changed()
        return self._origins


loader = ConfigLoader(IMAGE_IDS_FILE, ORIGINS_FILE)


# ---------- 图片池 ----------
class ImagePool:
    """服务端图片池：让「返回图片字节」的请求直接从本地取，规避回源延迟。

    三个组成部分：
      1. 磁盘缓存：<CACHE_DIR>/<sha1(id)>.img 存图片字节、<sha1(id)>.type 存 content-type。
         同一 id 的内容固定，可长期复用；多个 gunicorn worker 之间共享同一份磁盘缓存，
         进程重启后也依然命中（不需要重新回源）。
      2. 内存池  ：每个 worker 进程各自维护一个「就绪 id」队列 deque，长度上限 POOL_SIZE。
         worker 之间不共享内存，但共享磁盘，所以任一 worker 下过的图，另一个也能直接命中。
      3. 补货    ：每取走一张就立即在后台线程补一张，使池恢复到 POOL_SIZE。
         即用户要的「取 1 张 → 剩 4 张 → 异步补 1 张 → 又是 5 张」效果。
    """

    def __init__(self, cache_dir: str, size: int):
        self.cache_dir = cache_dir
        self.size = max(1, int(size))
        self._lock = threading.Lock()
        self._ready: deque[str] = deque()   # 已就绪（本地可取）的 image_id
        self._inflight: set[str] = set()    # 正在下载中的 image_id，避免重复下载
        self._recent: deque[str] = deque(maxlen=max(1, int(size)))  # 最近返回过的 id，避免短期重复
        self._refilling = False             # 是否已有后台补货线程在跑
        os.makedirs(self.cache_dir, exist_ok=True)

    # ----- 查询 -----
    def ready_count(self) -> int:
        with self._lock:
            return len(self._ready)

    # ----- 磁盘缓存 -----
    def _cache_paths(self, img_id: str) -> tuple[str, str]:
        key = hashlib.sha1(img_id.encode("utf-8")).hexdigest()
        return (
            os.path.join(self.cache_dir, key + ".img"),
            os.path.join(self.cache_dir, key + ".type"),
        )

    def _cached(self, img_id: str) -> tuple[str, str] | None:
        """若该 id 已在磁盘缓存中，返回 (路径, content-type)，否则返回 None。"""
        data_path, type_path = self._cache_paths(img_id)
        if not os.path.exists(data_path):
            return None
        ctype = "image/jpeg"
        try:
            with open(type_path, "r", encoding="utf-8") as f:
                ctype = f.read().strip() or ctype
        except OSError:
            pass
        return data_path, ctype

    def _ensure_cached(self, img_id: str) -> tuple[str, str] | None:
        """确保该 id 的图片已落盘（命中则直接用，否则回源下载并原子写入）。"""
        hit = self._cached(img_id)
        if hit:
            return hit
        result = download_image(img_id)
        if result is None:
            return None
        data, ctype = result
        data_path, type_path = self._cache_paths(img_id)
        tmp_path = f"{data_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, data_path)  # 原子替换：避免别的请求读到写了一半的文件
            with open(type_path, "w", encoding="utf-8") as f:
                f.write(ctype)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return None
        return data_path, ctype

    # ----- 补货 -----
    def _candidates(self) -> list[str]:
        """可用于补货的候选 id。

        优先选「不在池里、不在下载中、也不在最近返回列表里」的，尽量避免短期重复；
        若候选被穷尽，则逐级放宽条件，保证始终返回非空候选（除非 id 列表本身为空）。
        """
        ids = loader.image_ids
        if not ids:
            return []
        with self._lock:
            ready = set(self._ready)
            inflight = set(self._inflight)
            recent = set(self._recent)
        free = [i for i in ids if i not in ready and i not in inflight and i not in recent]
        if free:
            return free
        relaxed = [i for i in ids if i not in ready and i not in inflight]
        return relaxed or list(ids)

    def _refill_once(self) -> bool:
        """尝试补 1 张进池，成功返回 True。"""
        candidates = self._candidates()
        if not candidates:
            return False
        img_id = secrets.choice(candidates)
        with self._lock:
            if img_id in self._ready or img_id in self._inflight:
                return False
            self._inflight.add(img_id)
        try:
            hit = self._ensure_cached(img_id)
        finally:
            with self._lock:
                self._inflight.discard(img_id)
        if hit is None:
            return False
        with self._lock:
            if img_id not in self._ready:
                self._ready.append(img_id)
        return True

    def refill(self) -> None:
        """把池补到 POOL_SIZE。

        每轮并发补若干张（最多 3 张），加快冷启动预热；带尝试上限，
        避免个别 id 长期下载失败时死循环。
        """
        limit = max(self.size * 3, 9)
        attempts = 0
        while attempts < limit:
            with self._lock:
                need = self.size - len(self._ready)
            if need <= 0:
                return
            batch = min(need, 3)
            threads = [
                threading.Thread(target=self._refill_once, name="pool-fill", daemon=True)
                for _ in range(batch)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            attempts += batch

    def refill_async(self) -> None:
        """后台线程补货；同一时刻只允许一个补货线程在跑，避免线程堆积。"""
        with self._lock:
            if self._refilling:
                return
            self._refilling = True

        def _job() -> None:
            try:
                self.refill()
            finally:
                with self._lock:
                    self._refilling = False

        threading.Thread(target=_job, name="pool-refill", daemon=True).start()

    # ----- 取图 -----
    def _mark_recent(self, img_id: str) -> None:
        with self._lock:
            self._recent.append(img_id)

    def acquire(self) -> tuple[str, str, str] | None:
        """取一张图片。

        返回 (本地路径, content-type, image_id)；池空（冷启动或并发高峰）时同步下载一张兜底。
        """
        # 快路径：池里已有现成的，直接拿走（这就是「从缓存拿」的秒回路径）
        for _ in range(8):
            with self._lock:
                img_id = self._ready.popleft() if self._ready else None
            if img_id is None:
                break
            hit = self._cached(img_id)
            if hit:
                self._mark_recent(img_id)
                return hit[0], hit[1], img_id
            # 缓存文件被外部清掉了：丢弃该 id，继续从池里取下一张

        # 慢路径：池空 → 同步下载一张兜底（只有冷启动/首次才会走到这里）
        candidates = self._candidates()
        random.shuffle(candidates)
        for img_id in candidates[:3]:
            hit = self._ensure_cached(img_id)
            if hit:
                with self._lock:
                    self._recent.append(img_id)
                    try:
                        self._ready.remove(img_id)
                    except ValueError:
                        pass
                return hit[0], hit[1], img_id
        return None


pool = ImagePool(CACHE_DIR, POOL_SIZE)


# ---------- 来源校验 ----------
def get_request_origin(referer: str | None, origin: str | None) -> str | None:
    """从 Origin 头或 Referer 头中解析出请求来源（scheme://host[:port]）。"""
    if origin:
        return origin.strip().rstrip("/")
    if referer:
        p = urlparse(referer)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}".rstrip("/")
    return None


def origin_allowed(request_origin: str | None, whitelist: list[str]) -> bool:
    """白名单为空 -> 开放模式（允许所有）；否则精确匹配或 *.子域通配。"""
    if not whitelist:
        return True
    if not request_origin:
        return False
    ro = request_origin.lower().rstrip("/")
    for entry in whitelist:
        e = entry.strip().lower().rstrip("/")
        if not e:
            continue
        if e == ro:
            return True
        if e.startswith("*."):
            domain = e[2:]
            host = urlparse(ro).netloc
            if host == domain or host.endswith("." + domain):
                return True
    return False


# ---------- 应用 ----------
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时后台预热图片池（不阻塞启动；预热失败也无妨，首个请求会兜底同步下载）
    pool.refill_async()
    yield


app = FastAPI(title="bing-img-proxy", lifespan=lifespan)


@app.get("/api/bg/health")
def health() -> dict:
    return {"status": "ok", "image_count": len(loader.image_ids)}


@app.get("/api/bg/pool")
def pool_status() -> dict:
    """图片池状态，便于运维观测当前 worker 里有多少张已就绪。"""
    return {"pool_size": pool.size, "ready": pool.ready_count()}


def handle_random(request: Request) -> RedirectResponse:
    if not loader.image_ids:
        raise HTTPException(status_code=503, detail="no image ids configured")

    req_origin = get_request_origin(
        request.headers.get("referer"), request.headers.get("origin")
    )
    if not origin_allowed(req_origin, loader.origins):
        raise HTTPException(status_code=403, detail="origin not allowed")

    # 若携带 seed 则确定性选图：同一 seed 永远映射到同一张图（用于前端缓存背景，
    # 只在用户点击「刷新背景」时换 seed），否则保持原有随机行为。
    seed = request.query_params.get("seed")
    ids = loader.image_ids
    if seed:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        img_id = ids[int(digest, 16) % len(ids)]
    else:
        img_id = secrets.choice(ids)

    url = f"{BING_BASE_URL}{img_id}"
    # 防缓存：每次都随机，不被浏览器 / CDN 缓存
    return RedirectResponse(
        url,
        status_code=302,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def handle_pool_image(request: Request) -> FileResponse:
    """图片池接口：直接返回同源图片字节 + 强缓存头，取走后异步补货。

    与原 302 接口的区别：
      - 响应体是图片本身（同源），可被浏览器磁盘缓存；
      - URL 稳定（前端按 seed 生成）→ 切换菜单命中浏览器缓存，不再回源，从而消除白屏。
    """
    if not loader.image_ids:
        raise HTTPException(status_code=503, detail="no image ids configured")

    req_origin = get_request_origin(
        request.headers.get("referer"), request.headers.get("origin")
    )
    if not origin_allowed(req_origin, loader.origins):
        raise HTTPException(status_code=403, detail="origin not allowed")

    picked = pool.acquire()
    if picked is None:
        raise HTTPException(status_code=502, detail="failed to fetch image from upstream")
    path, ctype, img_id = picked

    # 取走一张后立刻异步补一张，使池恢复到 POOL_SIZE（对前端表现为「秒回」）
    pool.refill_async()

    return FileResponse(
        path,
        media_type=ctype,
        headers={
            # 强缓存 1 天：同一 seed 的 URL 在浏览器端直接命中，不再回源 → 切换零延迟
            "Cache-Control": f"public, max-age={POOL_CACHE_MAX_AGE}, immutable",
            "X-Bing-Image-Id": img_id,
        },
    )


app.add_api_route(ROUTE_PATH, handle_random, methods=["GET"])

# 图片池接口独立注册（默认 /api/bg/image），与原 ROUTE_PATH 互不影响
if POOL_ROUTE_PATH != ROUTE_PATH:
    app.add_api_route(POOL_ROUTE_PATH, handle_pool_image, methods=["GET"])
