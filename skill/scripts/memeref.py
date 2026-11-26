"""Meme Referans — reference library of 'white band + caption on top, meme scene below'
Shorts thumbnails from the channels tracked on abd-le-saat.vercel.app.

Two modes:
  publisher  the one PC that owns the archive (DATA/publisher.json exists). It runs the daily update and
             pushes a copy of the archive to the public GitHub repo REPO.
  mirror     every other machine / cloud sandbox. Reads index.json from GitHub (re-synced every 30 min),
             downloads thumbnails on demand. Never updates anything.

Commands:
  update              [publisher] daily job: refresh channels, fetch new thumbnails, OCR, prune, publish
  backfill            [publisher] first-time fill (uses yt-dlp flat listing, dates estimated)
  reocr [--all]       [publisher] OCR pending thumbnails (or redo analysis + OCR for all)
  publish             [publisher] push the archive to GitHub now
  search <input>      compare a video / image / URL (or --text "caption") against the library
  thumbs <id> ...     make sure these thumbnails are on disk (mirror: download) and print their paths
  status              print library stats
"""
import argparse, concurrent.futures as cf, datetime as dt, difflib, io, json, math, os, re
import shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:
    raise SystemExit(f"Missing Python package ({e}). Install with:  pip install numpy pillow")

REPO = os.environ.get("MEMEREF_REPO", "sinsnato97-collab/meme-referans-data")
import html
REMOTE_BASES = [f"https://raw.githubusercontent.com/{REPO}/main",
                f"https://api.github.com/repos/{REPO}/contents",  # needs Accept: vnd.github.raw
                f"https://cdn.jsdelivr.net/gh/{REPO}@main"]        # CDN: may be up to 12 h stale
SYNC_MINUTES = 30

def _pick_data():
    if os.environ.get("MEMEREF_DATA"):
        return Path(os.environ["MEMEREF_DATA"])
    d = Path.home() / "MemeReferans"
    if (d / "publisher.json").exists():  # the publisher PC never falls back to a temp mirror
        return d
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / ".w").write_text("x"); (d / ".w").unlink()
        return d
    except Exception:  # read-only home (some sandboxes)
        return Path(tempfile.gettempdir()) / "MemeReferans"

DATA = _pick_data()
PUBLISHER = (DATA / "publisher.json").exists()
PUB = DATA / "publish"  # publisher: git working copy of REPO
THUMBS = DATA / "thumbs"
INDEX = DATA / "index.json"
CHANNELS = DATA / "channels.json"
STATE = DATA / "state.json"
LOG = DATA / "update.log"
SCRIPTS = Path(__file__).resolve().parent
SITE = "https://abd-le-saat.vercel.app"
WINDOW_DAYS = 30  # keep thumbnails for 30 days; anything older is deleted (on day 31)
# The library was first filled back to this date (with a 23-day window). Older videos are never
# fetched retroactively — the 30-day window fills up naturally as time passes.
FILL_FROM = dt.datetime(2026, 11, 3, 7, 0, tzinfo=dt.timezone.utc)

def ingest_cutoff():
    return max(now_utc() - dt.timedelta(days=WINDOW_DAYS), FILL_FROM)
RECHECK_DAYS = 7
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
YTDLP_FALLBACK = Path.home() / r"AppData\Local\Microsoft\WinGet\Packages\yt-dlp.yt-dlp_Microsoft.Winget.Source_8wekyb3d8bbwe\yt-dlp.exe"
FFMPEG_FALLBACK = Path.home() / r"AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.stdout is None:  # pythonw (scheduled task): no console
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = sys.stdout

_run = subprocess.run
def _run_hidden(*a, **kw):
    kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return _run(*a, **kw)
subprocess.run = _run_hidden


# ----------------------------------------------------------------- utilities
def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def log(msg):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def rotate_log(max_bytes=1_000_000):
    try:
        if LOG.exists() and LOG.stat().st_size > max_bytes:
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            LOG.write_text("\n".join(lines[-3000:]) + "\n", encoding="utf-8")
    except Exception:
        pass

def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(5):  # a concurrent reader (search) can briefly block the replace on Windows
        try:
            os.replace(tmp, path); return
        except PermissionError:
            time.sleep(1 + i)
    os.replace(tmp, path)

def pid_alive(pid):
    """Windows: is a process with this PID still running?"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code)); k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return True

def clean_title(t):
    return (t or "").split(" · ")[0].strip()

def http_get(url, timeout=25, tries=3, headers=None):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))

def tool(name, fallback):
    p = shutil.which(name)
    return p if p else (str(fallback) if Path(fallback).exists() else None)

def ytdlp_cmd():
    """yt-dlp as a command list: exe on PATH / winget, else the Python module (pip install yt-dlp)."""
    exe = tool("yt-dlp", YTDLP_FALLBACK)
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None

def ffmpeg_exe():
    exe = tool("ffmpeg", FFMPEG_FALLBACK)
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # pip install imageio-ffmpeg (bundles a static ffmpeg)
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

def git_exe():
    return tool("git", r"C:\Program Files\Git\cmd\git.exe")


# ----------------------------------------------------------------- mirror (read-only copy from GitHub)
def remote_get(rel, timeout=20, validate=None):
    """Fetch a repo file; returns (bytes, base). `validate` rejects bodies that aren't what we expect
    (captive portals / HTML error pages) so the next base is tried."""
    last = RuntimeError("no remote")
    for base in REMOTE_BASES:
        hdr = {"Accept": "application/vnd.github.raw"} if "api.github.com" in base else None
        try:
            data = http_get(f"{base}/{rel}", timeout=timeout, tries=2, headers=hdr)
            if validate:
                validate(data)
            return data, base
        except Exception as e:
            last = e
    raise last

def write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)

def sync_mirror(force=False):
    """Mirror mode: refresh index/state/channels from GitHub when the local copy is >30 min old."""
    if PUBLISHER:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    fresh = INDEX.exists() and time.time() - INDEX.stat().st_mtime < SYNC_MINUTES * 60
    if fresh and not force:
        return
    retry_soon = time.time() - (SYNC_MINUTES - 5) * 60  # mtime that makes the next run retry in ~5 min
    try:
        got = {}
        for f in (STATE, CHANNELS, INDEX):
            got[f], base = remote_get(f.name, validate=json.loads)
            if "jsdelivr" in base and f is STATE and INDEX.exists():
                # CDN copy may be older than what we already have: keep ours
                if (json.loads(got[f]).get("published") or "") <= (load(STATE, {}).get("published") or ""):
                    os.utime(INDEX, (retry_soon, retry_soon)); return
        for f in (STATE, CHANNELS, INDEX):  # index last: its mtime marks a complete sync
            write_atomic(f, got[f])
        if "jsdelivr" in base:
            os.utime(INDEX, (retry_soon, retry_soon))
    except Exception as e:
        if not INDEX.exists():
            raise SystemExit(f"Could not download the archive from github.com/{REPO}: {e}\n"
                             "The sandbox may block GitHub; allow raw.githubusercontent.com / github.com "
                             "in its network settings.")
        os.utime(INDEX, (retry_soon, retry_soon))  # don't retry slow failures on every command
        print(f"warning: archive refresh failed, using the cached copy ({e})", file=sys.stderr)

def _check_jpeg(data):
    Image.open(io.BytesIO(data)).verify()

def ensure_thumb(vid):
    """Local path of a thumbnail; in mirror mode download it (GitHub copy, then YouTube) if missing."""
    p = THUMBS / f"{vid}.jpg"
    if p.exists() or PUBLISHER:
        return p
    THUMBS.mkdir(parents=True, exist_ok=True)
    try:
        data, _ = remote_get(f"thumbs/{vid}.jpg", validate=_check_jpeg)
    except Exception:
        data = download_thumb(vid)
        try:
            _check_jpeg(data)
        except Exception:
            data = None
    if data:
        write_atomic(p, data)
    return p

def ensure_thumbs(ids):
    ids = list(dict.fromkeys(ids))
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(ensure_thumb, ids))

def require_publisher(cmd):
    if not PUBLISHER:
        raise SystemExit(f"'{cmd}' only runs on the publisher PC (the archive owner). This machine is a "
                         f"read-only mirror of github.com/{REPO}; it is refreshed from there automatically.")


# ----------------------------------------------------------------- image analysis
def row_stats(img):
    g = np.asarray(img.convert("L").resize((108, 192)), dtype=np.int16)
    wf = (g > 225).mean(axis=1)
    df = (g < 70).mean(axis=1)
    return wf, df

def analyze_image(img):
    """Detect the concept layout and find where the meme frame starts."""
    wf, df = row_stats(img)
    h = len(wf)
    top, bottom = slice(0, h // 2), slice(int(h * 0.65), h)
    white_top = float((wf[top] > 0.95).mean())
    text_rows = int(((df[top] > 0.04) & (wf[top] > 0.15)).sum())
    white_bottom = float((wf[bottom] > 0.95).mean())
    concept = white_top >= 0.2 and text_rows >= 2 and white_bottom < 0.3
    # meme start: first row after the white band where 6 consecutive rows look like a picture
    # (lots of mid-tones, little white). Caption glyph rows have only antialiasing mid-tones.
    mid = 1 - wf - df
    pic = (mid > 0.45) & (wf < 0.5)
    meme_start = None
    first_white = next((y for y in range(h) if wf[y] > 0.95), None)
    if first_white is not None and first_white < h * 0.6:
        for y in range(first_white, int(h * 0.85)):
            if pic[y:y + 6].all():
                meme_start = y
                break
    return {"concept": concept, "white_top": round(white_top, 3),
            "meme_start": round(meme_start / h, 3) if meme_start is not None else None}

def trim_uniform(a, thr=10):
    """Trim rows/cols that are nearly uniform (black/white bars)."""
    rows = np.where(a.std(axis=1) > thr)[0]
    cols = np.where(a.std(axis=0) > thr)[0]
    if len(rows) < 8 or len(cols) < 8:
        return a
    return a[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]

def dhash(arr, size=16):
    im = Image.fromarray(arr.astype(np.uint8)).resize((size + 1, size), Image.LANCZOS)
    a = np.asarray(im, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return "".join("1" if b else "0" for b in bits)

def meme_hash(img, info):
    g = np.asarray(img.convert("L"), dtype=np.int16)
    H = g.shape[0]
    start = int((info.get("meme_start") or 0) * H)
    crop = trim_uniform(g[start:])
    return dhash(crop)

def full_hash(img):
    return dhash(trim_uniform(np.asarray(img.convert("L"), dtype=np.int16)))

def hamming(a, b):
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(x != y for x, y in zip(a, b)) / len(a)


# ----------------------------------------------------------------- OCR (Windows.Media.Ocr via PowerShell; tesseract elsewhere)
def ocr_engine():
    if sys.platform == "win32":
        return "windows"
    return "tesseract" if shutil.which("tesseract") else "none"

def ocr_tesseract(paths):
    out = {}
    for p in paths:
        try:
            h = Image.open(p).size[1]
            r = subprocess.run(["tesseract", p, "stdout", "-l", "eng", "tsv"], capture_output=True,
                               text=True, timeout=60, encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = {}
        for row in r.stdout.splitlines()[1:]:
            c = row.split("\t")
            if len(c) < 12 or not c[11].strip() or c[10] == "-1":
                continue
            key = (c[2], c[3], c[4])  # block, paragraph, line
            lines.setdefault(key, []).append((int(c[6]), int(c[7]), int(c[9]), c[11]))
        res = []
        for words in lines.values():
            words.sort()
            y = sum(t + hh / 2 for _, t, hh, _ in words) / len(words) / h
            res.append({"text": " ".join(w for *_, w in words), "y": round(y, 4)})
        out[p] = sorted(res, key=lambda l: l["y"])
    return out

def ocr_files(paths):
    """Return {path: [ {text, y} ... ]} using Windows OCR (en-US preferred) or tesseract."""
    if not paths:
        return {}
    eng = ocr_engine()
    if eng == "tesseract":
        return ocr_tesseract(paths)
    if eng == "none":
        return {}
    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / "list.txt"; out = Path(td) / "out.json"
        lst.write_text("\n".join(str(p) for p in paths), encoding="utf-8")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(SCRIPTS / "ocr.ps1"), "-List", str(lst), "-Out", str(out)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120 + 5 * len(paths))
        except Exception as e:
            log(f"OCR batch failed ({len(paths)} files): {e}")
            return {}
        try:
            res = json.loads(out.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    if isinstance(res, dict):
        res = [res]
    outd = {}
    for r in res:
        if r.get("error"):  # failed file: leave it out so it stays pending and is retried
            continue
        lines = r.get("lines") or []
        if isinstance(lines, dict):
            lines = [lines]
        outd[r["path"]] = [{"text": l.get("text", ""), "y": float(l.get("y", 0))} for l in lines]
    return outd

def split_text(lines, meme_start):
    """Caption = OCR lines above the meme frame; rest = overlay text inside the meme."""
    cut = meme_start if meme_start else 0.5
    top = [l["text"] for l in lines if l["y"] < cut]
    rest = [l["text"] for l in lines if l["y"] >= cut]
    return " ".join(top).strip(), " ".join(rest).strip()


# ----------------------------------------------------------------- text similarity
STOP = set("a an the and or but of to in on at for is are was were be been it its this that with as by i im you your he she they we me my bro his her their our when how what who just so".split())

def norm(s):
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9' ğüşıöç]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def tokens(s):
    return {t for t in norm(s).split() if t not in STOP and len(t) > 1}

def same_word(a, b):
    """Exact or same stem: licence~license, sliming~slime (shared prefix >= max(4, shorter-2))."""
    if a == b:
        return True
    p = 0
    for x, y in zip(a, b):
        if x != y:
            break
        p += 1
    return p >= max(4, min(len(a), len(b)) - 2)

def make_idf(texts):
    df = {}
    for t in texts:
        for w in tokens(t):
            df[w] = df.get(w, 0) + 1
    n = len(texts)
    return lambda w: math.log((n + 1) / (df.get(w, 0) + 1)) + 1

def text_sim(a, b, idf=lambda w: 1.0):
    """Rare-word-weighted, stem-tolerant overlap (shared rare words like 'licence', 'slime' count most),
    plus plain character similarity when it is clearly high (near-identical captions)."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = tokens(a), tokens(b)
    w = 0.0
    if ta and tb:
        ma = {x for x in ta if any(same_word(x, y) for y in tb)}
        mb = {y for y in tb if any(same_word(x, y) for x in ta)}
        w = (sum(map(idf, ma)) + sum(map(idf, mb))) / (sum(map(idf, ta)) + sum(map(idf, tb)))
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    return round(max(w, seq if seq >= 0.7 else 0.0), 3)


# ----------------------------------------------------------------- channels
def fetch_site_channels():
    chans = {}
    for url in (f"{SITE}/data/monthly.json", f"{SITE}/api/weekly"):
        try:
            data = json.loads(http_get(url))
        except Exception as e:
            log(f"site fetch failed {url}: {e}")
            continue
        for c in data.get("channels", []):
            if c.get("id") and c["id"] not in chans:
                chans[c["id"]] = {"title": clean_title(c.get("title")), "shorts30": int(c.get("shortsCount") or 0)}
    return chans

def rss_shorts(cid):
    xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}").decode("utf-8", "ignore")
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)", e)
        pub = re.search(r"<published>([^<]+)", e)
        link = re.search(r'<link rel="alternate" href="([^"]+)"', e)
        title = re.search(r"<title>([^<]*)", e)
        if vid and pub and link and "/shorts/" in link.group(1):
            out.append({"id": vid.group(1), "published": iso(parse_iso(pub.group(1))),
                        "title": html.unescape(title.group(1)) if title else ""})
    return out

def download_thumb(vid):
    for name in ("oardefault", "oar2"):
        try:
            return http_get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg")
        except Exception:
            continue
    return None

def classify_channel(cid):
    shorts = rss_shorts(cid)[:8]
    n = m = 0
    for s in shorts:
        data = download_thumb(s["id"])
        if not data:
            continue
        n += 1
        m += analyze_image(Image.open(io.BytesIO(data)))["concept"]
        time.sleep(0.3)
    return n, m

def refresh_channels(force=False):
    chans = load(CHANNELS, {})
    site = fetch_site_channels()
    for cid, c in site.items():
        chans.setdefault(cid, {}).update(c)
    todo = []
    cutoff = now_utc() - dt.timedelta(days=RECHECK_DAYS)
    for cid, c in chans.items():
        if force or "checked_at" not in c or parse_iso(c["checked_at"]) < cutoff:
            todo.append(cid)
    if todo:
        log(f"classifying {len(todo)} channels")
    def work(cid):
        try:
            return cid, classify_channel(cid)
        except Exception as e:
            return cid, e
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for cid, res in ex.map(work, todo):
            c = chans[cid]
            if isinstance(res, Exception):
                log(f"  classify failed {c.get('title')}: {res}")
                continue
            n, m = res
            if n == 0:  # no Shorts / thumbnails right now: keep the previous decision, retry next time
                continue
            ratio = m / n
            was = c.get("concept", False)
            # hysteresis: join at >=50%, leave only below 25%
            c["concept"] = (ratio >= 0.5 and n >= 2) or (was and ratio >= 0.25)
            c["ratio"] = round(ratio, 2); c["sampled"] = n
            c["checked_at"] = iso(now_utc())
    save(CHANNELS, chans)
    return chans


# ----------------------------------------------------------------- listing via yt-dlp
def ytdlp_list(cid, n):
    exe = tool("yt-dlp", YTDLP_FALLBACK)
    if not exe:
        log("yt-dlp not found"); return []
    cmd = [exe, "--flat-playlist", "--print", "id", "--playlist-end", str(n),
           "--sleep-requests", "1", f"https://www.youtube.com/channel/{cid}/shorts"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        return [l.strip() for l in r.stdout.splitlines() if re.fullmatch(r"[\w-]{11}", l.strip())]
    except Exception as e:
        log(f"yt-dlp list failed {cid}: {e}")
        return []


# ----------------------------------------------------------------- ingest
def ingest(items, index, chans):
    """items: list of dict(id, channel_id, published, estimated, title). Downloads + analyzes + OCR."""
    THUMBS.mkdir(parents=True, exist_ok=True)
    added = []
    def dl(it):
        data = download_thumb(it["id"])
        time.sleep(0.25)
        return it, data
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for it, data in ex.map(dl, items):
            if not data:
                continue
            try:
                img = Image.open(io.BytesIO(data)); img.load()
                info = analyze_image(img)
                hm, hf = meme_hash(img, info), full_hash(img)
            except Exception as e:
                log(f"  bad thumbnail {it['id']}: {e}")
                continue
            (THUMBS / f"{it['id']}.jpg").write_bytes(data)
            index[it["id"]] = {
                "channel_id": it["channel_id"], "channel": chans.get(it["channel_id"], {}).get("title", ""),
                "published": it["published"], "date_estimated": it.get("estimated", False),
                "added": iso(now_utc()), "yt_title": it.get("title", ""),
                "concept": info["concept"], "meme_start": info["meme_start"],
                "hash_meme": hm, "hash_full": hf,
                "caption": "", "overlay": "",
            }
            added.append(it["id"])
    save(INDEX, index)
    # OCR everything still pending (new items + anything a previous run didn't finish)
    run_ocr(index, [v for v, e in index.items() if not e.get("ocr")])
    return added

def prune(index):
    cutoff = now_utc() - dt.timedelta(days=WINDOW_DAYS)
    gone = [v for v, e in index.items() if parse_iso(e["published"]) < cutoff]
    for v in gone:
        (THUMBS / f"{v}.jpg").unlink(missing_ok=True)
        del index[v]
    # orphan files
    for f in THUMBS.glob("*.jpg"):
        if f.stem not in index:
            f.unlink(missing_ok=True)
    return len(gone)


# ----------------------------------------------------------------- commands
def wait_for_network(max_wait=600):
    """After boot / wake the network may not be up yet; wait up to 10 min for YouTube to answer."""
    t0 = time.time()
    while True:
        try:
            http_get("https://www.youtube.com/feeds/videos.xml?channel_id=UCxULzGyhtIUotnMIM4G4-rw", timeout=15, tries=1)
            return True
        except Exception:
            if time.time() - t0 > max_wait:
                return False
            time.sleep(30)

def cmd_update(args):
    t0 = time.time()
    rotate_log()
    if not wait_for_network():
        log("update skipped: no network for 10 minutes")
        raise SystemExit(1)  # non-zero result so Task Scheduler shows the failure
    state = load(STATE, {})
    index = load(INDEX, {})
    chans = refresh_channels(force=args.reclassify)
    concept = {cid: c for cid, c in chans.items() if c.get("concept")}
    cutoff = ingest_cutoff()
    items, gaps = [], 0
    for cid, c in concept.items():
        try:
            rss = rss_shorts(cid)
        except Exception as e:
            log(f"  rss failed {c.get('title')}: {e}"); continue
        last_ok = c.get("last_ok")
        new = [s for s in rss if s["id"] not in index and parse_iso(s["published"]) > cutoff]
        for s in new:
            items.append({**s, "channel_id": cid})
        rss_ids = {s["id"] for s in rss}
        per_day = max(c.get("shorts30", 0), 1) / 30
        if not last_ok:
            # channel newly joined the concept list: fill its window like a backfill (dates estimated)
            gaps += 1
            now = now_utc()
            days = (now - cutoff).total_seconds() / 86400
            for i, vid in enumerate(ytdlp_list(cid, min(400, math.ceil(per_day * days * 1.15) + 5))):
                if vid in rss_ids or vid in index:
                    continue
                est = now - dt.timedelta(days=(i + 0.5) / per_day)
                if est > cutoff:
                    items.append({"id": vid, "channel_id": cid, "published": iso(est), "estimated": True})
        elif rss and len(rss) >= 10 and parse_iso(rss[-1]["published"]) > parse_iso(last_ok):
            # gap: RSS (15 newest) doesn't reach back to the previous run -> fill with yt-dlp.
            # Size the listing by how long we've been away (PC off for days, prolific channels).
            gaps += 1
            oldest, prev = parse_iso(rss[-1]["published"]), parse_iso(last_ok)
            away_days = (now_utc() - prev).total_seconds() / 86400
            n = min(400, math.ceil(per_day * away_days * 1.5) + 20)
            gap_ids = []
            for vid in ytdlp_list(cid, n):
                if vid in rss_ids:
                    continue
                if vid in index:
                    break
                gap_ids.append(vid)
            for k, vid in enumerate(gap_ids):  # spread estimated dates evenly between prev run and oldest RSS item
                est = oldest - (oldest - prev) * (k + 1) / (len(gap_ids) + 1)
                if est > cutoff:
                    items.append({"id": vid, "channel_id": cid, "published": iso(est), "estimated": True})
        c["last_ok"] = iso(now_utc())
        time.sleep(0.4)
    save(CHANNELS, chans)
    for e in index.values():  # keep channel names tidy (site sometimes sends junk after " · ")
        e["channel"] = clean_title(chans.get(e["channel_id"], {}).get("title") or e["channel"])
        if "&" in e.get("yt_title", ""):
            e["yt_title"] = html.unescape(e["yt_title"])
    seen = set(); items = [i for i in items if not (i["id"] in seen or seen.add(i["id"]))]
    added = ingest(items, index, chans)
    removed = prune(index)
    save(INDEX, index)
    state["last_update"] = iso(now_utc())
    save(STATE, state)
    log(f"update done: concept channels={len(concept)} new={len(added)} gap-fills={gaps} pruned={removed} "
        f"total={len(index)} ({time.time() - t0:.0f}s)")
    try:
        publish()
    except Exception as e:
        log(f"publish failed: {e}")


# ----------------------------------------------------------------- publish (publisher -> GitHub)
PUB_THUMB = (360, 640)  # GitHub copy is downscaled: plenty for eyeballing, fingerprints are precomputed

README = """# Meme Referans arşivi

Bu depo otomatik güncellenir (her gün, tek bir bilgisayardan). Elle değiştirmeyin.

- `index.json`: her Shorts kapağı için kanal, tarih, OCR'lı üst yazı, görsel parmak izleri
- `thumbs/<videoId>.jpg`: son 30 günün kapakları (360x640)
- `skill/`: `meme-referans` skillinin güncel kopyası

Son güncelleme: {when}
"""

def git(*args, check=True):
    exe = git_exe()
    if not exe:
        raise RuntimeError("git not found (winget install Git.Git)")
    r = subprocess.run([exe, "-C", str(PUB), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()

def publish():
    """Push index + downscaled thumbs + the skill to the public repo as a single (amended) commit,
    so the repo never accumulates history: only the current 30-day window is stored."""
    t0 = time.time()
    if not (PUB / ".git").exists():
        PUB.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([git_exe(), "clone", "--depth", "1", f"https://github.com/{REPO}.git", str(PUB)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"git clone: {r.stderr.strip()[-400:]}")
    # a git killed mid-run (timeout, shutdown) leaves index.lock behind; our own .lock already
    # guarantees a single job, so a leftover one is always stale
    (PUB / ".git" / "index.lock").unlink(missing_ok=True)
    git("config", "user.name", "meme-referans")
    git("config", "user.email", f"{REPO.split('/')[0]}@users.noreply.github.com")
    git("config", "core.autocrlf", "false")
    index = load(INDEX, {})
    pt = PUB / "thumbs"; pt.mkdir(exist_ok=True)
    made = 0
    for vid in index:
        dst, src = pt / f"{vid}.jpg", THUMBS / f"{vid}.jpg"
        if dst.exists() or not src.exists():
            continue
        im = Image.open(src).convert("RGB"); im.thumbnail(PUB_THUMB, Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=82, optimize=True)
        write_atomic(dst, buf.getvalue()); made += 1
    for f in pt.glob("*.tmp"):  # leftovers of an interrupted run
        f.unlink(missing_ok=True)
    for f in pt.glob("*.jpg"):
        if f.stem not in index:
            f.unlink()
    slim = {v: {k: x for k, x in e.items() if k != "lines"} for v, e in index.items()}
    st = load(STATE, {}); st["published"] = iso(now_utc())
    save(PUB / "index.json", slim); save(PUB / "state.json", st)
    shutil.copyfile(CHANNELS, PUB / "channels.json")
    skill_src = SCRIPTS.parent
    skill_dst = PUB / "skill"
    shutil.rmtree(skill_dst, ignore_errors=True)
    shutil.copytree(skill_src, skill_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    dirs_exist_ok=True)
    (PUB / "README.md").write_text(README.format(when=st["published"]), encoding="utf-8")
    git("add", "-A")
    has_head = git("rev-parse", "--verify", "HEAD", check=False) != ""
    git("commit", "-q", "--amend" if has_head else "-q", "-m", f"archive {st['published']}")
    git("push", "-q", "--force", "origin", "HEAD:main")
    if made > 300 or datetime_weekday() == 0:  # drop the amended-away objects now and then
        git("reflog", "expire", "--expire=now", "--all", check=False)
        git("gc", "-q", "--prune=now", check=False)
    save(STATE, {**load(STATE, {}), "published": st["published"]})
    log(f"published to github.com/{REPO}: thumbs={len(index)} new={made} ({time.time() - t0:.0f}s)")

def datetime_weekday():
    return dt.datetime.now().weekday()

def cmd_publish(args):
    publish()

def cmd_backfill(args):
    t0 = time.time()
    index = load(INDEX, {})
    chans = refresh_channels(force=False)
    concept = {cid: c for cid, c in chans.items() if c.get("concept")}
    now = now_utc(); cutoff = ingest_cutoff()
    log(f"backfill: {len(concept)} concept channels")
    all_items = []
    for k, (cid, c) in enumerate(concept.items(), 1):
        per_day = max(c.get("shorts30", 0), 1) / 30
        n = min(400, math.ceil(per_day * WINDOW_DAYS * 1.15) + 5)
        try:
            rss = {s["id"]: s for s in rss_shorts(cid)}
        except Exception:
            rss = {}
        ids = ytdlp_list(cid, n)
        for i, vid in enumerate(ids):
            if vid in index:
                continue
            if vid in rss:
                it = {**rss[vid], "channel_id": cid}
            else:
                est = now - dt.timedelta(days=(i + 0.5) / per_day)
                it = {"id": vid, "channel_id": cid, "published": iso(est), "estimated": True}
            if parse_iso(it["published"]) > cutoff:
                all_items.append(it)
        c["last_ok"] = iso(now)
        log(f"  [{k}/{len(concept)}] {c.get('title')}: listed {len(ids)}")
    save(CHANNELS, chans)
    log(f"downloading {len(all_items)} thumbnails")
    added = ingest(all_items, index, chans)
    prune(index); save(INDEX, index)
    st = load(STATE, {}); st["last_backfill"] = iso(now_utc()); save(STATE, st)
    log(f"backfill done: added={len(added)} total={len(index)} ({time.time() - t0:.0f}s)")

def run_ocr(index, ids, batch_size=100):
    """OCR the given ids in batches, saving after each batch (resumable via the 'ocr' flag)."""
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        res = ocr_files([str(THUMBS / f"{v}.jpg") for v in batch])
        for v in batch:
            key = str(THUMBS / f"{v}.jpg")
            if key not in res:
                continue  # OCR failed for this file; stays pending for the next run
            index[v]["lines"] = res[key]
            cap, ov = split_text(res[key], index[v]["meme_start"])
            index[v]["caption"], index[v]["overlay"], index[v]["ocr"] = cap, ov, True
        save(INDEX, index)
        print(f"  ocr {min(i + batch_size, len(ids))}/{len(ids)}", flush=True)

def cmd_reocr(args):
    index = load(INDEX, {})
    if args.all:  # also redo layout analysis + fingerprints
        for v, e in index.items():
            img = Image.open(THUMBS / f"{v}.jpg")
            info = analyze_image(img)
            e.update(concept=info["concept"], meme_start=info["meme_start"],
                     hash_meme=meme_hash(img, info), hash_full=full_hash(img))
    ids = list(index) if args.all else [v for v, e in index.items() if not e.get("ocr")]
    run_ocr(index, ids)
    log(f"OCR done: {len(ids)} thumbnails ({'all' if args.all else 'pending only'})")

def cmd_status(args):
    sync_mirror(force=getattr(args, "sync", False))
    index = load(INDEX, {}); chans = load(CHANNELS, {}); st = load(STATE, {})
    concept = [c for c in chans.values() if c.get("concept")]
    no_cap = sum(1 for e in index.values() if not e.get("caption"))
    print(json.dumps({
        "mode": "publisher" if PUBLISHER else "mirror", "repo": f"https://github.com/{REPO}",
        "ocr_engine": ocr_engine(), "published": st.get("published"),
        "data_dir": str(DATA), "index_file": str(INDEX), "thumbnails": len(index), "concept_channels": len(concept),
        "channels_known": len(chans), "without_caption_text": no_cap,
        "last_update": st.get("last_update"), "last_backfill": st.get("last_backfill"),
        "oldest": min((e["published"] for e in index.values()), default=None),
        "newest": max((e["published"] for e in index.values()), default=None),
    }, ensure_ascii=False, indent=1))


# ----------------------------------------------------------------- search
def get_input_frames(src, workdir):
    """Return list of PIL images from a video/image path or URL."""
    p = Path(src)
    if not p.exists() and re.match(r"https?://", src):
        yt = ytdlp_cmd()
        if not yt:
            raise SystemExit("yt-dlp is not installed (pip install yt-dlp). Send the video file instead.")
        out = Path(workdir) / "input.%(ext)s"
        r = subprocess.run([*yt, "-f", "mp4/best", "-o", str(out), src], capture_output=True, timeout=300)
        got = [g for g in Path(workdir).glob("input.*") if g.suffix not in (".part", ".ytdl", ".temp")]
        if r.returncode != 0:
            got = []
        if not got:
            raise SystemExit("Could not download the URL (Instagram may need login, or the sandbox blocks "
                             "the site). Send the video file instead.")
        p = got[0]
    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        im = Image.open(p).convert("RGB")
        fdir = Path(workdir) / "frames"; fdir.mkdir(exist_ok=True)
        im.save(fdir / "f_001.jpg", quality=92)  # so workdir/frames is always there to look at
        return [im]
    ff = ffmpeg_exe()
    if not ff:
        raise SystemExit("ffmpeg not found. Install it (pip install imageio-ffmpeg, or apt/winget ffmpeg), "
                         "or send a screenshot of the video instead.")
    fdir = Path(workdir) / "frames"; fdir.mkdir(exist_ok=True)
    # sample ~30 frames spread over the WHOLE clip (the YouTube thumbnail may be any moment of it)
    dur = 0.0
    try:  # ffmpeg prints "Duration: hh:mm:ss.xx" for -i without output (no ffprobe needed)
        r = subprocess.run([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    fps = 2 if dur <= 15 else 30 / dur
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", str(p), "-vf", f"fps={fps:.3f}",
                    str(fdir / "f_%03d.jpg")], capture_output=True, timeout=600)
    frames = sorted(fdir.glob("f_*.jpg"))
    if not frames:
        raise SystemExit("ffmpeg produced no frames")
    return [Image.open(f).convert("RGB") for f in frames]

def make_sheet(query_img, cands, path):
    w, h = 216, 384
    cols = 6
    def thumb_img(c):
        try:
            return Image.open(THUMBS / f"{c['id']}.jpg").convert("RGB")
        except Exception:  # could not be downloaded
            return Image.new("RGB", (w, h), (220, 220, 220))
    tiles = [("SORGU", query_img)] + [(f"#{i+1} {c['channel'][:14]}", thumb_img(c)) for i, c in enumerate(cands)]
    rows = math.ceil(len(tiles) / cols)
    S = Image.new("RGB", (cols * w, rows * (h + 22)), "white")
    d = ImageDraw.Draw(S)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
    for i, (label, im) in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * (h + 22)
        S.paste(im.resize((w, h)), (x, y + 22))
        d.text((x + 4, y + 2), label, fill="red" if i == 0 else "black", font=font)
    S.save(path, quality=88)

def cleanup_old_workdirs(days=2):
    for d in Path(tempfile.gettempdir()).glob("memeref_*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

def cmd_thumbs(args):
    sync_mirror()
    for vid, p in zip(args.ids, ensure_thumbs(args.ids)):
        print(json.dumps({"id": vid, "thumb": str(p), "exists": p.exists(),
                          "url": f"https://www.youtube.com/shorts/{vid}"}, ensure_ascii=False))

def cmd_search(args):
    sync_mirror()
    index = load(INDEX, {})
    if not index:
        raise SystemExit("Library is empty — run backfill first." if PUBLISHER else
                         f"Mirror archive is empty — check github.com/{REPO}.")
    cleanup_old_workdirs()
    q_caption, q_overlay, q_hashes, q_full, sheet_img = args.text or "", "", [], [], None
    work = Path(tempfile.mkdtemp(prefix="memeref_"))
    if args.input:
        frames = get_input_frames(args.input, work)
        # analyze every frame; OCR a handful spread across the clip
        infos = [analyze_image(f) for f in frames]
        for f, info in zip(frames, infos):
            q_hashes.append(meme_hash(f, info)); q_full.append(full_hash(f))
        pick = sorted(set([0, len(frames) // 4, len(frames) // 2, (3 * len(frames)) // 4, len(frames) - 1]))
        paths = []
        for i in pick:
            fp = work / f"ocr_{i:03d}.png"; frames[i].save(fp); paths.append((i, fp))
        res = ocr_files([str(p) for _, p in paths])
        best = ("", "")
        for i, fp in paths:
            cap, ov = split_text(res.get(str(fp), []), infos[i]["meme_start"])
            if len(cap) > len(best[0]):
                best = (cap, ov); sheet_img = frames[i]
        if not args.text:
            q_caption, q_overlay = best
        sheet_img = sheet_img or frames[len(frames) // 2]
    idf = make_idf([e.get("caption", "") for e in index.values()])
    qh = [int(h, 2) for h in q_hashes + q_full]
    nbits = len(q_hashes[0]) if q_hashes else 256
    scored = []
    for vid, e in index.items():
        ts = max(text_sim(q_caption, e.get("caption", ""), idf),
                 0.8 * text_sim(q_caption, e.get("yt_title", ""), idf)) if q_caption else 0.0
        refs = [int(h, 2) for h in (e.get("hash_meme"), e.get("hash_full")) if h]
        best = min((bin(a ^ b).count("1") for a in qh for b in refs), default=nbits)  # py<3.10 safe
        img_sim = 1 - best / nbits
        scored.append({"id": vid, "channel": e["channel"], "published": e["published"][:10],
                       "date_estimated": e.get("date_estimated", False),
                       "caption": e.get("caption", ""), "yt_title": e.get("yt_title", ""),
                       "text_score": ts, "image_score": round(img_sim, 3),
                       "url": f"https://www.youtube.com/shorts/{vid}",
                       "thumb": str(THUMBS / f"{vid}.jpg")})
    by_text = sorted(scored, key=lambda x: -x["text_score"])[: args.top]
    by_img = sorted(scored, key=lambda x: -x["image_score"])[: args.top]
    cands, seen = [], set()
    for c in [x for pair in zip(by_text, by_img) for x in pair]:
        if c["id"] not in seen:
            seen.add(c["id"]); cands.append(c)
    cands = cands[: args.top * 2]
    ensure_thumbs([c["id"] for c in cands])
    sheet = None
    if sheet_img is not None or cands:
        sheet = str(work / "candidates.jpg")
        make_sheet(sheet_img or Image.new("RGB", (216, 384), "white"), cands, sheet)
    note = None
    if args.input and not args.text and not q_caption:
        note = ("query_caption is empty (no OCR on this machine, or it read nothing). Look at the frames in "
                "workdir/frames, read the top caption yourself and rerun: search <input> --text \"<caption>\"")
    print(json.dumps({"mode": "publisher" if PUBLISHER else "mirror", "ocr_engine": ocr_engine(), "note": note,
                      "query_caption": q_caption, "query_overlay": q_overlay, "frames": len(q_hashes),
                      "library_size": len(index), "candidates_sheet": sheet, "workdir": str(work),
                      "candidates": cands}, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--reclassify", action="store_true")
    sub.add_parser("backfill")
    sub.add_parser("publish")
    stp = sub.add_parser("status"); stp.add_argument("--sync", action="store_true", help="mirror: re-download now")
    ro = sub.add_parser("reocr"); ro.add_argument("--all", action="store_true")
    s = sub.add_parser("search"); s.add_argument("input", nargs="?"); s.add_argument("--text"); s.add_argument("--top", type=int, default=12)
    th = sub.add_parser("thumbs"); th.add_argument("ids", nargs="+")
    argv = sys.argv[1:]
    if argv[:1] == ["thumbs"] and "--" not in argv:  # video ids may start with "-" (e.g. --qrAMGzmlI)
        argv = ["thumbs", "--", *argv[1:]]
    if argv[:1] == ["search"]:  # same for --text "-something" -> --text=-something
        fixed, i = [], 0
        while i < len(argv):
            if argv[i] == "--text" and i + 1 < len(argv):
                fixed.append("--text=" + argv[i + 1]); i += 2
            else:
                fixed.append(argv[i]); i += 1
        argv = fixed
    args = ap.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    if args.cmd in ("update", "backfill", "reocr", "publish"):
        require_publisher(args.cmd)
        lock = DATA / ".lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
        except FileExistsError:
            try:
                owner = int(lock.read_text().strip() or 0)
            except Exception:
                owner = 0
            age = time.time() - lock.stat().st_mtime
            if owner and pid_alive(owner) and age < 3 * 3600:
                log(f"{args.cmd}: another job (pid {owner}) is running — skipped")
                return
            lock.write_text(str(os.getpid()))  # stale lock (process gone / killed): take it over
        try:
            {"update": cmd_update, "backfill": cmd_backfill, "reocr": cmd_reocr,
             "publish": cmd_publish}[args.cmd](args)
        finally:
            lock.unlink(missing_ok=True)
        return
    {"status": cmd_status, "search": cmd_search, "thumbs": cmd_thumbs}[args.cmd](args)

if __name__ == "__main__":
    main()
