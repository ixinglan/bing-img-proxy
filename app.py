"""
bing-img-proxy
读取配置文件中的 Bing 图片 id，随机挑一个，302 重定向到：
    <BING_BASE_URL> + <id>
并对请求来源（Origin / Referer）做白名单校验。

配置（均可用环境变量覆盖）：
    CONFIG_DIR     配置目录，容器默认 /app/config
    IMAGE_IDS_FILE 图片 id 文件，默认 <CONFIG_DIR>/image_ids.txt
    ORIGINS_FILE   来源白名单文件，默认 <CONFIG_DIR>/origins.txt
    BING_BASE_URL  重定向前缀，默认 https://cn.bing.com/th?id=
    ROUTE_PATH     对外随机图路径，默认 /
"""
import os
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse

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
app = FastAPI(title="bing-img-proxy")


@app.get("/api/bg/health")
def health() -> dict:
    return {"status": "ok", "image_count": len(loader.image_ids)}


def handle_random(request: Request) -> RedirectResponse:
    if not loader.image_ids:
        raise HTTPException(status_code=503, detail="no image ids configured")

    req_origin = get_request_origin(
        request.headers.get("referer"), request.headers.get("origin")
    )
    if not origin_allowed(req_origin, loader.origins):
        raise HTTPException(status_code=403, detail="origin not allowed")

    img_id = secrets.choice(loader.image_ids)
    url = f"{BING_BASE_URL}{img_id}"
    # 防缓存：每次都随机，不被浏览器 / CDN 缓存
    return RedirectResponse(
        url,
        status_code=302,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


app.add_api_route(ROUTE_PATH, handle_random, methods=["GET"])
