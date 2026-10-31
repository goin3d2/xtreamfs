"""Settings and status page for xtreamfs.

Serves one page (web.html) plus a small JSON API. Runs in a background thread
inside the xtreamfs process, so it sees live refresh progress and log lines.
The provider login is never shown or editable here.
"""

import base64
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import xtreamfs as xf

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")

# Settings shown on the Settings tab: (name, group, label, kind, default, help).
# Category selection and folders have their own tab.
FIELDS = [
    ("MOVIE_CATEGORY_FOLDERS", "Folders", "Movie category subfolders", "bool", "0",
     "Movies/<Category>/<Title>. Turning it on or off moves every movie once."),
    ("SERIES_CATEGORY_FOLDERS", "Folders", "TV category subfolders", "bool", "0",
     "TV/<Category>/<Show>. TV libraries then need each category folder added "
     "separately, so usually leave this off."),
    ("MOVIE_OVERRIDES", "Folders", "Movie overrides", "text", "",
     "Send movies to a folder by TMDB ID: 128,129=Anime Movies;1234=Kids"),
    ("SERIES_OVERRIDES", "Folders", "Show overrides", "text", "",
     "Send shows to a folder by TMDB ID: 46260,31910=Anime"),
    ("STRIP_PREFIX", "Folders", "Strip provider tags", "bool", "1",
     'Remove tags like "EN - " and "SUB:" from titles.'),
    ("REFRESH_HOURS", "Refresh", "Refresh every (hours)", "number", "12",
     "How often to re-read the catalog, counted from the last refresh."),
    ("API_DELAY", "Refresh", "Pause between API calls (seconds)", "number", "0.3",
     "Higher is gentler on the provider, but refreshes take longer."),
    ("SHRINK_GUARD_PERCENT", "Refresh", "Shrink guard (%)", "number", "25",
     "Keep the previous catalog if a refresh would remove more than this share "
     "of movies or shows. 0 turns it off."),
    ("SHRINK_GUARD_MIN", "Refresh", "Shrink guard minimum", "number", "50",
     "Ignore the guard for libraries smaller than this."),
    ("UNAVAILABLE_RETRY_HOURS", "Refresh", "Retry refused titles after (hours)", "number",
     "24", "Titles the provider refuses (403/404) stay hidden this long."),
    ("STRM_NFO", "Emby / Jellyfin", "Write .nfo files", "bool", "0",
     "Metadata files next to each .strm, from the provider's catalog."),
    ("STRM_NFO_LOCK", "Emby / Jellyfin", "Lock .nfo files", "bool", "0",
     "Tell the server to keep these details and skip online lookups."),
    ("MAX_STREAMS", "Streaming", "Max provider connections", "number", "1",
     "Your plan's connection limit, minus any you need for live TV."),
    ("USER_AGENT", "Streaming", "User agent", "text", "VLC/3.0.20 LibVLC/3.0.20",
     "Some providers only accept known players."),
    ("REDIRECT_CACHE_MINUTES", "Streaming", "Reuse stream links for (minutes)", "number",
     "20", "How long to reuse a title's resolved streaming URL."),
    ("CACHE_MB", "Streaming", "Memory cache (MB)", "number", "256",
     "For the start and end of recently read files. 0 turns it off."),
    ("CACHE_EDGE_MB", "Streaming", "Cached edge size (MB)", "number", "8",
     "How much of each file's start and end counts as cacheable."),
    ("STATS_INTERVAL", "Streaming", "Stats line every (seconds)", "number", "60",
     "0 turns the provider stats line off."),
    ("LOG_LEVEL", "Streaming", "Log level", "choice:DEBUG,INFO,WARNING,ERROR", "INFO", ""),
]
RESTART = {"MAX_STREAMS", "USER_AGENT", "CACHE_MB", "CACHE_EDGE_MB", "STATS_INTERVAL"}
CATEGORY_KEYS = {"MOVIE_CATEGORIES", "SERIES_CATEGORIES", "MOVIE_EXCLUDE", "SERIES_EXCLUDE",
                 "MOVIE_FOLDERS", "SERIES_FOLDERS"}


def settings_view():
    xf.load_settings()
    out = []
    for name, group, label, kind, default, help_text in FIELDS:
        if name in xf.SETTINGS:
            value, source = xf.SETTINGS[name], "page"
        elif name in os.environ:
            value, source = os.environ[name], "env"
        else:
            value, source = default, "default"
        out.append({"name": name, "group": group, "label": label, "kind": kind,
                    "value": value, "source": source, "default": default,
                    "env": os.environ.get(name), "help": help_text,
                    "restart": name in RESTART})
    return out


def validate(changes):
    kinds = {f[0]: f[3] for f in FIELDS}
    clean = {}
    for k, v in changes.items():
        if k not in xf.EDITABLE:
            raise ValueError(f"{k} can't be changed here")
        if v is None:
            clean[k] = None
            continue
        v = str(v).strip()
        kind = kinds.get(k, "text")
        if kind == "number" and v:
            float(v)  # raises ValueError
        elif kind == "bool" and v not in ("0", "1"):
            raise ValueError(f"{k} must be 0 or 1")
        elif kind.startswith("choice:") and v not in kind[7:].split(","):
            raise ValueError(f"{k} must be one of {kind[7:]}")
        clean[k] = v
    return clean


def start(fs, port, password=""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xtreamfs"

        def log_message(self, *args):  # keep request noise out of the log
            pass

        def _authorized(self):
            if not password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    if hmac.compare_digest(pw, password):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="xtreamfs"')
            self.end_headers()
            return False

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(HTML_PATH, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self._send(200, {
                        "now": int(time.time()), "status": xf.STATUS,
                        "catalog": fs.catalog_summary(), "started": fs.start,
                        "unavailable": len(fs.unavailable),
                        "strm_dir": fs.cfg.strm_dir,
                        "max_streams": fs.cfg.max_streams,
                        "overrides_from_page": sorted(xf.SETTINGS)})
                if u.path == "/api/log":
                    after = int((q.get("after") or ["0"])[0])
                    lines, last = xf.RING.since(after)
                    return self._send(200, {"last": last, "lines": lines})
                if u.path == "/api/settings":
                    return self._send(200, settings_view())
                if u.path == "/api/login":
                    return self._send(200, dict(fs.login_view(), protected=bool(password)))
                if u.path == "/api/account":
                    fresh = (q.get("fresh") or ["0"])[0] == "1"
                    return self._send(200, fs.account(fresh))
                if u.path == "/api/categories":
                    live = (q.get("live") or ["0"])[0] == "1"
                    return self._send(200, fs.categories_view(live))
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": xf.scrub(e)})

        def do_POST(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            try:
                if u.path == "/api/refresh":
                    fs.request_refresh()
                    return self._send(200, {"ok": True})
                if u.path == "/api/accept-shrink":
                    fs.accept_shrink()
                    return self._send(200, {"ok": True})
                if u.path == "/api/settings":
                    return self._send(200, fs.save_settings(validate(self._body())))
                if u.path in ("/api/login/test", "/api/login"):
                    b = self._body()
                    url, user = str(b.get("url") or ""), str(b.get("user") or "")
                    pw = str(b.get("password") or "") or fs.cfg.password
                    if u.path == "/api/login/test":
                        return self._send(200, fs.check_login(url, user, pw))
                    return self._send(200, fs.save_login(url, user, pw, bool(b.get("force"))))
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": xf.scrub(e)})
            except Exception as e:
                self._send(500, {"error": xf.scrub(e)})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
