"""
bing-img-proxy
读取配置文件中的 Bing 图片 id，随机挑一个，302 重定向到：
    <BING_BASE_URL> + <id>
并对请求来源（Origin / Referer）做白名单校验。

另提供一个「图片池」接口（默认 /api/bg/image）：
不做 302 跳转，而是把 Bing 图片的字节直接返回给浏览器，并带强缓存头
（Cache-Control: public, max-age=..., immutable）；服务端维护一个小型磁盘缓存 + 热池，
命中缓存时毫秒级返回，未命中才回源下载，下载完立即在后台补货。

要让「切换菜单不再白屏」真正成立，下面三件事必须同时满足，缺一不可：

  1) 确定性映射：同一个 ?seed= 永远解析到同一个 image_id。
     前端 URL 因此是稳定的 → 浏览器 HTTP 缓存才能真正命中 → 切换页面 0 请求。
     （反面教材：服务端若对同一 seed 随机返回不同图，浏览器缓存就失去意义。）

  2) 磁盘缓存有上限：LRU 淘汰，文件对数不超过 CACHE_MAX_FILES。
     否则随着 ?seed= 的多样性增长，image_ids.txt 里的 id 会被逐个下载并永久留在磁盘上。
     注意：磁盘上限 ≠ 热池大小。热池 POOL_SIZE 是「预热几张」，磁盘上限是「最多留几份」。

  3) 回源缩图：对新接口默认追加 w/h/c/rs 参数（原 302 接口默认保持原样）。
     实测（2026-09-16）：UHD 原图 3,679,107 字节 / 12.87s →
     缩到 1920x1080 后 318,167 字节 / 1.38s，体积约 11.6 倍、耗时约 9.3 倍差异。

配置（均可用环境变量覆盖）：
    CONFIG_DIR          配置目录，容器默认 /app/config
    IMAGE_IDS_FILE      图片 id 文件，默认 <CONFIG_DIR>/image_ids.txt
    ORIGINS_FILE        来源白名单文件，默认 <CONFIG_DIR>/origins.txt
    BING_BASE_URL       回源前缀，默认 https://cn.bing.com/th?id=
    ROUTE_PATH          对外「随机重定向」路径，默认 /

    POOL_ROUTE_PATH     对外「图片池」路径，默认 /api/bg/image
    POOL_SIZE           服务端预热（常驻）的图片数量，默认 5
    CACHE_DIR           图片磁盘缓存目录，默认 cache
    CACHE_MAX_FILES     磁盘缓存文件对数上限（LRU），默认 POOL_SIZE*4（=20）
                        设为 0 表示不限制（不推荐）
    BING_IMAGE_PARAMS   回源缩图参数，默认 w=1920&h=1080&c=7&rs=1；置空则取原图
    RESIZE_ON_REDIRECT  是否让「原 302 接口」也缩图，默认 0（不改动原接口）
    POOL_CACHE_MAX_AGE  返回给浏览器的缓存秒数，默认 86400（1 天）
    DOWNLOAD_TIMEOUT    回源下载超时秒数，默认 10
    UPSTREAM_USER_AGENT 回源时的 User-Agent
"""
import os
import hashlib
import secrets
import threading
import time
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

# Bing 的 th 接口支持 w/h/c/rs 参数按需缩放：不传就是原始尺寸（可能是 UHD 大图）。
# 前置的 "&" 由 build_upstream_url 统一拼接，这里只存 "k=v&k=v" 形式。
BING_IMAGE_PARAMS = os.getenv("BING_IMAGE_PARAMS", "w=1920&h=1080&c=7&rs=1").strip().lstrip("&")

# 是否把缩图参数也用在「原 302 接口」的跳转目标上。
# 默认关闭：需求明确要求「原 302 接口不动」，缩图只作用于新接口 /api/bg/image。
# 若你确认想让 302 也缩图（能显著加快线上首屏），把它设为 1 即可。
RESIZE_ON_REDIRECT = os.getenv("RESIZE_ON_REDIRECT", "0").strip().lower() in {"1", "true", "yes", "on"}

# 处理 ROUTE_PATH 环境变量，确保路径格式规范
_route = os.getenv("ROUTE_PATH", "/") or "/"
if not _route.startswith("/"):
    _route = "/" + _route
ROUTE_PATH = _route.rstrip("/") if _route != "/" else "/"

# ---------- 图片池相关配置 ----------
POOL_SIZE = max(1, int(os.getenv("POOL_SIZE", "5")))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")
# 磁盘缓存上限：默认给热池的 4 倍，既留足余量给「访客各自 seed 映射到的图」，
# 又能保证淘汰逻辑有空隙可做（protected 数量必须小于 cap，否则永远淘汰不动）。
CACHE_MAX_FILES = int(os.getenv("CACHE_MAX_FILES", str(POOL_SIZE * 4)))
POOL_CACHE_MAX_AGE = int(os.getenv("POOL_CACHE_MAX_AGE", "86400"))
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "10"))
UPSTREAM_USER_AGENT = os.getenv(
    "UPSTREAM_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
# 残留 .tmp 文件的清理阈值（秒）：进程被 kill 时可能留下写了一半的临时文件
TMP_TTL_SECONDS = 600

# 处理 POOL_ROUTE_PATH 环境变量，确保路径格式规范
_pool_route = os.getenv("POOL_ROUTE_PATH", "/api/bg/image") or "/api/bg/image"
if not _pool_route.startswith("/"):
    _pool_route = "/" + _pool_route
POOL_ROUTE_PATH = _pool_route.rstrip("/") or "/api/bg/image"


# ---------- 回源下载 ----------
def build_upstream_url(img_id: str, resize: bool = True) -> str:
    """拼出回源 URL。

    img_id 里可能带点号、下划线、连字符，但不会有 "?"，所以这里用 "&" 直接续参数即可。

    resize=False 时不追加缩图参数（用于「原 302 接口保持完全不变」的场景）。
    """
    url = f"{BING_BASE_URL}{img_id}"
    if resize and BING_IMAGE_PARAMS:
        url = f"{url}&{BING_IMAGE_PARAMS}"
    return url


def download_image(img_id: str) -> tuple[bytes, str] | None:
    """回源下载一张 Bing 图片。

    成功返回 (图片字节, content-type)；任何失败（超时/网络异常/上游非 2xx/空响应）
    都返回 None，交由调用方决定是否重试。只用标准库 urllib，避免引入额外依赖。
    """
    url = build_upstream_url(img_id)
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


def pick_image_id(seed: str | None) -> str | None:
    """把一个 seed 解析成 image_id（确定性）。

    - 带 seed：用 sha256 取模，同一 seed 永远落到同一个 id → 前端 URL 稳定 → 浏览器可缓存。
    - 不带 seed：随机取一个（此时响应会标记为 no-store，避免「随机结果被缓存成固定图」）。

    id 列表为空时返回 None，由调用方转成 503。
    """
    ids = loader.image_ids
    if not ids:
        return None
    if seed:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return ids[int(digest, 16) % len(ids)]
    return secrets.choice(ids)


# ---------- 图片池 ----------
class ImagePool:
    """服务端图片缓存：让「返回图片字节」的请求直接从本地取，规避回源延迟。

    三个组成部分：
      1. 磁盘缓存：<CACHE_DIR>/<sha1(id)>.img 存图片字节、<sha1(id)>.type 存 content-type。
         同一 id 的内容固定，可长期复用；多个 gunicorn worker 之间共享同一份磁盘缓存，
         进程重启后也依然命中（不需要重新回源）。
         文件对数由 CACHE_MAX_FILES 做 LRU 上限，避免无限增长。
      2. 热池    ：每个 worker 进程各自维护一个「已就绪 id」队列 deque，长度上限 POOL_SIZE。
         worker 之间不共享内存，但共享磁盘，所以任一 worker 下过的图，另一个也能直接命中。
      3. 补货    ：每取走一张就立即在后台线程补一张，使热池恢复到 POOL_SIZE。
         即用户要的「取 1 张 → 剩 4 张 → 异步补 1 张 → 又是 5 张」效果。
    """

    def __init__(self, cache_dir: str, size: int, max_files: int):
        self.cache_dir = cache_dir
        self.size = max(1, int(size))
        self.cache_max_files = max(0, int(max_files))
        self._lock = threading.Lock()
        self._evict_lock = threading.Lock()  # 淘汰过程串行化，避免并发 listdir/remove 打架
        self._ready: deque[str] = deque()   # 已就绪（本地可取）的 image_id
        self._inflight: set[str] = set()    # 正在下载中的 image_id，避免重复下载
        self._recent: deque[str] = deque(maxlen=max(1, int(size)))  # 最近返回过的 id，避免短期重复
        self._refilling = False             # 是否已有后台补货线程在跑
        os.makedirs(self.cache_dir, exist_ok=True)

    # ----- 查询 -----
    def ready_count(self) -> int:
        with self._lock:
            return len(self._ready)

    def cache_file_count(self) -> int:
        """当前磁盘缓存里已落盘的图片份数（用于运维观测 LRU 上限是否生效）。"""
        try:
            return sum(1 for n in os.listdir(self.cache_dir) if n.endswith(".img"))
        except OSError:
            return 0

    # ----- 磁盘缓存 -----
    def _key(self, img_id: str) -> str:
        """image_id -> 缓存文件名主干（sha1，规避路径注入与超长文件名）。"""
        return hashlib.sha1(img_id.encode("utf-8")).hexdigest()

    def _cache_paths(self, img_id: str) -> tuple[str, str]:
        key = self._key(img_id)
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

    # ----- 磁盘上限（LRU 淘汰） -----
    def _evict_if_needed(self, protect: set[str] | None = None) -> None:
        """把磁盘缓存的文件对数压回 CACHE_MAX_FILES 以内。

        淘汰策略：按 mtime 由旧到新删除，但**永不删除**下面两类：
          - 热池中已就绪的 id（_ready）：它们随时会被下一个请求命中；
          - 最近返回过的 id（_recent）：访客刚看过的图，马上可能再来；
          - 调用方显式传入的 protect（通常是「本次刚写完的那张」）。
        删除时 .img 与 .type 成对删除，避免留下孤儿 meta 文件。
        同时顺手清理超过 TMP_TTL_SECONDS 的残留 .tmp（进程被 kill 时的遗留物）。
        """
        with self._evict_lock:
            try:
                names = os.listdir(self.cache_dir)
            except OSError:
                return

            # 1) 清理残留临时文件
            now = time.time()
            for n in names:
                if not n.endswith(".tmp"):
                    continue
                p = os.path.join(self.cache_dir, n)
                try:
                    if now - os.path.getmtime(p) > TMP_TTL_SECONDS:
                        os.remove(p)
                except OSError:
                    pass

            if self.cache_max_files <= 0:
                return  # 0 表示不限制

            keys = {n[:-4] for n in names if n.endswith(".img")}
            if len(keys) <= self.cache_max_files:
                return

            with self._lock:
                hot = set(self._ready) | set(self._recent)
            protected = {self._key(i) for i in hot}
            if protect:
                protected |= protect

            # 只统计「可淘汰」的，按 mtime 升序（最旧在前）
            evictable: list[tuple[float, str]] = []
            for k in keys - protected:
                p = os.path.join(self.cache_dir, k + ".img")
                try:
                    evictable.append((os.path.getmtime(p), k))
                except OSError:
                    continue
            evictable.sort()

            need = len(keys) - self.cache_max_files
            for _, k in evictable[:need]:
                for suffix in (".img", ".type"):
                    try:
                        os.remove(os.path.join(self.cache_dir, k + suffix))
                    except OSError:
                        pass

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
        # 写盘成功后立刻做一次上限检查；protect 保证「刚写好的这张」不会被自己淘汰掉
        self._evict_if_needed(protect={self._key(img_id)})
        return data_path, ctype

    # ----- 取图（按 id，确定性） -----
    def get(self, img_id: str) -> tuple[str, str, str] | None:
        """取指定 id 的图片。

        命中磁盘缓存 → 毫秒级返回；未命中 → 同步回源下载一张再返回（首次访问才会走到）。
        返回 (本地路径, content-type, image_id)。
        """
        # 若该 id 恰好在热池里，先把它取出（它马上要被消费掉，由 refill 再补上）
        with self._lock:
            try:
                self._ready.remove(img_id)
            except ValueError:
                pass
        hit = self._ensure_cached(img_id)
        if hit is None:
            return None
        self._mark_recent(img_id)
        return hit[0], hit[1], img_id

    # ----- 补货 -----
    def _candidates(self) -> list[str]:
        """可用于补货的候选 id。

        优先选「不在热池里、不在下载中、也不在最近返回列表里」的，尽量避免短期重复；
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
        """把热池补到 POOL_SIZE。

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

    # ----- 辅助 -----
    def _mark_recent(self, img_id: str) -> None:
        with self._lock:
            self._recent.append(img_id)


pool = ImagePool(CACHE_DIR, POOL_SIZE, CACHE_MAX_FILES)


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


def check_origin(request: Request) -> None:
    """白名单校验，不通过直接 403。"""
    req_origin = get_request_origin(
        request.headers.get("referer"), request.headers.get("origin")
    )
    if not origin_allowed(req_origin, loader.origins):
        raise HTTPException(status_code=403, detail="origin not allowed")


# ---------- 应用 ----------
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时后台预热热池（不阻塞启动；预热失败也无妨，首个请求会兜底同步下载）
    pool.refill_async()
    yield


app = FastAPI(title="bing-img-proxy", lifespan=lifespan)


@app.get("/api/bg/health")
def health() -> dict:
    return {"status": "ok", "image_count": len(loader.image_ids)}


@app.get("/api/bg/pool")
def pool_status() -> dict:
    """图片池与磁盘缓存状态，便于运维观测（无需进容器即可核对 LRU 上限是否生效）。"""
    return {
        "pool_size": pool.size,
        "ready": pool.ready_count(),
        "cache_files": pool.cache_file_count(),
        "cache_max_files": pool.cache_max_files,
        "resize_params": BING_IMAGE_PARAMS,
    }


def handle_random(request: Request) -> RedirectResponse:
    if not loader.image_ids:
        raise HTTPException(status_code=503, detail="no image ids configured")

    check_origin(request)

    # 若携带 seed 则确定性选图（同一 seed 永远映射到同一张图）；否则保持原有随机行为。
    # 注意：本接口是 302 跳转，响应本身仍标 no-store，行为与改造前完全一致。
    img_id = pick_image_id(request.query_params.get("seed"))
    if img_id is None:
        raise HTTPException(status_code=503, detail="no image ids configured")

    url = build_upstream_url(img_id, resize=RESIZE_ON_REDIRECT)
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
      - URL 与图片一一对应（seed 决定 id）→ 切换菜单命中浏览器缓存，不再回源，从而消除白屏。
    """
    if not loader.image_ids:
        raise HTTPException(status_code=503, detail="no image ids configured")

    check_origin(request)

    seed = request.query_params.get("seed")
    img_id = pick_image_id(seed)
    if img_id is None:
        raise HTTPException(status_code=503, detail="no image ids configured")

    picked = pool.get(img_id)
    if picked is None:
        raise HTTPException(status_code=502, detail="failed to fetch image from upstream")
    path, ctype, real_id = picked

    # 取走一张后立刻异步补货，使热池恢复到 POOL_SIZE（对前端表现为「秒回」）
    pool.refill_async()

    headers = {"X-Bing-Image-Id": real_id}
    if seed:
        # 带 seed → URL 与图片一一对应，可以放心强缓存：
        # 同一 seed 的 URL 在浏览器端直接命中，不再回源 → 切换菜单零延迟。
        headers["Cache-Control"] = f"public, max-age={POOL_CACHE_MAX_AGE}, immutable"
    else:
        # 不带 seed → 每次都是随机图，URL 却稳定，若允许缓存会把「随机」冻结成「固定」。
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    return FileResponse(path, media_type=ctype, headers=headers)


app.add_api_route(ROUTE_PATH, handle_random, methods=["GET"])

# 图片池接口独立注册（默认 /api/bg/image），与原 ROUTE_PATH 互不影响
if POOL_ROUTE_PATH != ROUTE_PATH:
    app.add_api_route(POOL_ROUTE_PATH, handle_pool_image, methods=["GET"])
