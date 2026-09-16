"""
bing-img-proxy
读取配置文件中的 Bing 图片 id，随机挑一个，302 重定向到：
    <BING_BASE_URL> + <id>
并对请求来源（Origin / Referer）做白名单校验。

另提供一个「图片池」接口（默认 /api/bg/image）：
不做 302 跳转，而是把 Bing 图片的字节直接返回给浏览器，并带强缓存头
（Cache-Control: public, max-age=..., immutable）；服务端维护一个小型磁盘缓存 + 热池，
命中缓存时毫秒级返回，未命中才回源下载，下载完立即在后台补货。

要让「切换菜单不再白屏」真正成立，下面四件事必须同时满足，缺一不可：

  1) 确定性映射：同一个 ?seed= 永远解析到同一个 image_id。
     前端 URL 因此是稳定的 → 浏览器 HTTP 缓存才能真正命中 → 切换页面 0 请求。
     （反面教材：服务端若对同一 seed 随机返回不同图，浏览器缓存就失去意义。）

  2) 磁盘缓存有上限：LRU 淘汰，文件对数不超过 CACHE_MAX_FILES。
     否则随着 ?seed= 的多样性增长，image_ids.txt 里的 id 会被逐个下载并永久留在磁盘上。
     注意：磁盘上限 ≠ 热池大小。热池 POOL_SIZE 是「预热几张」，磁盘上限是「最多留几份」。

  3) 回源缩图：对新接口默认追加 w/h/c/rs 参数（原 302 接口默认保持原样）。
     尺寸必须按「实际设备像素」来定，而不是按 CSS 像素。

     实测（2026-09-16，同一张 UHD 原图）：
       不传参数        3,679,107 字节 / 16.6s  实际 3840x2160
       w=1920&h=1080     296,908 字节 /  0.6s  实际 1920x1080
       w=2560&h=1440     513,973 字节 /  1.2s  实际 2560x1440
       w=3840&h=2160   1,126,953 字节 /  1.2s  实际 3840x2160
     两个关键结论：
       a) 指定 w/h 会让 Bing 用更高效的编码重出图：同为 3840x2160，原图 3.68MB，
          加参数后 1.13MB（3.3 倍差距），且 1:1 裁切对比肉眼看不出画质损失；
       b) 下载耗时与尺寸基本无关（瓶颈在 Bing 端处理），所以「放大尺寸」几乎不额外花钱。

     为什么默认给到 3840x2160：背景是 `background-size: cover` 铺满整个视口，
     需要的是「视口 CSS 尺寸 x devicePixelRatio」那么多设备像素。
     实测本机为 Liquid Retina XDR 3024x1964（逻辑 1512x982、DPR 2）：
       1920x1080 会被浏览器放大 1.575 倍 -> 明显发虚（这就是「太模糊」的根因）
       2560x1440 仍需放大 1.36 倍   -> 仍略虚
       3200x1800 需放大 1.09 倍     -> 临界
       3840x2160 是缩小 0.91 倍     -> 清晰，且能覆盖到 4K 屏
     若你的访客以手机为主，可把 BING_IMAGE_PARAMS 调小以省流量（手机视口小得多）。

  4) 失败必须兜底：单个 id 回源失败（Bing 侧已失效 / 瞬时抖动）不能让背景空着。
     策略是「重试 → 校验 → 失败则换一张能用的图」，并把反复失败的 id 临时拉黑。

配置（均可用环境变量覆盖）：
    CONFIG_DIR           配置目录，容器默认 /app/config
    IMAGE_IDS_FILE       图片 id 文件，默认 <CONFIG_DIR>/image_ids.txt
    ORIGINS_FILE         来源白名单文件，默认 <CONFIG_DIR>/origins.txt
    BING_BASE_URL        回源前缀，默认 https://cn.bing.com/th?id=
    ROUTE_PATH           对外「随机重定向」路径，默认 /

    POOL_ROUTE_PATH      对外「图片池」路径，默认 /api/bg/image
    POOL_SIZE            服务端预热（常驻）的图片数量，默认 5
    CACHE_DIR            图片磁盘缓存目录，默认 cache
    CACHE_MAX_FILES      磁盘缓存文件对数上限（LRU），默认 POOL_SIZE*4（=20）
                         设为 0 表示不限制；过小会被自动抬到「保护集合」之上
    BING_IMAGE_PARAMS    回源缩图参数，默认 w=1920&h=1080&c=7&rs=1；置空则取原图
    RESIZE_ON_REDIRECT   是否让「原 302 接口」也缩图，默认 0（不改动原接口）
    POOL_CACHE_MAX_AGE   返回给浏览器的缓存秒数，默认 86400（1 天）
    DOWNLOAD_TIMEOUT     回源下载超时秒数，默认 10
    DOWNLOAD_ATTEMPTS    单个 id 的回源尝试次数（含首次），默认 2
    DOWNLOAD_RETRY_DELAY 重试之间的基础退避秒数（线性递增），默认 0.3
    MIN_IMAGE_BYTES      认为响应有效的字节下限，默认 512
    DEAD_ID_THRESHOLD    连续失败多少次后临时拉黑该 id，默认 3
    DEAD_ID_TTL          拉黑持续时间（秒），到期自动放出来重试，默认 3600
    REFILL_MIN_INTERVAL  两次后台补货之间的最小间隔秒数，默认 5
    REFILL_MAX_INTERVAL  连续补货无进展时的退避上限秒数，默认 60
    UPSTREAM_USER_AGENT  回源时的 User-Agent
    LOG_LEVEL            日志级别，默认 INFO
"""
import os
import hashlib
import logging
import secrets
import threading
import time
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, Response


# ---------- 环境变量解析（容错版） ----------
# 说明：直接用 int(os.getenv(...)) 时，只要有人把变量写成 "5sp" 或留了奇怪的字符，
# 进程会在 import 阶段就抛 ValueError 起不来，且报错完全看不出是哪个变量的问题。
# 这里统一包一层：解析失败就打日志并回落到默认值，保证服务总能起来。
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _bootstrap_logger.warning("环境变量 %s=%r 不是整数，回落到默认值 %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        _bootstrap_logger.warning("环境变量 %s=%r 不是数字，回落到默认值 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_bootstrap_logger = logging.getLogger("bing-img-proxy")


def _setup_logging() -> None:
    """配置日志。

    gunicorn/uvicorn 一般已经配好 root handler，此时不要抢（否则日志会重复或格式错乱）；
    只有在「裸跑」时才自己挂一个 handler。无论哪种情况，WARNING 及以上都一定能进容器日志，
    这正是修掉「回源失败被静默吞掉、线上 502 查不出原因」的关键。
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )


_setup_logging()
logger = logging.getLogger("bing-img-proxy")


# ---------- 配置 ----------
CONFIG_DIR = os.getenv("CONFIG_DIR", "config")
IMAGE_IDS_FILE = os.getenv("IMAGE_IDS_FILE", os.path.join(CONFIG_DIR, "image_ids.txt"))
ORIGINS_FILE = os.getenv("ORIGINS_FILE", os.path.join(CONFIG_DIR, "origins.txt"))
BING_BASE_URL = os.getenv("BING_BASE_URL", "https://cn.bing.com/th?id=").rstrip()

# Bing 的 th 接口支持 w/h/c/rs 参数按需缩放：不传就是原始尺寸（可能是 UHD 大图）。
# 前置的 "&" 由 build_upstream_url 统一拼接，这里只存 "k=v&k=v" 形式。
# lstrip("&?") 是为了容错：有人习惯把参数写成 "?w=100" 或 "&w=100"，
# 若只去掉 "&"，"?w=100" 会拼出 "...jpg&?w=100" 这种畸形 URL。
#
# 默认取 3840x2160 而不是 1920x1080：背景是 cover 铺满视口，需要
# 「视口 CSS 尺寸 x DPR」个设备像素；在 Retina（DPR 2）上 1080p 会被放大 1.5 倍以上而发虚。
# 实测同为 3840x2160，指定 w/h 后比原图小 3.3 倍且肉眼看不出画质损失，下载耗时也几乎不变，
# 所以「取大」几乎没有额外代价。详见模块开头 docstring 的实测表。
BING_IMAGE_PARAMS = os.getenv("BING_IMAGE_PARAMS", "w=3840&h=2160&c=7&rs=1").strip().lstrip("&?")

# 磁盘缓存文件名的「盐」：把取图参数拼进 key。
# 为什么必须有这一层：磁盘缓存原本只按 image_id 命名，一旦改了 BING_IMAGE_PARAMS，
# 旧文件仍会被命中，新参数永远不生效（表现为「改了参数没反应」）；
# 若历史上用不同参数跑过，同一个 id 的两种尺寸还会互相覆盖。
# 加盐后：新参数生成新 key，旧文件变成「孤儿」，会被 LRU 按 mtime 优先淘汰，自愈清理。
CACHE_KEY_SALT = BING_IMAGE_PARAMS or "raw"

# 是否把缩图参数也用在「原 302 接口」的跳转目标上。
# 默认关闭：需求明确要求「原 302 接口不动」，缩图只作用于新接口 /api/bg/image。
RESIZE_ON_REDIRECT = _env_bool("RESIZE_ON_REDIRECT", False)

# 处理 ROUTE_PATH 环境变量，确保路径格式规范
_route = os.getenv("ROUTE_PATH", "/") or "/"
if not _route.startswith("/"):
    _route = "/" + _route
ROUTE_PATH = _route.rstrip("/") if _route != "/" else "/"

# ---------- 图片池相关配置 ----------
POOL_SIZE = max(1, _env_int("POOL_SIZE", 5))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")

# 磁盘缓存上限：默认给热池的 4 倍，既留足余量给「访客各自 seed 映射到的图」，
# 又能保证淘汰逻辑有空隙可做（可淘汰数量必须为正，否则永远淘汰不动）。
#
# 下限保护：淘汰时受保护的 id 最多有 POOL_SIZE(热池) + POOL_SIZE(最近返回) = 2*POOL_SIZE 个。
# 若上限被设得比它还小，淘汰会永远收敛不到目标，等于悄悄退化成「无上限」。
# 这里自动抬到 3*POOL_SIZE，并打日志告知，避免这种静默失效。
CACHE_MAX_FILES = _env_int("CACHE_MAX_FILES", POOL_SIZE * 4)
if CACHE_MAX_FILES != 0 and CACHE_MAX_FILES < POOL_SIZE * 3:
    _floor = POOL_SIZE * 3
    logger.warning(
        "CACHE_MAX_FILES=%s 过小（热池+最近返回的保护集合最多 %s 项），已自动抬到 %s；设 0 表示不限制",
        CACHE_MAX_FILES, POOL_SIZE * 2, _floor,
    )
    CACHE_MAX_FILES = _floor

POOL_CACHE_MAX_AGE = _env_int("POOL_CACHE_MAX_AGE", 86400)
DOWNLOAD_TIMEOUT = _env_float("DOWNLOAD_TIMEOUT", 10.0)
DOWNLOAD_ATTEMPTS = max(1, _env_int("DOWNLOAD_ATTEMPTS", 2))
DOWNLOAD_RETRY_DELAY = max(0.0, _env_float("DOWNLOAD_RETRY_DELAY", 0.3))
MIN_IMAGE_BYTES = max(1, _env_int("MIN_IMAGE_BYTES", 512))
DEAD_ID_THRESHOLD = max(1, _env_int("DEAD_ID_THRESHOLD", 3))
DEAD_ID_TTL = max(1, _env_int("DEAD_ID_TTL", 3600))
REFILL_MIN_INTERVAL = max(0.0, _env_float("REFILL_MIN_INTERVAL", 5.0))
REFILL_MAX_INTERVAL = max(REFILL_MIN_INTERVAL, _env_float("REFILL_MAX_INTERVAL", 60.0))
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


def is_valid_image(data: bytes, ctype: str) -> bool:
    """判断一段字节是不是「可用的图片」。

    两个判据缺一不可：
      - content-type 必须以 image/ 开头：否则上游的 HTML 错误页 / 网关提示页会被当成
        图片写进缓存并长期复用（浏览器侧表现为背景裂图，且磁盘上留着一个坏文件）；
      - 字节数不小于 MIN_IMAGE_BYTES：拦住空响应与占位小图。

    这个函数被 download_image（回源出口）和 _ensure_cached（写盘边界）双重调用，
    保证「不论字节从哪来」都不会有非法内容进缓存。
    """
    if not data or len(data) < MIN_IMAGE_BYTES:
        return False
    return ctype.split(";")[0].strip().lower().startswith("image/")


def download_image(img_id: str) -> tuple[bytes, str] | None:
    """回源下载一张 Bing 图片。

    成功返回 (图片字节, content-type)；任何失败都返回 None，交由调用方决定是否重试。

    与改造前的区别（这是修掉「线上 50% 的 502 查不出原因」的关键）：
      - 失败会记日志：id、耗时、异常类型与内容，一眼能看出是超时 / 429 / 404 / 连接重置；
      - 会校验响应是不是「真图片」：content-type 必须以 image/ 开头、字节数不能过小。
        否则上游返回的 HTML 错误页 / 占位图会被当成正常图片写进磁盘缓存并长期复用。
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
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            data = resp.read()
            status = getattr(resp, "status", 200)
            raw_type = resp.headers.get("Content-Type") or ""
    except Exception as exc:
        logger.warning(
            "回源失败 id=%s 耗时=%.2fs 异常=%s: %s",
            img_id, time.monotonic() - started, type(exc).__name__, exc,
        )
        return None

    ctype = raw_type.split(";")[0].strip().lower() or "image/jpeg"
    if status != 200:
        logger.warning("回源非 200 id=%s status=%s", img_id, status)
        return None
    if not is_valid_image(data, ctype):
        logger.warning(
            "回源内容不是有效图片 id=%s content-type=%r 字节=%s（下限 %s）",
            img_id, raw_type, len(data), MIN_IMAGE_BYTES,
        )
        return None

    logger.debug("回源成功 id=%s 字节=%s type=%s 耗时=%.2fs", img_id, len(data), ctype, time.monotonic() - started)
    return data, ctype


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

    四个组成部分：
      1. 磁盘缓存：<CACHE_DIR>/<sha1(id)>.img 存图片字节、<sha1(id)>.type 存 content-type。
         同一 id 的内容固定，可长期复用；多个 gunicorn worker 之间共享同一份磁盘缓存，
         进程重启后也依然命中（不需要重新回源）。文件对数由 CACHE_MAX_FILES 做 LRU 上限。
      2. 热池    ：每个 worker 进程各自维护一个「已就绪 id」队列 deque，长度上限 POOL_SIZE。
         worker 之间不共享内存，但共享磁盘，所以任一 worker 下过的图，另一个也能直接命中。
      3. 补货    ：取走或失败后按节流间隔补货，使热池恢复到 POOL_SIZE。
      4. 坏 id 拉黑：连续失败 DEAD_ID_THRESHOLD 次的 id 临时拉黑 DEAD_ID_TTL 秒，
         期间不再为它浪费回源请求，到期自动放出来重试。
    """

    def __init__(self, cache_dir: str, size: int, max_files: int):
        self.cache_dir = cache_dir
        self.size = max(1, int(size))
        self.cache_max_files = max(0, int(max_files))
        self._lock = threading.Lock()
        self._evict_lock = threading.Lock()  # 淘汰过程串行化，避免并发 listdir/remove 打架
        self._ready: deque[str] = deque()   # 已就绪（本地可取）的 image_id
        self._inflight: set[str] = set()    # 正在下载中的 image_id，避免重复下载
        self._recent: deque[str] = deque(maxlen=max(1, int(size)))  # 最近返回过的 id
        self._refilling = False             # 是否已有后台补货线程在跑
        # 补货节流：每次补货最多发起 size*3 次回源，若不加节流，密集请求会把回源放大成风暴，
        # 极易被上游限流（这正是「冷启动 502 率偏高」的一个放大器）。
        self._last_refill_at = 0.0
        self._refill_fail_streak = 0
        # 坏 id 记账：id -> 连续失败次数；id -> 拉黑到期时间（time.monotonic 基准）
        self._failures: dict[str, int] = {}
        self._dead: dict[str, float] = {}
        os.makedirs(self.cache_dir, exist_ok=True)

    # ----- 查询 -----
    def ready_count(self) -> int:
        with self._lock:
            return len(self._ready)

    def dead_count(self) -> int:
        with self._lock:
            return len(self._dead)

    def cache_file_count(self) -> int:
        """当前磁盘缓存里已落盘的图片份数（用于运维观测 LRU 上限是否生效）。"""
        try:
            return sum(1 for n in os.listdir(self.cache_dir) if n.endswith(".img"))
        except OSError:
            return 0

    # ----- 磁盘缓存 -----
    def _key(self, img_id: str) -> str:
        """image_id -> 缓存文件名主干（sha1，规避路径注入与超长文件名）。

        key = sha1(image_id + "|" + 取图参数)：把参数纳入身份的一部分，
        否则改了 BING_IMAGE_PARAMS 之后旧缓存仍会被命中，改动等于白改。
        """
        return hashlib.sha1(f"{img_id}|{CACHE_KEY_SALT}".encode("utf-8")).hexdigest()

    def _cache_paths(self, img_id: str) -> tuple[str, str]:
        key = self._key(img_id)
        return (
            os.path.join(self.cache_dir, key + ".img"),
            os.path.join(self.cache_dir, key + ".type"),
        )

    def _read_cached(self, img_id: str) -> tuple[bytes, str] | None:
        """把缓存中的图**读成字节**返回。

        为什么读成字节而不是返回文件路径（原实现返回路径 + FileResponse）：
          多 worker 共享同一个缓存目录，A 进程正在返回某个文件时，B 进程的 LRU 淘汰
          完全可能把它删掉，FileResponse 随后打开就失败（偶发 500）。
          先把字节读进内存，后续谁删文件都与本次响应无关，从根上消除这个竞态。
          代价是每请求约几百 KB 的内存，对背景图这种尺寸可以忽略。
        """
        data_path, type_path = self._cache_paths(img_id)
        try:
            with open(data_path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if not data:
            return None
        ctype = "image/jpeg"
        try:
            with open(type_path, "r", encoding="utf-8") as f:
                ctype = f.read().strip() or ctype
        except OSError:
            pass
        return data, ctype

    def _write_cached(self, img_id: str, data: bytes, ctype: str) -> bool:
        """原子写盘：先写临时文件再 os.replace，避免别的请求读到写了一半的内容。"""
        data_path, type_path = self._cache_paths(img_id)
        tmp_path = f"{data_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, data_path)
            with open(type_path, "w", encoding="utf-8") as f:
                f.write(ctype)
        except OSError as exc:
            logger.warning("写缓存失败 id=%s 异常=%s: %s", img_id, type(exc).__name__, exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False
        return True

    # ----- 坏 id 拉黑 -----
    def _is_dead(self, img_id: str) -> bool:
        """该 id 是否在拉黑期内（到期会自动放出来重试）。"""
        with self._lock:
            until = self._dead.get(img_id)
            if until is None:
                return False
            if time.monotonic() >= until:
                self._dead.pop(img_id, None)
                self._failures.pop(img_id, None)
                logger.info("坏 id 拉黑到期，重新启用：%s", img_id)
                return False
            return True

    def _note_failure(self, img_id: str) -> None:
        with self._lock:
            n = self._failures.get(img_id, 0) + 1
            self._failures[img_id] = n
            if n >= DEAD_ID_THRESHOLD and img_id not in self._dead:
                self._dead[img_id] = time.monotonic() + DEAD_ID_TTL
                logger.warning(
                    "id 连续失败 %s 次，拉黑 %s 秒：%s", n, DEAD_ID_TTL, img_id,
                )
                return
        if n < DEAD_ID_THRESHOLD:
            logger.info("id 失败计数 %s/%s：%s", n, DEAD_ID_THRESHOLD, img_id)

    def _note_success(self, img_id: str) -> None:
        with self._lock:
            self._failures.pop(img_id, None)
            self._dead.pop(img_id, None)

    # ----- 磁盘上限（LRU 淘汰） -----
    def _evict_if_needed(self, protect: set[str] | None = None) -> None:
        """把磁盘缓存的文件对数压回 CACHE_MAX_FILES 以内。

        淘汰策略：按 mtime 由旧到新删除，但**永不删除**下面几类：
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
            removed = 0
            for _, k in evictable[:need]:
                for suffix in (".img", ".type"):
                    try:
                        os.remove(os.path.join(self.cache_dir, k + suffix))
                    except OSError:
                        pass
                removed += 1
            if removed:
                logger.debug("LRU 淘汰 %s 份，当前 %s 份（上限 %s）", removed, len(keys) - removed, self.cache_max_files)

    def _prune_ready(self) -> int:
        """剔除热池里「文件已经不在磁盘上」的 id。

        多 worker 下，另一个 worker 的淘汰可能删掉本 worker 记录为「就绪」的文件。
        不清掉的话，ready 计数会虚高，补货判断也会被误导。
        """
        with self._lock:
            stale = [i for i in self._ready if not os.path.exists(self._cache_paths(i)[0])]
            for i in stale:
                try:
                    self._ready.remove(i)
                except ValueError:
                    pass
        if stale:
            logger.info("热池清理失效条目 %s 个", len(stale))
        return len(stale)

    # ----- 取图（按 id，确定性） -----
    def _ensure_cached(self, img_id: str) -> tuple[bytes, str] | None:
        """确保该 id 的图片可用：命中磁盘直接返回，否则回源下载（带重试）并落盘。"""
        hit = self._read_cached(img_id)
        if hit:
            return hit
        if self._is_dead(img_id):
            # 已知坏 id，不再浪费回源请求（拉黑到期后会自动放出来）
            return None

        result = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            result = download_image(img_id)
            if result is not None:
                break
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_DELAY * attempt)  # 线性退避，避开瞬时抖动
        if result is None:
            self._note_failure(img_id)
            return None

        data, ctype = result
        # 写盘边界再校验一次（防御性）：无论字节是从哪个路径来的，都不允许把非图片写进缓存。
        # 回源出口已经校验过一次，这里是第二道关卡，保证「缓存里只有真图片」这个不变量。
        if not is_valid_image(data, ctype):
            logger.warning("拒绝写入非法图片 id=%s type=%r 字节=%s", img_id, ctype, len(data))
            self._note_failure(img_id)
            return None
        if not self._write_cached(img_id, data, ctype):
            return None
        self._note_success(img_id)
        # 写盘成功后立刻做一次上限检查；protect 保证「刚写好的这张」不会被自己淘汰掉
        self._evict_if_needed(protect={self._key(img_id)})
        return data, ctype

    def _fallback(self, exclude: str | None = None) -> tuple[bytes, str, str] | None:
        """兜底取图：交不出指定的 id 时，换一张「能用的」返回。

        顺序：热池与最近返回过的 id → 配置里的全部 id（命中磁盘缓存即可）。
        为什么要这么做：某个 id 在 Bing 侧失效是常态（实测存在稳定失败的 id），
        若坚持返回 502，用户看到的就是「背景直接不出来」——比换一张图糟糕得多。

        代价（明确的取舍）：同一个 seed 的 URL 在不同浏览器上可能落到不同图（各自下载成败不同）。
        但响应带 immutable 强缓存，同一个浏览器在 24h 内看到的图是稳定的，用户无感；
        且 URL↔图片一一对应的关系只在「该 id 可用」时严格成立，这属于可接受的降级。
        """
        with self._lock:
            preferred = list(self._ready) + list(self._recent)

        for img_id in preferred + loader.image_ids:
            if not img_id or img_id == exclude:
                continue
            # 先做一次轻量的存在性检查再读文件：候选可能上百个，
            # 如果每张都整个读进内存，兜底路径会白白产生上百次全文件读取。
            if not os.path.exists(self._cache_paths(img_id)[0]):
                continue
            hit = self._read_cached(img_id)
            if hit:
                self._mark_recent(img_id)
                logger.info("兜底命中：请求 id=%s 不可用，改用 id=%s", exclude, img_id)
                return hit[0], hit[1], img_id
        return None

    def get(self, img_id: str) -> tuple[bytes, str, str, bool] | None:
        """取指定 id 的图片。

        返回 (图片字节, content-type, 实际使用的 image_id, 是否走了兜底)；
        连兜底都拿不到（缓存全空且回源全失败）才返回 None，由调用方转成 502。
        """
        hit = self._ensure_cached(img_id)
        if hit is not None:
            # 若该 id 恰好在热池里，先把它取出（它马上要被消费掉，由 refill 再补上）
            with self._lock:
                try:
                    self._ready.remove(img_id)
                except ValueError:
                    pass
            self._mark_recent(img_id)
            return hit[0], hit[1], img_id, False

        logger.warning("指定图不可用，尝试兜底：请求 id=%s", img_id)
        fb = self._fallback(exclude=img_id)
        if fb is None:
            logger.error("兜底也失败（缓存为空且回源不可用）：请求 id=%s", img_id)
            return None
        return fb[0], fb[1], fb[2], True

    # ----- 补货 -----
    def _candidates(self) -> list[str]:
        """可用于补货的候选 id。

        优先选「不在热池里、不在下载中、不在最近返回列表里、也没被拉黑」的；
        若候选被穷尽，则逐级放宽条件，保证始终返回非空候选（除非 id 列表本身为空）。
        """
        ids = loader.image_ids
        if not ids:
            return []
        with self._lock:
            ready = set(self._ready)
            inflight = set(self._inflight)
            recent = set(self._recent)
            dead = {
                i for i, until in self._dead.items() if time.monotonic() < until
            }
        free = [
            i for i in ids
            if i not in ready and i not in inflight and i not in recent and i not in dead
        ]
        if free:
            return free
        relaxed = [i for i in ids if i not in ready and i not in inflight and i not in dead]
        if relaxed:
            return relaxed
        # 全部被拉黑/在池里：仍然返回原始列表，避免池子彻底无法补货（拉黑到期会自愈）
        return list(ids)

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

    def refill(self) -> int:
        """把热池补到 POOL_SIZE，返回本次成功补进池的张数。

        每轮并发补若干张（最多 3 张），加快冷启动预热；带尝试上限，
        避免个别 id 长期下载失败时死循环。
        """
        self._prune_ready()
        with self._lock:
            before = len(self._ready)
        limit = max(self.size * 3, 9)
        attempts = 0
        while attempts < limit:
            with self._lock:
                need = self.size - len(self._ready)
            if need <= 0:
                break
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
        with self._lock:
            return len(self._ready) - before

    def refill_async(self) -> None:
        """后台线程补货。

        双重节流，防止把回源放大成风暴（原实现每个请求都会触发一次、且失败时最多
        连续发起 size*3 次回源）：
          1) 同一时刻只允许一个补货线程（_refilling）；
          2) 两次补货之间至少间隔 REFILL_MIN_INTERVAL 秒；若上一轮毫无进展，
             间隔按 2 的幂退避，最多 REFILL_MAX_INTERVAL 秒。
        """
        with self._lock:
            if self._refilling:
                return
            now = time.monotonic()
            interval = min(REFILL_MIN_INTERVAL * (2 ** min(self._refill_fail_streak, 4)), REFILL_MAX_INTERVAL)
            if now - self._last_refill_at < interval:
                return
            self._refilling = True
            self._last_refill_at = now

        def _job() -> None:
            added = 0
            try:
                added = self.refill()
            except Exception as exc:  # 后台线程绝不能把异常抛出去
                logger.warning("补货线程异常：%s: %s", type(exc).__name__, exc)
            finally:
                with self._lock:
                    self._refilling = False
                    if added > 0:
                        self._refill_fail_streak = 0
                    else:
                        self._refill_fail_streak = min(self._refill_fail_streak + 1, 4)

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
    logger.info(
        "启动：id 数=%s 热池=%s 磁盘上限=%s 缩图参数=%r 302缩图=%s 回源尝试=%s次",
        len(loader.image_ids), POOL_SIZE, CACHE_MAX_FILES,
        BING_IMAGE_PARAMS, RESIZE_ON_REDIRECT, DOWNLOAD_ATTEMPTS,
    )
    pool.refill_async()
    yield


app = FastAPI(title="bing-img-proxy", lifespan=lifespan)


@app.get("/api/bg/health")
def health() -> dict:
    return {"status": "ok", "image_count": len(loader.image_ids)}


@app.get("/api/bg/pool")
def pool_status() -> dict:
    """图片池与磁盘缓存状态，便于运维观测（无需进容器即可核对上限是否生效）。

    注意：`ready` 是**单个 worker 进程**的就绪数，多 worker 下按进程各算各的，
    所以响应里带上 pid，避免把「两次请求返回不同 ready」误读成池子不稳。
    """
    pool._prune_ready()
    return {
        "pid": os.getpid(),
        "pool_size": pool.size,
        "ready": pool.ready_count(),
        "cache_files": pool.cache_file_count(),
        "cache_max_files": pool.cache_max_files,
        "dead_ids": pool.dead_count(),
        "resize_params": BING_IMAGE_PARAMS,
        "download_attempts": DOWNLOAD_ATTEMPTS,
    }


def handle_random(request: Request) -> RedirectResponse:
    check_origin(request)

    # 若携带 seed 则确定性选图（同一 seed 永远映射到同一张图）；否则保持原有随机行为。
    # 注意：本接口是 302 跳转，响应本身仍标 no-store，行为与改造前完全一致。
    # id 列表为空时 pick_image_id 返回 None，统一在这里转 503（此前有两处重复判断）。
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


def handle_pool_image(request: Request) -> Response:
    """图片池接口：直接返回同源图片字节 + 强缓存头，取走后异步补货。

    与原 302 接口的区别：
      - 响应体是图片本身（同源），可被浏览器磁盘缓存；
      - URL 与图片一一对应（seed 决定 id）→ 切换菜单命中浏览器缓存，不再回源，从而消除白屏。
      - 指定的 id 不可用时自动兜底换图（见 ImagePool._fallback），不让背景空着。
    """
    check_origin(request)

    seed = request.query_params.get("seed")
    img_id = pick_image_id(seed)
    if img_id is None:
        raise HTTPException(status_code=503, detail="no image ids configured")

    picked = pool.get(img_id)
    if picked is None:
        raise HTTPException(status_code=502, detail="failed to fetch image from upstream")
    data, ctype, real_id, fell_back = picked

    # 取走一张后立刻异步补货（内部有节流，不会把回源放大）
    pool.refill_async()

    headers = {
        "X-Bing-Image-Id": real_id,
        # ETag 用 id 的 sha1：同一 id 的内容固定，所以这个 ETag 是稳定的
        "ETag": f'"{pool._key(real_id)}"',
    }
    if fell_back:
        # 显式标记走了兜底，便于排查「为什么这个 seed 看到的不是它映射的图」
        headers["X-Bing-Image-Fallback"] = "1"
    if seed:
        # 带 seed → URL 与图片一一对应，可以放心强缓存：
        # 同一 seed 的 URL 在浏览器端直接命中，不再回源 → 切换菜单零延迟。
        headers["Cache-Control"] = f"public, max-age={POOL_CACHE_MAX_AGE}, immutable"
    else:
        # 不带 seed → 每次都是随机图，URL 却稳定，若允许缓存会把「随机」冻结成「固定」。
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    return Response(content=data, media_type=ctype, headers=headers)


app.add_api_route(ROUTE_PATH, handle_random, methods=["GET"])

# 图片池接口独立注册（默认 /api/bg/image），与原 ROUTE_PATH 互不影响
if POOL_ROUTE_PATH != ROUTE_PATH:
    app.add_api_route(POOL_ROUTE_PATH, handle_pool_image, methods=["GET"])
