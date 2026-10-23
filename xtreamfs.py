#!/usr/bin/env python3
"""
xtreamfs - expose an Xtream Codes VOD catalog as a read-only folder of
video files that Plex can scan and play.

  <mount>/Movies/Title (Year) {tmdb-123}/Title (Year).mkv
  <mount>/TV/Show (Year) {tmdb-456}/Season 01/Show - S01E01.mkv

Reads are streamed from the provider with HTTP range requests. Only the
categories you choose are included, and the number of simultaneous
provider connections is capped.

Configuration is by environment variable (see README.md).
Run with --list-categories to print the category IDs your provider offers.
"""

import collections
import hashlib
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from collections import OrderedDict
from errno import EIO, ENOENT

import requests

try:
    import mfusepy as fuse
except ImportError:  # pragma: no cover
    import fuse  # fusepy

log = logging.getLogger("xtreamfs")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing required environment variable {name}")
    return v


class Config:
    def __init__(self):
        self.url = env("XTREAM_URL", required=True).rstrip("/")
        self.user = env("XTREAM_USER", required=True)
        self.password = env("XTREAM_PASS", required=True)
        self.mount = env("MOUNT_POINT", "/mnt/xtream")
        self.cache_dir = env("CACHE_DIR", "/config")
        self.movie_cats = self._list(env("MOVIE_CATEGORIES", ""))
        self.series_cats = self._list(env("SERIES_CATEGORIES", ""))
        # Categories that get their own top-level folder, e.g. "843=Anime".
        self.movie_folders = self._map(env("MOVIE_FOLDERS", ""))
        self.series_folders = self._map(env("SERIES_FOLDERS", ""))
        # Individual titles sent to a folder by TMDB ID, whatever their category,
        # e.g. "31910,46260=Anime;1234=Kids".
        self.overrides = {"movies": self._overrides(env("MOVIE_OVERRIDES", "")),
                          "series": self._overrides(env("SERIES_OVERRIDES", ""))}
        for cid in self.movie_folders:
            if cid not in self.movie_cats:
                self.movie_cats.append(cid)
        for cid in self.series_folders:
            if cid not in self.series_cats:
                self.series_cats.append(cid)
        self.refresh_hours = float(env("REFRESH_HOURS", "12"))
        self.max_streams = max(1, int(env("MAX_STREAMS", "1")))
        self.user_agent = env("USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20")
        self.strip_prefix = env("STRIP_PREFIX", "1") == "1"
        self.api_delay = float(env("API_DELAY", "0.3"))
        self.redirect_ttl = float(env("REDIRECT_CACHE_MINUTES", "20")) * 60
        self.unavailable_retry = float(env("UNAVAILABLE_RETRY_HOURS", "24")) * 3600
        self.cache_mb = max(0, int(env("CACHE_MB", "256")))
        self.cache_edge_mb = max(0, int(env("CACHE_EDGE_MB", "8")))
        self.stats_interval = float(env("STATS_INTERVAL", "60"))

    @staticmethod
    def _list(s):
        return [x.strip() for x in s.split(",") if x.strip()]

    @staticmethod
    def _overrides(s):
        out = {}
        for group in s.split(";"):
            if "=" not in group:
                continue
            ids, folder = group.rsplit("=", 1)
            folder = folder.strip()
            if not folder:
                continue
            for tid in ids.split(","):
                tid = tid.strip()
                if tid.isdigit():
                    out[tid] = safe(folder)
        return out

    @staticmethod
    def _map(s):
        out = {}
        for part in s.split(","):
            if "=" in part:
                cid, folder = part.split("=", 1)
                if cid.strip() and folder.strip():
                    out[cid.strip()] = safe(folder.strip())
        return out


# --------------------------------------------------------------------------
# Xtream API
# --------------------------------------------------------------------------


class Xtream:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["User-Agent"] = cfg.user_agent

    def api(self, action, **params):
        p = {"username": self.cfg.user, "password": self.cfg.password,
             "action": action, **params}
        r = self.s.get(f"{self.cfg.url}/player_api.php", params=p, timeout=60)
        r.raise_for_status()
        data = r.json()
        time.sleep(self.cfg.api_delay)
        return data

    def movie_url(self, sid, ext):
        c = self.cfg
        return f"{c.url}/movie/{c.user}/{c.password}/{sid}.{ext}"

    def episode_url(self, eid, ext):
        c = self.cfg
        return f"{c.url}/series/{c.user}/{c.password}/{eid}.{ext}"


def resolve_categories(wanted, available):
    """Match wanted entries (IDs or names, case-insensitive) to category IDs."""
    if not wanted:
        return []
    by_id = {str(c["category_id"]): c for c in available}
    by_name = {str(c.get("category_name", "")).strip().lower(): c for c in available}
    out = []
    for w in wanted:
        c = by_id.get(w) or by_name.get(w.lower())
        if c:
            out.append(str(c["category_id"]))
        else:
            log.warning("Category %r not found at provider; skipping", w)
    return out


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

PREFIX_RE = re.compile(
    r"^\s*(?:\|[^|]{1,12}\|\s*|\[[^\]]{1,12}\]\s*|[A-Z0-9]{2,5}\s*[-:|]\s+"
    r"|(?:4K|UHD)\s+)+")
QUALITY_RE = re.compile(r"^\s*(?:4K|UHD)\b", re.I)
YEAR_RE = re.compile(r"[\(\[]?\b(19\d{2}|20\d{2})\b[\)\]]?")
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_title(name, strip_prefix=True):
    name = (name or "").strip()
    if strip_prefix:
        name = PREFIX_RE.sub("", name)
    return name.strip()


def split_year(title, year_field=None):
    year = None
    if year_field:
        m = re.search(r"(19\d{2}|20\d{2})", str(year_field))
        if m:
            year = m.group(1)
    m = None
    for m in YEAR_RE.finditer(title):
        pass
    if m:  # last year-looking token in the title
        year = year or m.group(1)
        title = (title[: m.start()] + title[m.end():])
    title = re.sub(r"\s{2,}", " ", title).strip(" -._")
    return title, year


def safe(s):
    s = re.sub(r"\s*:\s+", " - ", s)  # "Alien: Romulus" -> "Alien - Romulus"
    s = BAD_CHARS.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" .")
    return s or "Unknown"


def tmdb_of(item):
    """Providers use either "tmdb_id" or "tmdb" for the TMDB ID."""
    return item.get("tmdb_id") or item.get("tmdb")


def tmdb_tag(v):
    v = str(v or "").strip()
    return f" {{tmdb-{v}}}" if v.isdigit() and v != "0" else ""


def ext_of(v, default="mkv"):
    v = str(v or "").strip().lstrip(".").lower()
    return v if re.fullmatch(r"[a-z0-9]{2,4}", v) else default


# --------------------------------------------------------------------------
# Catalog -> tree
# --------------------------------------------------------------------------


class Entry:
    __slots__ = ("url", "key", "mtime")

    def __init__(self, url, key, mtime):
        self.url = url
        self.key = key      # stable cache key for size lookups
        self.mtime = mtime


class Tree:
    def __init__(self):
        self.dirs = {"/": set()}
        self.files = {}
        self.dir_mtime = {}

    def finish(self):
        """Give each folder the newest timestamp of anything inside it, so a
        folder only looks changed when its contents change."""
        for path, e in self.files.items():
            d = path.rsplit("/", 1)[0] or "/"
            while True:
                if self.dir_mtime.get(d, 0) < e.mtime:
                    self.dir_mtime[d] = e.mtime
                if d == "/":
                    break
                d = d.rsplit("/", 1)[0] or "/"

    def add_file(self, path, entry):
        parts = path.strip("/").split("/")
        cur = ""
        for p in parts[:-1]:
            parent = cur or "/"
            cur = f"{cur}/{p}"
            self.dirs.setdefault(parent, set()).add(p)
            self.dirs.setdefault(cur, set())
        self.dirs.setdefault(cur or "/", set()).add(parts[-1])
        self.files[path] = entry

    def unique(self, path):
        if path not in self.dirs and path not in self.files:
            return path
        i = 2
        while f"{path} ({i})" in self.dirs:
            i += 1
        return f"{path} ({i})"


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_ts(v, default):
    """Unix timestamp from the API, ignoring missing or nonsense values."""
    t = to_int(v, 0)
    return t if t > 946684800 else default  # after 2000-01-01


NAMING_VERSION = 2  # bump when the naming rules change


def build_tree(catalog, xt: Xtream, strip_prefix=True, state=None, overrides=None):
    """Build the folder tree from a catalog.

    `state` remembers, from earlier builds, the path each title was given and
    when it was first seen. Reusing them keeps unchanged titles identical
    between refreshes (same path, same timestamps), so Plex doesn't treat
    them as new or modified. Returns (tree, new_state).
    """
    state = state or {}
    overrides = overrides or {}
    movie_over = overrides.get("movies", {})
    series_over = overrides.get("series", {})
    # When the naming rules change, rename everything once to the new scheme.
    old_paths = state.get("paths", {}) if state.get("naming") == NAMING_VERSION else {}
    old_seen = state.get("first_seen", {})
    paths, first_seen = {}, {}
    now = int(time.time())

    def seen(key, provider_ts):
        t = old_seen.get(key) or to_ts(provider_ts, 0) or now
        first_seen[key] = t
        return t

    tree = Tree()
    tree.dirs["/"].update({"Movies", "TV"})
    tree.dirs.setdefault("/Movies", set())
    tree.dirs.setdefault("/TV", set())

    # ---- Movies ------------------------------------------------------------
    # The same title listed twice by the provider (e.g. two qualities) goes in
    # one folder as separate files, which Plex shows as versions of one movie.
    movies, seen_ids = [], set()
    for m in catalog.get("movies", []):
        sid = m.get("stream_id")
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        movies.append(m)
    # Titles that already have a path go first, so they keep it.
    # Regular copies before 4K ones, so the 4K copy is the one labelled "- 4K".
    movies.sort(key=lambda m: (f"m{m.get('stream_id')}" not in old_paths,
                               bool(QUALITY_RE.match(str(m.get("name") or "")))))
    for m in movies:
        sid = m.get("stream_id")
        key = f"m{sid}"
        ext = ext_of(m.get("container_extension"))
        root = movie_over.get(str(tmdb_of(m) or "")) or m.get("_folder") or "Movies"
        tree.dirs["/"].add(root)
        tree.dirs.setdefault(f"/{root}", set())
        path = old_paths.get(key)
        if not path or path in tree.files or not path.startswith(f"/{root}/"):
            title, year = split_year(clean_title(m.get("name"), strip_prefix),
                                     m.get("year") or m.get("releaseDate"))
            base = safe(f"{title} ({year})" if year else title)
            folder = f"/{root}/{safe(base + tmdb_tag(tmdb_of(m)))}"
            label = "4K" if QUALITY_RE.match(str(m.get("name") or "")) else None
            path = f"{folder}/{base}.{ext}"
            if label and path in tree.files:
                path = f"{folder}/{base} - {label}.{ext}"
            if path in tree.files:
                path = f"{folder}/{base} - {sid}.{ext}"
        paths[key] = path
        tree.add_file(path, Entry(xt.movie_url(sid, ext), key,
                                  seen(key, m.get("added"))))

    # ---- Series --------------------------------------------------------------
    shows = [s for s in catalog.get("series", []) if (s.get("_info") or {}).get("episodes")]
    shows.sort(key=lambda s: f"s{s.get('series_id')}" not in old_paths)
    for s in shows:
        skey = f"s{s.get('series_id')}"
        episodes = s["_info"]["episodes"]
        if isinstance(episodes, list):  # some panels return a list of lists
            episodes = {str(i + 1): v for i, v in enumerate(episodes)}
        title, year = split_year(clean_title(s.get("name"), strip_prefix),
                                 s.get("year") or s.get("releaseDate"))
        root = series_over.get(str(tmdb_of(s) or "")) or s.get("_folder") or "TV"
        tree.dirs["/"].add(root)
        tree.dirs.setdefault(f"/{root}", set())
        folder = old_paths.get(skey)
        if not folder or folder in tree.dirs or not folder.startswith(f"/{root}/"):
            show = safe(f"{title} ({year})" if year else title)
            folder = tree.unique(f"/{root}/{safe(show + tmdb_tag(tmdb_of(s)))}")
        paths[skey] = folder
        tree.dirs.setdefault(folder, set())
        tree.dirs[f"/{root}"].add(folder.rsplit("/", 1)[1])
        # Episode file names come from the show folder, so they stay stable too.
        ep_title = folder.rsplit("/", 1)[1]
        ep_title = re.sub(r"\s*\{tmdb-\d+\}$", "", ep_title)
        ep_title = re.sub(r"\s*\((19|20)\d{2}\)( \(\d+\))?$", "", ep_title) or title
        for season_key, eps in episodes.items():
            for ep in eps or []:
                eid = ep.get("id")
                key = f"e{eid}"
                season = to_int(ep.get("season") or season_key, 0)
                num = to_int(ep.get("episode_num"), 0)
                ext = ext_of(ep.get("container_extension"))
                name = safe(f"{ep_title} - S{season:02d}E{num:02d}.{ext}")
                path = f"{folder}/Season {season:02d}/{name}"
                if path in tree.files:
                    path = path[: -len(ext) - 1] + f" - {eid}.{ext}"
                tree.add_file(path, Entry(
                    xt.episode_url(eid, ext), key,
                    seen(key, ep.get("added") or s.get("last_modified"))))

    tree.finish()
    dirs = settle_dir_times(tree, state.get("dirs"), now)
    return tree, {"naming": NAMING_VERSION, "paths": paths,
                  "first_seen": first_seen, "dirs": dirs}


def settle_dir_times(tree, old_dirs, now):
    """Make a folder's date change whenever something is added to or removed
    from it (including a show moved in from another folder), and keep it
    unchanged otherwise. Plex skips folders whose date hasn't changed, so this
    is what lets it find moved and new titles without re-reading everything.
    Returns {folder: [contents fingerprint, date]} to remember for next time.
    """
    sigs = {d: hashlib.md5("\0".join(sorted(names)).encode("utf-8", "surrogateescape")).hexdigest()
            for d, names in tree.dirs.items()}
    if old_dirs is None:  # first run with this feature: keep the dates as built
        return {d: [sigs[d], tree.dir_mtime.get(d, now)] for d in sigs}
    changed = {d for d in sigs if d not in old_dirs or old_dirs[d][0] != sigs[d]}
    for d in list(changed):  # parents of a changed folder count as changed too
        while d != "/":
            d = d.rsplit("/", 1)[0] or "/"
            changed.add(d)
    out = {}
    for d, sig in sigs.items():
        if d in changed:
            t = max(now, tree.dir_mtime.get(d, 0))
        else:
            t = max(old_dirs[d][1], tree.dir_mtime.get(d, 0))
        tree.dir_mtime[d] = t
        out[d] = [sig, t]
    return out


def catalog_key(cfg: Config):
    """The category selection a catalog was built from."""
    return {"movies": cfg.movie_cats, "series": cfg.series_cats,
            "movie_folders": cfg.movie_folders,
            "series_folders": cfg.series_folders}


def fetch_catalog(cfg: Config, xt: Xtream, old_catalog=None):
    """Pull the selected categories from the provider."""
    catalog = {"movies": [], "series": [], "fetched": int(time.time()),
               "cats": catalog_key(cfg)}

    if cfg.movie_cats:
        cats = resolve_categories(cfg.movie_cats, xt.api("get_vod_categories"))
        movies = {}
        for cid in cats:
            items = xt.api("get_vod_streams", category_id=cid) or []
            log.info("Movies category %s: %d titles", cid, len(items))
            folder = cfg.movie_folders.get(cid)
            for m in items:
                sid = m.get("stream_id")
                if sid not in movies:
                    m["_folder"] = folder or "Movies"
                    movies[sid] = m
                elif folder:  # a category with its own folder wins
                    movies[sid]["_folder"] = folder
        catalog["movies"] = list(movies.values())

    if cfg.series_cats:
        catalog["series"] = fetch_series(cfg, xt, old_catalog)
    return catalog


def fetch_series(cfg: Config, xt: Xtream, old_catalog=None):
    """Fetch the selected series categories and each show's episode list.

    Episode lists are the slow part (one API call per show), so they are
    cached in episodes.json as they come in. A show is only re-fetched when
    the provider's last_modified for it changes, and an interrupted run picks
    up where it left off.
    """
    cache_path = os.path.join(cfg.cache_dir, "episodes.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    # Seed from a catalog written by an older version.
    for s in (old_catalog or {}).get("series", []):
        sid = str(s.get("series_id"))
        if sid not in cache and (s.get("_info") or {}).get("episodes"):
            cache[sid] = {"lm": str(s.get("last_modified")),
                          "episodes": s["_info"]["episodes"]}

    def save():
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, cache_path)

    # Gather show lists first (fast), dropping shows listed in several categories.
    shows, seen = [], set()
    cats = resolve_categories(cfg.series_cats, xt.api("get_series_categories"))
    by_id = {}
    for cid in cats:
        items = xt.api("get_series", category_id=cid) or []
        log.info("Series category %s: %d shows", cid, len(items))
        folder = cfg.series_folders.get(cid)
        for s in items:
            sid = str(s.get("series_id"))
            if sid not in seen:
                seen.add(sid)
                s["_folder"] = folder or "TV"
                by_id[sid] = s
                shows.append(s)
            elif folder:  # a category with its own folder wins
                by_id[sid]["_folder"] = folder

    def fresh(s):
        c = cache.get(str(s.get("series_id")))
        return c and c.get("lm") == str(s.get("last_modified"))

    todo = [s for s in shows if not fresh(s)]
    est = len(todo) * (cfg.api_delay + 0.25) / 60
    log.info("Series: %d unique shows, %d cached, %d need episode lists (about %d min)",
             len(shows), len(shows) - len(todo), len(todo), round(est))

    started = last_log = time.time()
    for i, s in enumerate(todo, 1):
        sid = str(s.get("series_id"))
        try:
            info = xt.api("get_series_info", series_id=sid) or {}
            cache[sid] = {"lm": str(s.get("last_modified")),
                          "episodes": info.get("episodes") or {}}
        except Exception as e:  # keep going on one bad series
            log.warning("Series %s (%s): %s", sid, s.get("name"), e)
        if i % 100 == 0:
            save()
        if time.time() - last_log >= 30 or i == len(todo):
            elapsed = time.time() - started
            left = elapsed / i * (len(todo) - i)
            log.info("Episode lists: %d/%d (%d%%), about %d min left",
                     i, len(todo), i * 100 // len(todo), round(left / 60))
            last_log = time.time()
    if todo:
        save()

    for s in shows:
        c = cache.get(str(s.get("series_id"))) or {}
        s["_info"] = {"episodes": c.get("episodes") or {}}
    return shows


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class ConnectionPool:
    """Caps simultaneous provider connections across all open files."""

    def __init__(self, limit):
        self.limit = limit
        self.cond = threading.Condition()
        self.active = OrderedDict()  # stream -> True (LRU order)

    def acquire(self, stream, timeout=30):
        with self.cond:
            deadline = time.time() + timeout
            while len(self.active) >= self.limit:
                # Evict the least-recently-used idle connection, if any.
                for victim in list(self.active):
                    if victim is not stream and not victim.busy:
                        victim._close_locked_by_pool()
                        self.active.pop(victim, None)
                        break
                else:
                    left = deadline - time.time()
                    if left <= 0:
                        return False
                    self.cond.wait(left)
                    continue
            self.active[stream] = True
            self.active.move_to_end(stream)
            return True

    def touch(self, stream):
        with self.cond:
            if stream in self.active:
                self.active.move_to_end(stream)

    def release(self, stream):
        with self.cond:
            self.active.pop(stream, None)
            self.cond.notify_all()


NS = 1_000_000_000
SKIP_FORWARD = 4 * 1024 * 1024  # read through small forward gaps instead of reconnecting


class Stats:
    """Per-interval counters for provider traffic, logged periodically."""

    def __init__(self):
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.requests = self.redirects = self.cache_hits = self.errors = 0
        self.mem_reads = 0
        self.unavailable = 0
        self.error_kinds = collections.Counter()
        self.bytes = 0
        self.ttfb = []
        self.files = set()

    def request(self, key, seconds, redirected, cache_hit, status):
        with self.lock:
            self.requests += 1
            self.files.add(key)
            self.ttfb.append(seconds)
            self.redirects += redirected
            self.cache_hits += cache_hit
            if status in UNAVAILABLE:
                self.unavailable += 1
            elif status not in (200, 206):
                self.errors += 1
                self.error_kinds[f"HTTP {status}"] += 1

    def error(self, key, kind="connection error"):
        with self.lock:
            self.requests += 1
            self.files.add(key)
            self.errors += 1
            self.error_kinds[kind] += 1

    def mem_read(self):
        with self.lock:
            self.mem_reads += 1

    def add_bytes(self, n):
        with self.lock:
            self.bytes += n

    def report(self, interval):
        with self.lock:
            if not self.requests and not self.mem_reads:
                return None
            t = sorted(self.ttfb) or [0]
            msg = ("Provider, last %ds: %d requests for %d files | first byte avg %.2fs, "
                   "p90 %.2fs | %d redirects followed, %d redirect-cache hits | "
                   "%.0f MB read | %d errors | %d unavailable at provider | "
                   "%d reads served from memory") % (
                interval, self.requests, len(self.files), sum(t) / len(t),
                t[int(len(t) * 0.9) if len(t) > 1 else 0], self.redirects,
                self.cache_hits, self.bytes / 2**20, self.errors, self.unavailable,
                self.mem_reads)
            if self.error_kinds:
                msg += " (" + ", ".join(f"{k} x{v}" for k, v in
                                       self.error_kinds.most_common(4)) + ")"
            self._reset()
            return msg


BLOCK = 128 * 1024
UNAVAILABLE = (403, 404, 410)  # the provider refuses this particular title


class EdgeCache:
    """Keeps the first and last few MB of recently read files in memory.

    While analysing a file, Plex reads its header and its end (where MKV and
    MP4 keep their index) several times, often from separate opens. Serving
    those repeats from memory saves a provider request for each one. Only the
    edges of files are cached, so playback through the middle of a title
    doesn't push them out.
    """

    def __init__(self, max_bytes, edge_bytes):
        self.max_bytes = max_bytes
        self.edge = edge_bytes
        self.blocks = OrderedDict()  # (key, block index) -> bytes
        self.used = 0
        self.lock = threading.Lock()

    def enabled(self):
        return self.max_bytes > 0 and self.edge > 0

    def in_edge(self, offset, size, file_size):
        return offset < self.edge or offset + size > file_size - self.edge

    def get(self, key, offset, size, file_size):
        if not self.enabled():
            return None
        end = min(offset + size, file_size)
        first, last = offset // BLOCK, (end - 1) // BLOCK
        with self.lock:
            parts = []
            for i in range(first, last + 1):
                b = self.blocks.get((key, i))
                if b is None:
                    return None
                self.blocks.move_to_end((key, i))
                parts.append(b)
        data = b"".join(parts)
        start = offset - first * BLOCK
        return data[start:start + (end - offset)]

    def put(self, key, offset, data, file_size):
        """Store the whole blocks contained in data (which starts at offset)."""
        if not self.enabled() or offset % BLOCK:
            return
        with self.lock:
            for pos in range(0, len(data), BLOCK):
                chunk = data[pos:pos + BLOCK]
                start = offset + pos
                if len(chunk) < BLOCK and start + len(chunk) < file_size:
                    break  # incomplete block that isn't the end of the file
                if not self.in_edge(start, len(chunk), file_size):
                    continue
                k = (key, start // BLOCK)
                if k in self.blocks:
                    self.blocks.move_to_end(k)
                    continue
                self.blocks[k] = chunk
                self.used += len(chunk)
            while self.used > self.max_bytes and self.blocks:
                _, old = self.blocks.popitem(last=False)
                self.used -= len(old)


class Stream:
    def __init__(self, fs, entry, size):
        self.fs = fs
        self.entry = entry
        self.size = size
        self.resp = None
        self.pos = 0
        self.lock = threading.Lock()
        self.busy = False
        # Bytes read since the last block boundary, collected for the edge cache.
        self.acc_pos = None
        self.acc = b""
        self.nxt = -1

    def _close_locked_by_pool(self):
        r, self.resp = self.resp, None
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    def _open(self, offset):
        self._close_locked_by_pool()
        self.fs.pool.release(self)
        if not self.fs.pool.acquire(self):
            raise OSError(EIO, "provider connection limit reached")
        # Near either end of the file, start at a block boundary so the
        # whole block can go in the edge cache (costs under 128 KB extra).
        start = offset
        if self.fs.cache.enabled() and self.fs.cache.in_edge(offset, 1, self.size):
            start = offset - offset % BLOCK
        try:
            r = self.fs.fetch(self.entry, f"bytes={start}-", (15, 60))
        except Exception:
            self.fs.pool.release(self)
            raise
        if r.status_code == 200:
            start = 0  # server ignored the range; the body starts at byte 0
        elif r.status_code != 206:
            r.close()
            self.fs.pool.release(self)
            raise OSError(EIO, f"HTTP {r.status_code}")
        self.resp = r
        self.pos = start
        self.acc_pos = None
        if start < offset:
            self._read_exact(offset - start)  # read forward, filling the cache

    def read(self, size, offset):
        if offset >= self.size:
            return b""
        size = min(size, self.size - offset)
        with self.lock:
            self.busy = True
            try:
                if self.resp is not None and offset < self.pos:
                    # The kernel sometimes sends reads slightly out of order.
                    # If the stream already passed this range, it may be in
                    # the edge cache; that avoids reconnecting to go back.
                    cached = self.fs.cache.get(self.entry.key, offset, size, self.size)
                    if cached is not None and len(cached) == size:
                        self.fs.stats.mem_read()
                        return cached
                if self.resp is None or offset < self.pos or \
                        offset - self.pos > SKIP_FORWARD:
                    self._open(offset)
                elif offset > self.pos:
                    self._read_exact(offset - self.pos)
                data = self._read_exact(size)
                self.fs.pool.touch(self)
                return data
            except OSError:
                raise
            except Exception as e:
                log.warning("Read error on %s at %d: %s", self.entry.key, offset, e)
                self._close_locked_by_pool()
                self.fs.pool.release(self)
                raise OSError(EIO, str(e))
            finally:
                self.busy = False

    def _read_exact(self, n):
        chunks, got = [], 0
        while got < n:
            b = self.resp.raw.read(n - got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        data = b"".join(chunks)
        self._feed(self.pos, data)
        self.pos += got
        self.fs.stats.add_bytes(got)
        return data

    def _feed(self, p, b):
        """Collect sequentially read bytes into whole blocks for the edge cache."""
        cache = self.fs.cache
        if not cache.enabled() or not b:
            return
        if self.acc_pos is None or p != self.nxt:
            self.acc_pos = p + (-p) % BLOCK  # next block boundary
            self.acc = b""
        want = self.acc_pos + len(self.acc)
        if p <= want < p + len(b):
            self.acc += b[want - p:]
        self.nxt = p + len(b)
        n = len(self.acc) // BLOCK * BLOCK
        if self.acc_pos + len(self.acc) >= self.size:
            n = len(self.acc)  # the file's last, shorter block
        if n:
            cache.put(self.entry.key, self.acc_pos, self.acc[:n], self.size)
            self.acc_pos += n
            self.acc = self.acc[n:]


    def close(self):
        with self.lock:
            self._close_locked_by_pool()
        self.fs.pool.release(self)


# --------------------------------------------------------------------------
# Filesystem
# --------------------------------------------------------------------------


class XtreamFS(fuse.Operations):
    use_ns = True

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.xt = Xtream(cfg)
        self.http = requests.Session()
        self.http.headers["User-Agent"] = cfg.user_agent
        self.pool = ConnectionPool(cfg.max_streams)
        self.stats = Stats()
        self.cache = EdgeCache(cfg.cache_mb * 2**20, cfg.cache_edge_mb * 2**20)
        self.resolved = {}  # entry key -> (final URL after redirects, time resolved)
        self.tree = Tree()
        self.tree_lock = threading.Lock()
        self.handles = {}
        self.next_fh = 1
        self.fh_lock = threading.Lock()
        self.start = int(time.time())

        os.makedirs(cfg.cache_dir, exist_ok=True)
        self.catalog_path = os.path.join(cfg.cache_dir, "catalog.json")
        self.sizes_path = os.path.join(cfg.cache_dir, "sizes.json")
        self.trigger_path = os.path.join(cfg.cache_dir, "refresh")
        self.sizes = self._load_json(self.sizes_path, {})
        self.size_fail = {}
        self.unavail_path = os.path.join(cfg.cache_dir, "unavailable.json")
        self.unavailable = self._load_json(self.unavail_path, {})  # key -> [status, time]
        self.unavail_dirty = False
        self.size_lock = threading.Lock()
        self.sizes_dirty = False

        self.state_path = os.path.join(cfg.cache_dir, "state.json")
        self.state = self._load_json(self.state_path, {})
        catalog = self._load_json(self.catalog_path, None)
        if catalog:
            self.tree, self.state = build_tree(catalog, self.xt,
                                               cfg.strip_prefix, self.state,
                                               cfg.overrides)
            log.info("Loaded cached catalog: %d files", len(self.tree.files))
        self.catalog = catalog
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        threading.Thread(target=self._save_sizes_loop, daemon=True).start()
        threading.Thread(target=self._stats_loop, daemon=True).start()

    # ---- persistence -----------------------------------------------------

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def _save_sizes_loop(self):
        while True:
            time.sleep(30)
            if self.sizes_dirty:
                with self.size_lock:
                    snapshot = dict(self.sizes)
                    self.sizes_dirty = False
                self._save_json(self.sizes_path, snapshot)
            if self.unavail_dirty:
                with self.size_lock:
                    snapshot = dict(self.unavailable)
                    self.unavail_dirty = False
                self._save_json(self.unavail_path, snapshot)

    def _wait(self, seconds):
        """Sleep until the next refresh is due, or until someone creates the
        trigger file (docker exec xtreamfs touch /config/refresh)."""
        end = time.time() + seconds
        while time.time() < end:
            if os.path.exists(self.trigger_path):
                try:
                    os.remove(self.trigger_path)
                except OSError:
                    pass
                log.info("Manual refresh requested")
                return
            time.sleep(min(10, max(0, end - time.time())))

    def _refresh_loop(self):
        if not self.catalog:
            delay = 0
        elif self.catalog.get("cats") != catalog_key(self.cfg):
            log.info("Category selection changed since last refresh")
            delay = 0
        else:
            delay = max(0, self.catalog.get("fetched", 0)
                        + self.cfg.refresh_hours * 3600 - time.time())
        while True:
            self._wait(delay)
            try:
                log.info("Refreshing catalog from provider")
                catalog = fetch_catalog(self.cfg, self.xt, self.catalog)
                tree, state = build_tree(catalog, self.xt,
                                         self.cfg.strip_prefix, self.state,
                                         self.cfg.overrides)
                with self.tree_lock:
                    self.tree = tree
                self.catalog, self.state = catalog, state
                self._save_json(self.catalog_path, catalog)
                self._save_json(self.state_path, state)
                log.info("Catalog ready: %d files", len(tree.files))
                delay = self.cfg.refresh_hours * 3600
            except Exception as e:
                log.error("Catalog refresh failed: %s (retrying in 15 min)", e)
                delay = 900

    # ---- sizes -----------------------------------------------------------

    def is_unavailable(self, entry):
        u = self.unavailable.get(entry.key)
        return bool(u) and time.time() - u[1] < self.cfg.unavailable_retry

    def _size(self, entry):
        s = self.sizes.get(entry.key)
        if s:
            return s
        if self.is_unavailable(entry):
            return 0
        if time.time() - self.size_fail.get(entry.key, 0) < 3600:
            return 0
        size, status = self._probe_size(entry)
        with self.size_lock:
            if size:
                self.sizes[entry.key] = size
                self.sizes_dirty = True
                if self.unavailable.pop(entry.key, None):
                    self.unavail_dirty = True
            elif status in UNAVAILABLE:
                # The provider refuses this title. Hide it, and check again later.
                self.unavailable[entry.key] = [status, time.time()]
                self.unavail_dirty = True
            else:
                self.size_fail[entry.key] = time.time()
        return size

    def fetch(self, entry, byte_range, timeout):
        """GET a byte range of a title from the provider.

        Xtream servers usually answer with a redirect to a tokenised URL on a
        streaming host. Plex opens each file several times while scanning it,
        so the final URL is remembered for a while and later requests go there
        directly, skipping the redirect. If the remembered URL stops working
        (tokens expire), it falls back to the original URL.
        """
        headers = {"Range": byte_range}
        cached = self.resolved.get(entry.key)
        if cached and time.time() - cached[1] < self.cfg.redirect_ttl:
            t0 = time.time()
            try:
                r = self.http.get(cached[0], stream=True, timeout=timeout,
                                  headers=headers, allow_redirects=False)
                if r.status_code in (200, 206):
                    self.stats.request(entry.key, time.time() - t0, False, True, r.status_code)
                    return r
                r.close()
            except requests.RequestException:
                pass
            self.resolved.pop(entry.key, None)
        t0 = time.time()
        try:
            r = self.http.get(entry.url, stream=True, timeout=timeout, headers=headers)
        except requests.RequestException as e:
            self.stats.error(entry.key, type(e).__name__)
            raise
        ok = r.status_code in (200, 206)
        if ok and r.history:
            self.resolved[entry.key] = (r.url, time.time())
        self.stats.request(entry.key, time.time() - t0, bool(r.history), False, r.status_code)
        return r

    def _stats_loop(self):
        interval = self.cfg.stats_interval
        while interval > 0:
            time.sleep(interval)
            msg = self.stats.report(interval)
            if msg:
                log.info(msg)
            cutoff = time.time() - self.cfg.redirect_ttl
            for k, (_, t) in list(self.resolved.items()):
                if t < cutoff:
                    self.resolved.pop(k, None)

    def _probe_size(self, entry):
        """Find the file size with a 1-byte range request (HEAD is often unsupported).
        Returns (size, HTTP status); size is 0 when it couldn't be found."""
        probe = Stream(self, entry, 0)
        if not self.pool.acquire(probe):
            return 0, None
        try:
            r = self.fetch(entry, "bytes=0-0", (15, 30))
            try:
                cr = r.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)$", cr)
                if r.status_code == 206 and m:
                    return int(m.group(1)), 206
                if r.status_code == 200:
                    return int(r.headers.get("Content-Length", 0)), 200
                if r.status_code in UNAVAILABLE:
                    log.debug("Size probe %s: HTTP %s (unavailable)", entry.key, r.status_code)
                else:
                    log.warning("Size probe %s: HTTP %s", entry.key, r.status_code)
                return 0, r.status_code
            finally:
                r.close()
        except Exception as e:
            log.warning("Size probe %s failed: %s", entry.key, e)
            return 0, None
        finally:
            self.pool.release(probe)

    # ---- FUSE ops ----------------------------------------------------------

    def getattr(self, path, fh=None):
        tree = self.tree
        if path in tree.dirs:
            t = tree.dir_mtime.get(path, self.start) * NS
            return {"st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2,
                    "st_mtime": t, "st_ctime": t, "st_atime": t}
        e = tree.files.get(path)
        if not e or self.is_unavailable(e):
            raise fuse.FuseOSError(ENOENT)
        size = self._size(e)
        if not size and self.is_unavailable(e):
            raise fuse.FuseOSError(ENOENT)
        return {"st_mode": stat.S_IFREG | 0o444, "st_nlink": 1,
                "st_size": size, "st_mtime": e.mtime * NS,
                "st_ctime": e.mtime * NS, "st_atime": e.mtime * NS}

    def readdir(self, path, fh):
        tree = self.tree
        names = tree.dirs.get(path)
        if names is None:
            raise fuse.FuseOSError(ENOENT)
        base = "" if path == "/" else path
        shown = []
        for n in sorted(names):
            e = tree.files.get(f"{base}/{n}")
            if e is None or not self.is_unavailable(e):
                shown.append(n)
        return [".", "..", *shown]

    def open(self, path, flags):
        e = self.tree.files.get(path)
        if not e:
            raise fuse.FuseOSError(ENOENT)
        size = self._size(e)
        if not size:
            raise fuse.FuseOSError(EIO)
        with self.fh_lock:
            fh = self.next_fh
            self.next_fh += 1
            self.handles[fh] = Stream(self, e, size)
        return fh

    def read(self, path, size, offset, fh):
        s = self.handles.get(fh)
        if not s:
            raise fuse.FuseOSError(EIO)
        key = s.entry.key
        try:
            data = self.cache.get(key, offset, size, s.size)
            if data is not None:
                self.stats.mem_read()
                return data
            return s.read(size, offset)
        except OSError as e:
            raise fuse.FuseOSError(e.errno or EIO)

    def release(self, path, fh):
        s = self.handles.pop(fh, None)
        if s:
            s.close()
        return 0

    def statfs(self, path):
        return {"f_bsize": 131072, "f_frsize": 131072, "f_blocks": 1 << 30,
                "f_bfree": 0, "f_bavail": 0, "f_namemax": 255}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def list_categories(cfg):
    xt = Xtream(cfg)
    for label, action in (("MOVIE_CATEGORIES", "get_vod_categories"),
                          ("SERIES_CATEGORIES", "get_series_categories")):
        print(f"\n== {label} ==")
        for c in xt.api(action) or []:
            print(f"  {str(c['category_id']):>6}  {c.get('category_name')}")


class Redact(logging.Filter):
    """Keep provider credentials out of the logs."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 3]

    def filter(self, record):
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    for h in logging.getLogger().handlers:
        h.addFilter(Redact([cfg.user, cfg.password]))
    if "--list-categories" in sys.argv:
        list_categories(cfg)
        return
    if not cfg.movie_cats and not cfg.series_cats:
        sys.exit("Set MOVIE_CATEGORIES and/or SERIES_CATEGORIES "
                 "(run with --list-categories to see them)")
    os.makedirs(cfg.mount, exist_ok=True)
    log.info("Mounting at %s (max %d provider connections)",
             cfg.mount, cfg.max_streams)
    fuse.FUSE(XtreamFS(cfg), cfg.mount, foreground=True, nothreads=False,
              allow_other=True, ro=True, attr_timeout=300, entry_timeout=300)


if __name__ == "__main__":
    main()
