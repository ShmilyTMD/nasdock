# -*- coding: utf-8 -*-
"""NASDock — 飞牛 NAS 容器自动发现主页

- 只读挂载 docker.sock，自动扫描容器：名称/图标/端口/状态/凭据(env)
- 凭据仅存储、默认隐藏，卡片上点锁图标按需查看/复制
- 自定义书签分组、壁纸（上传/URL/必应每日）、主题、搜索引擎
"""
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import docker
from flask import Flask, jsonify, request, send_from_directory, send_file

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
WALLPAPER_DIR = CONFIG_DIR / "wallpapers"
WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
ICONS_DIR = CONFIG_DIR / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)

OVERRIDES_FILE = CONFIG_DIR / "overrides.json"
BOOKMARKS_FILE = CONFIG_DIR / "bookmarks.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
AUTH_FILE = CONFIG_DIR / "auth.json"

SELF_NAME = os.getenv("HOSTNAME", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
# RLock：_write_json 内部也会取锁，端点持锁调用它会形成嵌套，必须可重入
_lock = threading.RLock()

# ---------------------------------------------------------------- 配置文件 IO

def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data) -> None:
    with _lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_token() -> str:
    env = (os.getenv("WEB_TOKEN") or "").strip()
    if env:
        return env
    data = _read_json(AUTH_FILE, {})
    if data.get("token"):
        return data["token"]
    token = secrets.token_urlsafe(18)
    _write_json(AUTH_FILE, {"token": token})
    return token


WEB_TOKEN = get_token()

DEFAULT_SETTINGS = {
    "theme": "dark",
    "engine": "baidu",
    "wallpaper": {"type": "builtin", "value": "aurora"},
    "title": "NASDock",
}


# ---------------------------------------------------------------- 书签名称清洗

NAME_NOISE = {"首页", "首頁", "种子", "種子", "主页", "主頁", "index", "home", "welcome", "欢迎", "歡迎", "论坛首页", "論壇首頁"}


def clean_link_name(name: str) -> str:
    """清洗浏览器书签的网页标题：去 Powered by 尾巴、去噪音词、统一分隔符"""
    n = (name or "").strip()
    n = re.sub(r"[-–—|｜：:\s]*[Pp]owered\s*[Bb]y\s+.*$", "", n).strip()
    parts = [p.strip(" -–—_|｜：:　\t") for p in re.split(r"::|：：", n)]
    parts = [p for p in parts if p]

    def strip_tokens(seg):
        words = [w for w in re.split(r"\s+", seg) if w.lower() not in NAME_NOISE]
        return " ".join(words)

    kept = [strip_tokens(p) for p in parts]
    kept = [p for p in kept if p]
    n = " ".join(kept) if kept else n
    n = re.sub(r"[-–—_|｜：:]+", " ", n)
    n = re.sub(r"\s{2,}", " ", n).strip(" -_|｜：:")
    return n or (name or "").strip()


# ---------------------------------------------------------------- Docker 扫描

_no_scan = threading.Event()
try:
    _client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    _client.ping()
except Exception:
    _client = None
    _no_scan.set()

CRED_NOISE = re.compile(
    r"^(PATH|HOME|HOSTNAME|TERM|LANG|LC_|GPG|PGDATA|POSTGRES?_(DB|USER|HOST|PORT)|TZ|UMASK|PUID|PGID|"
    r"PYTHON|PIP|PYTHONDONT|GALLERY|PLAYWRIGHT|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|CONFIG_DIR|WEB_TOKEN|"
    r"DB_(NAME|DATABASE)|NGINX_PORT|PORT|CHANNELS|CACHE|SHARD|ASYNC|GC_|CONCURRENCY|ENABLE_|AUTH_USERS|"
    r"MOVIEPILOT|PLUGIN_MARKET|SUPERUSER|DB_TYPE|DOCKER_HOST|SECRET$)", re.I)

CATEGORY_RULES = [
    ("media", ("jellyfin", "emby", "moviepilot", "plex", "navidrome", "audiobookshelf", "tidarr")),
    ("download", ("qbittorrent", "transmission", "savextube", "pansou", "go-music", "byte-muse",
                  "aria2", "yt-dlp", "music")),
    ("network", ("mihomo", "metacubexd", "nginx", "traefik", "proxy", "openresty", "adguard")),
    ("database", ("redis", "postgres", "mysql", "mariadb", "mongo", "minio")),
    ("tools", ("docker", "portainer", "dockercopilot", "mail", "hermes", "iopaint", "immortal")),
]


def categorize(name: str, image: str) -> str:
    blob = f"{name} {image}".lower()
    for cat, keys in CATEGORY_RULES:
        if any(k in blob for k in keys):
            return cat
    return "apps"


ENV_USER_RE = re.compile(r"(USER|USERNAME|ACCOUNT|SUPERUSER)", re.I)
ENV_PASS_RE = re.compile(r"(PASSWORD|PASSWD|PASS=|_PASS|TOKEN|SECRET|API_KEY|AUTH)", re.I)
ENV_VALUE_OK = re.compile(r"^.{3,80}$")


def extract_credentials(env_list):
    user, password, extras = "", "", []
    for item in env_list or []:
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value or not ENV_VALUE_OK.match(value):
            continue
        if CRED_NOISE.search(key):
            continue
        if key.endswith("_FILE") or "PROXY" in key.upper():
            continue
        ku = key.upper()
        if ENV_PASS_RE.search(ku):
            password = password or value
            extras.append({"key": key, "value": value})
        elif ENV_USER_RE.search(ku) and ku not in ("USERNAME",) or ku == "SUPERUSER":
            user = user or value
        elif ku in ("USERNAME", "ADMIN_USERNAME"):
            user = user or value
    return {"username": user, "password": password, "extras": extras[:4]}


def scan_containers():
    if _client is None:
        return []
    services = []
    try:
        containers = _client.containers.list(all)
        for c in containers:
            a = c.attrs
            name = (c.name or "").lstrip("/")
            # HOSTNAME 环境变量 = 容器短 ID，用它排除 nasdock 自身
            if name == SELF_NAME or a.get("Id", "").startswith(SELF_NAME):
                continue
            image = ""
            try:
                tags = c.image.tags
                image = tags[0] if tags else str(c.image.id)[:20]
            except Exception:
                pass
            state = a.get("State", {}).get("Status", "unknown")
            netmode = (a.get("HostConfig") or {}).get("NetworkMode", "")
            ports = []
            if netmode in ("host",) or str(netmode).startswith("container:"):
                exposed = ((a.get("Config") or {}).get("ExposedPorts") or {})
                ports = sorted({p.split("/")[0] for p in exposed if p.split("/")[0].isdigit()}, key=int)
            else:
                bindings = (a.get("HostConfig") or {}).get("PortBindings") or {}
                seen = set()
                for plist in bindings.values():
                    for pb in plist or []:
                        hp = pb.get("HostPort")
                        if hp and hp not in seen:
                            seen.add(hp)
                            ports.append(hp)
                ports = sorted(ports, key=lambda p: int(p) if p.isdigit() else 9999)
            env_creds = extract_credentials(a.get("Config", {}).get("Env") or [])
            services.append({
                "id": a.get("Id", "")[:12],
                "name": name,
                "image": image,
                "state": state,
                "ports": ports,
                "network_mode": netmode,
                "category": categorize(name, image),
                "icon": re.sub(r"[^a-z0-9\-]", "", name.lower()),
                "image_slug": re.sub(r"[^a-z0-9\-]", "", (image.split("/")[-1].split(":")[0].lower())),
                "credentials": env_creds,
            })
    except Exception as exc:
        app.logger.warning("docker scan failed: %s", exc)
    return services


# ---------------------------------------------------------------- API

def _authed():
    provided = (request.headers.get("X-Web-Token")
                or request.cookies.get("nasdock_token")
                or request.args.get("token") or "")
    return provided and secrets.compare_digest(provided, WEB_TOKEN)


@app.before_request
def _check_token():
    if request.path == "/api/health":
        return None
    if request.method in ("GET", "HEAD", "OPTIONS") and not request.path.startswith("/api/"):
        return None
    if _authed():
        return None
    return jsonify({"ok": False, "auth": "required", "error": "缺少或错误的访问令牌"}), 401


@app.get("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    return resp


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "docker": _client is not None})


@app.get("/api/data")
def api_data():
    services = scan_containers()
    overrides = _read_json(OVERRIDES_FILE, {})
    for s in services:
        ov = overrides.get(s["name"]) or {}
        for key in ("display_name", "url_override", "category"):
            if ov.get(key):
                s[key] = ov[key]
        if ov.get("credentials"):
            s["credentials"] = {**(s["credentials"] or {}), **ov["credentials"]}
        s["icon_override"] = bool(ov.get("icon_uploaded"))
        s["icon_ts"] = ov.get("icon_ts") or 0
        s["has_override"] = bool(ov)
    hidden = {n for n, ov in overrides.items() if ov.get("hide")}
    hidden_list = [{"name": s["name"], "image": s["image"], "state": s["state"]}
                   for s in services if s["name"] in hidden]
    services = [s for s in services if s["name"] not in hidden]
    services.sort(key=lambda s: (s["state"] != "running", s["category"], s["name"]))
    # 未命中本地缓存的图标入队，由后台线程抓取
    try:
        for s in services:
            enqueue_icon((s.get("icon") or s.get("image_slug") or ""))
    except Exception:
        pass
    bookmarks = _read_json(BOOKMARKS_FILE, [])
    settings = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}
    # 一次性清洗历史书签名称（浏览器导出的网页标题很脏）
    if not settings.get("names_cleaned_v2"):
        changed = False
        for g in bookmarks:
            for l in g.get("links") or []:
                new = clean_link_name(l.get("name", ""))
                if new and new != l.get("name"):
                    l["name"] = new
                    changed = True
        settings["names_cleaned_v2"] = True
        _write_json(SETTINGS_FILE, settings)
        if changed:
            _write_json(BOOKMARKS_FILE, bookmarks)
            app.logger.info("bookmarks names cleaned")
    if not settings.get("ai_seeded") and not any(g.get("name") == "AI" for g in bookmarks):
        bookmarks.append({
            "name": "AI",
            "links": [
                {"name": "ChatGPT", "url": "https://chatgpt.com/", "icon": "chatgpt"},
                {"name": "Gemini", "url": "https://gemini.google.com/app", "icon": "googlegemini"},
                {"name": "Grok", "url": "https://grok.com/", "icon": "grok"},
                {"name": "DeepSeek", "url": "https://chat.deepseek.com/", "icon": "deepseek"},
            ],
        })
        settings["ai_seeded"] = True
        _write_json(SETTINGS_FILE, settings)
        _write_json(BOOKMARKS_FILE, bookmarks)
    return jsonify({
        "ok": True,
        "services": services,
        "hidden": hidden_list,
        "bookmarks": bookmarks,
        "settings": settings,
    })


@app.post("/api/overrides/<name>")
def api_override(name):
    data = request.get_json(silent=True) or {}
    with _lock:
        overrides = _read_json(OVERRIDES_FILE, {})
        ov = overrides.get(name) or {}
        # 文本类覆盖：空字符串 = 清除覆盖、恢复自动行为
        for key in ("display_name", "url_override", "category"):
            if key in data:
                v = str(data.get(key) or "").strip()
                if v:
                    ov[key] = v
                else:
                    ov.pop(key, None)
        # 凭据：只合并非空值，避免误清自动扫描结果
        if isinstance(data.get("credentials"), dict):
            creds = ov.get("credentials") or {}
            for ck in ("username", "password"):
                v = str(data["credentials"].get(ck) or "").strip()
                if v:
                    creds[ck] = v
            ov["credentials"] = creds
        if "hide" in data:
            ov["hide"] = bool(data.get("hide"))
        if "icon_uploaded" in data:
            ov["icon_uploaded"] = bool(data.get("icon_uploaded"))
        overrides[name] = ov
        # 清掉完全空的覆盖记录
        clean_ov = {k: v for k, v in ov.items() if v not in ("", None, {}, [])}
        if clean_ov:
            overrides[name] = clean_ov
        else:
            overrides.pop(name, None)
        _write_json(OVERRIDES_FILE, overrides)
    return jsonify({"ok": True})


@app.post("/api/bookmarks")
def api_bookmarks():
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"ok": False, "error": "格式错误"}), 400
    clean = []
    for group in data:
        g = {"name": str(group.get("name") or "分组")[:30], "links": []}
        for link in group.get("links") or []:
            url = str(link.get("url") or "").strip()
            if not url:
                continue
            if not url.startswith("http"):
                url = "https://" + url
            g["links"].append({"name": str(link.get("name") or url)[:40], "url": url,
                               **({"icon": str(link.get("icon"))[:40]} if link.get("icon") else {})})
        clean.append(g)
    _write_json(BOOKMARKS_FILE, clean)
    return jsonify({"ok": True})


@app.post("/api/settings")
def api_settings():
    data = request.get_json(silent=True) or {}
    with _lock:
        settings = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}
        for key in ("theme", "engine", "title"):
            if key in data:
                settings[key] = str(data[key])[:40]
        if isinstance(data.get("wallpaper"), dict):
            wp = data["wallpaper"]
            settings["wallpaper"] = {
                "type": str(wp.get("type"))[:10],
                "value": str(wp.get("value"))[:300],
            }
        _write_json(SETTINGS_FILE, settings)
    return jsonify({"ok": True, "settings": settings})


@app.post("/api/wallpaper")
def api_wallpaper():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    suffix = Path(f.filename or "wp.jpg").suffix or ".jpg"
    if suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    name = "upload" + suffix.lower()
    f.save(WALLPAPER_DIR / name)
    return jsonify({"ok": True, "file": name})


@app.get("/api/wallpaper/<name>")
def api_wallpaper_get(name):
    if not re.match(r"^(upload|bing|rand|wh)\.(jpg|jpeg|png|webp)$", name):
        return jsonify({"ok": False}), 404
    p = WALLPAPER_DIR / name
    if not p.exists():
        return jsonify({"ok": False}), 404
    return send_file(p)


@app.post("/api/wallpaper/fetch")
def api_wallpaper_fetch():
    """从公开壁纸源抓一张：picsum（随机图）/ wallhaven（关键词搜索，免费 API）"""
    import httpx
    import random as _random
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    proxies = {"http://": os.getenv("HTTP_PROXY"), "https://": os.getenv("HTTP_PROXY")} if os.getenv("HTTP_PROXY") else None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        if source == "picsum":
            url = f"https://picsum.photos/1920/1080?random={int(time.time())}"
            r = httpx.get(url, follow_redirects=True, timeout=40, headers=headers, proxy=proxies)
            r.raise_for_status()
            if len(r.content) < 10000:
                raise IOError("图片内容异常")
            (WALLPAPER_DIR / "rand.jpg").write_bytes(r.content)
            return jsonify({"ok": True, "wallpaper": {"type": "local", "value": "rand.jpg"}})
        if source == "wallhaven":
            q = str(data.get("q") or "landscape").strip()[:60]
            r = httpx.get("https://wallhaven.cc/api/v1/search",
                          params={"q": q, "categories": "100", "purity": "100",
                                  "sorting": "random", "atleast": "1920x1080"},
                          timeout=25, headers=headers, proxy=proxies)
            r.raise_for_status()
            items = r.json().get("data") or []
            if not items:
                return jsonify({"ok": False, "error": "没有找到壁纸，换个关键词试试"}), 404
            img_url = _random.choice(items).get("path")
            img = httpx.get(img_url, follow_redirects=True, timeout=90, headers=headers, proxy=proxies)
            img.raise_for_status()
            (WALLPAPER_DIR / "wh.jpg").write_bytes(img.content)
            return jsonify({"ok": True, "wallpaper": {"type": "local", "value": "wh.jpg"}})
        return jsonify({"ok": False, "error": "未知来源"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 502


@app.get("/api/bing")
def api_bing():
    try:
        import httpx
        r = httpx.get("https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1", timeout=8,
                      headers={"User-Agent": "Mozilla/5.0"})
        url = r.json()["images"][0]["url"]
        return jsonify({"ok": True, "url": "https://cn.bing.com" + url})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


# ---------------------------------------------------------------- 浏览器书签导入

from html.parser import HTMLParser


class _NetscapeParser(HTMLParser):
    """解析浏览器导出的书签 HTML（Netscape 格式），收集 文件夹路径 → 链接列表"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.folders = {}
        self._in_h3 = False
        self._h3_text = ""
        self._pending_folder = None
        self._link = None

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self._in_h3 = True
            self._h3_text = ""
        elif tag == "a":
            d = dict(attrs)
            self._link = {"url": (d.get("href") or "").strip(), "name": ""}
        elif tag == "dl":
            if self._pending_folder is not None:
                self.stack.append(self._pending_folder)
                self._pending_folder = None

    def handle_endtag(self, tag):
        if tag == "h3":
            self._in_h3 = False
            self._pending_folder = (self._h3_text or "").strip() or "未命名文件夹"
        elif tag == "a":
            if self._link and self._link["url"].startswith(("http://", "https://")):
                path = " / ".join(self.stack) if self.stack else "（根目录）"
                leaf = self.stack[-1] if self.stack else "（根目录）"
                name = (self._link["name"] or self._link["url"]).strip()
                self.folders.setdefault((path, leaf), []).append({"name": name[:120], "url": self._link["url"]})
            self._link = None
        elif tag == "dl":
            if self.stack:
                self.stack.pop()

    def handle_data(self, data):
        if self._in_h3:
            self._h3_text += data
        elif self._link is not None:
            self._link["name"] += data


def _dedupe_links(links):
    seen, out = set(), []
    for l in links:
        if l["url"] in seen:
            continue
        seen.add(l["url"])
        out.append(l)
    return out


app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


# ---------------------------------------------------------------- 图标

_ICON_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_ICON_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_HREF_RE = re.compile(r"href\s*=\s*([\"'])(.*?)\1", re.I)
_SIZE_RE = re.compile(r"sizes\s*=\s*[\"'](\d+)x", re.I)


def _icon_path(key):
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".ico", ".svg", ".img"):
        p = ICONS_DIR / f"{key}{ext}"
        if p.exists():
            return p
    return None


def _save_icon(key, content):
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    old = _icon_path(key)
    if old:
        old.unlink(missing_ok=True)
    (ICONS_DIR / f"{key}.png").write_bytes(content)
    with _lock:
        overrides = _read_json(OVERRIDES_FILE, {})
        ov = overrides.get(key) or {}
        ov["icon_uploaded"] = True
        ov["icon_ts"] = int(time.time())
        overrides[key] = ov
        _write_json(OVERRIDES_FILE, overrides)


@app.post("/api/icon/upload")
def api_icon_upload():
    key = request.form.get("key", "")
    f = request.files.get("file")
    if not _KEY_RE.match(key) or not f:
        return jsonify({"ok": False, "error": "参数错误"}), 400
    suffix = Path(f.filename or "").suffix.lower()
    if suffix not in _ICON_MIME:
        suffix = ".png"
    content = f.read()
    if len(content) > 512 * 1024:
        return jsonify({"ok": False, "error": "图片超过 512KB"}), 400
    _save_icon(key, content)
    return jsonify({"ok": True, "url": f"/api/icon/{key}?v={int(time.time())}"})


@app.get("/api/icon/<key>")
def api_icon_get(key):
    if not _KEY_RE.match(key):
        return jsonify({"ok": False}), 404
    p = _icon_path(key)
    if not p:
        return jsonify({"ok": False}), 404
    return send_file(p, mimetype=_ICON_MIME.get(p.suffix, "application/octet-stream"), conditional=True)


def _probe_icon(url):
    """按 Homarr 同款思路找站点图标：页面声明图标 → favicon.ico → Google S2 → DuckDuckGo"""
    import httpx
    proxies = None
    if os.getenv("HTTP_PROXY"):
        proxies = {"http://": os.getenv("HTTP_PROXY"), "https://": os.getenv("HTTP_PROXY")}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
    with httpx.Client(follow_redirects=True, timeout=10, headers=headers, proxy=proxies) as client:
        try:
            r = client.get(url)
            final_url = str(r.url)
            import html as _html
            cands = []
            for m in _ICON_LINK_RE.finditer(r.text[:300000]):
                tag = m.group(0)
                low = tag.lower()
                if "icon" not in low:
                    continue
                hm = _HREF_RE.search(tag)
                if not hm:
                    continue
                href = _html.unescape(hm.group(2))
                if not href or href.startswith("data:"):
                    continue
                sm = _SIZE_RE.search(tag)
                score = 180 if "apple-touch" in low else (int(sm.group(1)) if sm else 32)
                cands.append((score, href))
            cands.sort(key=lambda x: -x[0])
            for _, href in cands[:3]:
                full = href if href.startswith("http") else final_url.rstrip("/") + "/" + href.lstrip("/")
                try:
                    ir = client.get(full)
                    if ir.status_code == 200 and not ir.content[:4].startswith(b"<!DO") and len(ir.content) > 100:
                        return ir.content, "页面声明图标"
                except Exception:
                    continue
        except Exception:
            pass
        try:
            from urllib.parse import urlparse
            p = urlparse(url if url.startswith("http") else "http://" + url)
            target = f"{p.scheme or 'http'}://{p.netloc}/favicon.ico"
            ir = client.get(target)
            if ir.status_code == 200 and not ir.content[:4].startswith(b"<!DO") and len(ir.content) > 100:
                return ir.content, "favicon.ico"
        except Exception:
            pass
        try:
            from urllib.parse import urlparse
            p = urlparse(url if url.startswith("http") else "http://" + url)
            ir = client.get(f"https://www.google.com/s2/favicons?domain={p.hostname}&sz=64")
            if ir.status_code == 200 and len(ir.content) > 100:
                return ir.content, "Google S2"
        except Exception:
            pass
        try:
            from urllib.parse import urlparse
            p = urlparse(url if url.startswith("http") else "http://" + url)
            ir = client.get(f"https://icons.duckduckgo.com/ip3/{p.hostname}.ico")
            if ir.status_code == 200 and len(ir.content) > 100:
                return ir.content, "DuckDuckGo"
        except Exception:
            pass
    return None, None


@app.post("/api/icon/find")
def api_icon_find():
    import base64
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    key = str(data.get("key") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "缺少 url"}), 400
    content, source = _probe_icon(url)
    if not content:
        return jsonify({"ok": False, "error": "未能找到该站点的图标"}), 404
    if key and _KEY_RE.match(key):
        _save_icon(key, content)
        return jsonify({"ok": True, "url": f"/api/icon/{key}?v={int(time.time())}", "source": source})
    b64 = base64.b64encode(content).decode()
    return jsonify({"ok": True, "data": f"data:image/png;base64,{b64}", "source": source})


# ---------------------------------------------------------------- 外部图标本地缓存

ICONCACHE_DIR = CONFIG_DIR / "iconcache"
ICONCACHE_DIR.mkdir(parents=True, exist_ok=True)
_icon_negative = {}          # slug -> 失败时间戳（TTL 内不重试）
_icon_lock = threading.Lock()
_ICON_RETRY_AFTER = 600      # 抓取失败 10 分钟内不重试（避免拖慢与限流）
_icon_warm_queue = set()     # 待后台抓取的 slug
_icon_wake = threading.Event()


def _icon_cached(slug):
    for suffix, min_size in ((".png", 120), (".svg", 120)):
        p = ICONCACHE_DIR / f"{slug}{suffix}"
        if p.exists() and p.stat().st_size > min_size:
            return True
    return False


def _icon_valid_slug(slug):
    return bool(re.match(r"^[a-z0-9][a-z0-9\-_.]{0,60}$", slug))


def enqueue_icon(slug):
    """图标未命中时入队，由后台线程抓取——请求路径永远不等待网络"""
    slug = (slug or "").strip().lower()
    if not _icon_valid_slug(slug) or _icon_cached(slug):
        return
    if time.time() - _icon_negative.get(slug, 0) < _ICON_RETRY_AFTER:
        return
    with _icon_lock:
        if slug not in _icon_warm_queue:
            _icon_warm_queue.add(slug)
            _icon_wake.set()


def _fetch_icon(slug):
    import httpx
    proxies = None
    if os.getenv("HTTP_PROXY"):
        proxies = {"http://": os.getenv("HTTP_PROXY"), "https://": os.getenv("HTTP_PROXY")}
    headers = {"User-Agent": "Mozilla/5.0"}
    base = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons"
    with httpx.Client(timeout=12, headers=headers, proxy=proxies, follow_redirects=True) as client:
        for suffix, min_size in ((".png", 120), (".svg", 200)):
            try:
                r = client.get(f"{base}/{suffix.strip('.')}/{slug}{suffix}")
                if r.status_code == 200 and len(r.content) > min_size:
                    (ICONCACHE_DIR / f"{slug}{suffix}").write_bytes(r.content)
                    return True
            except Exception:
                continue
    return False


def _icon_worker():
    while True:
        _icon_wake.wait()
        _t = time.time()
        with _icon_lock:
            batch = [s for s in _icon_warm_queue if _icon_cached(s) is False]
            _icon_warm_queue.clear()
        for slug in batch:
            try:
                if _icon_cached(slug):
                    continue
                if not _fetch_icon(slug):
                    _icon_negative[slug] = time.time()
            except Exception:
                _icon_negative[slug] = time.time()
            _t = time.time()
            while time.time() - _t < 0.5:  # 抓取间隔，防限流
                _t = _t
                time.sleep(0.5)
                break


threading.Thread(target=_icon_worker, daemon=True).start()


@app.get("/api/icache/<slug>")
def api_icache(slug):
    slug = slug.strip().lower()
    slug = re.sub(r"\.(png|svg)$", "", slug)  # 剥掉前端附加的扩展名
    if not _icon_valid_slug(slug):
        return jsonify({"ok": False}), 404
    for suffix, mime in ((".png", "image/png"), (".svg", "image/svg+xml")):
        p = ICONCACHE_DIR / f"{slug}{suffix}"
        if p.exists() and p.stat().st_size > 0:
            resp = send_file(p, mimetype=mime, conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp
    enqueue_icon(slug)  # 未命中：立即 404 让前端兜底，后台补缓存
    return jsonify({"ok": False}), 404


@app.post("/api/bookmarks/parse")

def api_bookmarks_parse():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "没有文件"}), 400
    raw = f.read().decode("utf-8", errors="replace")
    if "<!DOCTYPE NETSCAPE-Bookmark-file" not in raw[:200].upper() and "<DL>" not in raw.upper()[:2000]:
        # 仍然尝试解析（不同浏览器头部略有差异）
        if "<a " not in raw.lower():
            return jsonify({"ok": False, "error": "文件不是书签 HTML，请从浏览器书签管理器导出"}), 400
    parser = _NetscapeParser()
    try:
        parser.feed(raw)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"解析失败: {exc}"}), 400
    folders = []
    for (path, leaf), links in parser.folders.items():
        links = _dedupe_links(links)
        if links:
            folders.append({"path": path, "leaf": leaf, "count": len(links), "links": links})
    folders.sort(key=lambda x: -x["count"])
    return jsonify({"ok": True, "folders": folders})


@app.post("/api/bookmarks/import")
def api_bookmarks_import():
    data = request.get_json(silent=True) or {}
    group_name = str(data.get("group_name") or "").strip()[:30]
    links = data.get("links") or []
    if not group_name or not isinstance(links, list) or not links:
        return jsonify({"ok": False, "error": "缺少分组名或链接"}), 400
    links = _dedupe_links([{"name": clean_link_name(str(l.get("name") or ""))[:80], "url": str(l.get("url") or "")}
                           for l in links if str(l.get("url") or "").startswith(("http://", "https://"))])
    if not links:
        return jsonify({"ok": False, "error": "没有有效链接"}), 400
    with _lock:
        groups = _read_json(BOOKMARKS_FILE, [])
        target = next((g for g in groups if g.get("name") == group_name), None)
        if target:
            existing = {l["url"] for l in target.get("links") or []}
            added = sum(1 for l in links if l["url"] not in existing)
            target["links"] = (target.get("links") or []) + [l for l in links if l["url"] not in existing]
            mode = "merged"
        else:
            groups.append({"name": group_name, "links": links})
            added = len(links)
            mode = "created"
        _write_json(BOOKMARKS_FILE, groups)
    return jsonify({"ok": True, "added": added, "mode": mode, "total": len(links)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)
