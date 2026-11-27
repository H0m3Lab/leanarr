#!/usr/bin/env python3
"""Leanarr - seed-aware library shrinker (by H0m3Lab).

Re-encodes old, inefficient video (H.264, MPEG-2, VC-1, Xvid...) in a Plex/Sonarr/Radarr
library to HEVC, but ONLY files no torrent still needs, and only when the result is
verified and clearly smaller.
  * fail-closed qBittorrent protection (checked before encoding AND right before replacing)
  * optional "release": stopped torrents that met their tracker's seeding rule are removed
    from qBittorrent (files kept) so their files become eligible
  * local CPU worker (libx265, night window, nice 19) and REMOTE workers (e.g. a PC with an
    NVIDIA GPU running leanarr_worker.py): they fetch a job over HTTP, encode, upload the
    result; this server does every check and the safe replacement itself
  * editable settings page (encoders, window, checks), report mode by default
  * optional audio step for MKVs: add a Dolby (E-AC-3 / AC-3) track made from the DTS track when no kept track is
    Dolby (for TVs/PCs that can't play DTS), and remove commentary / other-language tracks; same seeding protection,
    verify and safe replace (done during a shrink too, so a file is rewritten once)

Run:  python3 leanarr.py            (daemon: web UI + API + scanner + local worker)
      python3 leanarr.py --scan     (one scan, print the report, exit)
      python3 leanarr.py --once     (one scan + shrink everything eligible now with the local worker)
Environment variables give the defaults; the settings page overrides them (stored in the DB).
"""
import argparse, datetime as dt, html, json, os, re, shlex, shutil, sqlite3, subprocess, threading, time
import urllib.error, urllib.parse, urllib.request, http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.4.0"


def env(name, default):
    return os.environ.get(name, default)


# fixed (deployment) settings: environment only
CONFIG = {
    "libraries": [p.strip() for p in env("LEANARR_LIBRARIES", "/media/tv,/media/movies").split(",") if p.strip()],
    "db": env("LEANARR_DB", "/config/leanarr.db"),
    "work_dir": env("LEANARR_WORK_DIR", "/work"),
    "nice": env("LEANARR_NICE", "19"),
    "scan_hours": float(env("LEANARR_SCAN_HOURS", "6")),
    "settle_minutes": int(env("LEANARR_SETTLE_MINUTES", "60")),
    "port": int(env("LEANARR_PORT", "8282")),
    "worker_token": env("LEANARR_WORKER_TOKEN", ""),
    "qbit_url": env("QBIT_URL", "http://qbittorrent:8080").rstrip("/"),
    "qbit_user": env("QBIT_USER", ""),
    "qbit_password": env("QBIT_PASSWORD", ""),
    "sonarr_url": env("SONARR_URL", "").rstrip("/"),
    "sonarr_key": env("SONARR_API_KEY", ""),
    "radarr_url": env("RADARR_URL", "").rstrip("/"),
    "radarr_key": env("RADARR_API_KEY", ""),
}

# editable settings: (default, kind, label, help)
SETTINGS = {
    "mode": (env("LEANARR_MODE", "report"), ["report", "shrink"], "Mode",
             "report = only list what would be shrunk; shrink = really do it"),
    "window": (env("LEANARR_WINDOW", "01:00-09:00"), "text", "Local worker hours",
               "HH:MM-HH:MM (local time), 'always' or 'never'. The CPU worker on this server only runs inside it."),
    "remote_window": (env("LEANARR_REMOTE_WINDOW", "always"), "text", "Remote worker hours",
                      "When remote workers (GPU PC) may take jobs: HH:MM-HH:MM, 'always' or 'never'."),
    "local_worker": (env("LEANARR_LOCAL_WORKER", "true"), "bool", "Use this server's CPU",
                     "Encode with libx265 on this server (lowest priority) in the window above."),
    "x265_crf": (env("LEANARR_CRF", "22"), "int:0:51", "x265 quality (CRF)",
                 "Lower = better quality, bigger file. 22 = old Tdarr setting; 24-26 = smaller."),
    "x265_preset": (env("LEANARR_PRESET", "slow"),
                    ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                    "x265 speed preset", "Slower = smaller at the same quality, much more CPU time."),
    "x265_extra": ("", "text", "x265 extra ffmpeg options", "Added after the x265 options, e.g. -tune grain"),
    "nvenc_cq": (env("LEANARR_NVENC_CQ", "24"), "int:0:51", "NVENC quality (CQ)",
                 "For GPU workers (NVIDIA). Lower = better quality, bigger file."),
    "nvenc_preset": (env("LEANARR_NVENC_PRESET", "p7"), ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
                     "NVENC preset", "p7 = slowest/best. Even p7 is many times faster than x265 on a CPU."),
    "nvenc_extra": ("-tune hq -rc vbr -b:v 0 -spatial-aq 1 -temporal-aq 1 -rc-lookahead 32 -bf 4 -b_ref_mode middle",
                    "text", "NVENC extra ffmpeg options", "Defaults suit RTX 20/30/40 cards."),
    "min_saving": (env("LEANARR_MIN_SAVING", "0.10"), "float:0:0.9", "Minimum saving",
                   "Keep a result only if at least this much smaller (0.10 = 10%)."),
    "min_size_mb": (env("LEANARR_MIN_SIZE_MB", "200"), "int:0:100000", "Ignore files smaller than (MB)", ""),
    "skip_codecs": ("hevc,av1,vp9", "text", "Never re-encode these codecs", "Comma-separated ffprobe codec names."),
    "skip_hdr": ("true", "bool", "Skip HDR10 / HLG / Dolby Vision", "Re-encoding can lose HDR/DV metadata."),
    "qbit_protection": ("true", "bool", "Protect files used by torrents (qBittorrent check)",
                        "STRONGLY recommended. Off = files that are still seeding can be replaced, which breaks "
                        "those torrents (hit & runs on private trackers)."),
    "release_seeds": (env("LEANARR_RELEASE_SEEDS", "false"), "bool", "Release finished seeds",
                      "Remove stopped torrents that met their tracker's seeding rule from qBittorrent "
                      "(files kept), so their files can be shrunk. Runs on every scan, in report mode too "
                      "(it never touches files on disk)."),
    "lease_hours": ("12", "int:1:168", "Remote job timeout (hours)",
                    "A remote job that doesn't report back in time goes back into the queue."),
    "dolby_mode": (env("LEANARR_DOLBY_MODE", "off"), ["off", "report", "add"], "Dolby track for DTS-only files",
                   "Many TVs' built-in apps can't play DTS (silence, or the server has to transcode). 'add' writes a "
                   "Dolby track made from the DTS track as the first, default audio track; the DTS track(s) stay in "
                   "the file. Only MKV files with DTS and no Dolby (AC-3 / E-AC-3 / TrueHD) track, never a file a "
                   "torrent uses. Longest files first (movies before episodes). 'report' only counts them. "
                   "Independent of the shrink mode."),
    "dolby_codec": (env("LEANARR_DOLBY_CODEC", "eac3"), ["eac3", "ac3"], "Dolby track format",
                    "eac3 = Dolby Digital Plus, ac3 = Dolby Digital (oldest, plays on everything). Max 5.1 channels."),
    "dolby_bitrate": (env("LEANARR_DOLBY_BITRATE", "640k"), ["384k", "448k", "640k"], "Dolby track bitrate", ""),
    "dolby_window": (env("LEANARR_DOLBY_WINDOW", "always"), "text", "Audio track hours",
                     "HH:MM-HH:MM, 'always' or 'never'. When the audio step (Dolby track, removing tracks) may rewrite "
                     "files. Each file is rewritten once (disk-heavy, light on CPU)."),
    "audio_languages": (env("LEANARR_AUDIO_LANGUAGES", ""), "text", "Audio languages to keep",
                        "Comma-separated ISO 639-2 codes, e.g. eng. Other-language audio tracks are removed from MKV "
                        "files. A file with none of these languages keeps its original language (the default track's). "
                        "Tracks without a language tag always stay. Empty = keep every language."),
    "drop_commentary": (env("LEANARR_DROP_COMMENTARY", "false"), "bool", "Remove commentary tracks",
                        "Remove audio and subtitle tracks marked or titled as commentary from MKV files. A file is never "
                        "left without audio."),
}
DOLBY_CODECS = ("ac3", "eac3", "truehd")
COMMENTARY = re.compile(r"(?i)comment")

VIDEO_EXT = re.compile(r"(?i)\.(mkv|mp4|m4v|avi|mov|wmv|mpg|mpeg|ts|m2ts|vob|flv|webm)$")
SKIP_PATH = re.compile(r"(?i)(^|/)(sample|samples|extras|featurettes|behind the scenes|trailers|other)(/|$)"
                       r"|[ ._-]sample[ ._-]|/sample\.[^/]+$")
EST_SAVING = {"h264": 0.45, "mpeg2video": 0.65, "vc1": 0.6, "mpeg4": 0.5, "msmpeg4v3": 0.55,
              "wmv3": 0.55, "mpeg1video": 0.6, "theora": 0.5}
H = 3600
# Seeding rules for "release finished seeds", one entry per tracker, read from seed_rules.json next to the
# database (see seed_rules.example.json). No file = no rules = Leanarr never releases a torrent.
#   {"MyTracker": {"hosts": ["tracker.example.org"], "ratio": 1.0, "single_hours": 48, "pack_hours": 144}}
def load_seed_rules():
    path = env("LEANARR_SEED_RULES", os.path.join(os.path.dirname(CONFIG["db"]) or ".", "seed_rules.json"))
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {name: {"hosts": tuple(r["hosts"]), "ratio": float(r["ratio"]),
                   "single": float(r["single_hours"]) * H, "pack": float(r.get("pack_hours", r["single_hours"])) * H}
            for name, r in raw.items()}


SEED_RULES = load_seed_rules()
CLAIM_LOCK = threading.Lock()


# ------------------------------------------------------------------ db + settings
def db():
    c = sqlite3.connect(CONFIG["db"], timeout=60)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(CONFIG["db"]) or ".", exist_ok=True)
    with db() as c:
        c.executescript("""
        pragma journal_mode=wal;
        create table if not exists files (
            path text primary key, size integer, mtime real, probed_at real,
            vcodec text, width integer, height integer, pix_fmt text, hdr text,
            duration real, audio_n integer, sub_n integer,
            status text, reason text, est_saving integer,
            new_path text, new_size integer, done_at real, error text);
        create index if not exists files_status on files(status);
        create table if not exists events (id integer primary key, ts real, msg text);
        create table if not exists settings (key text primary key, value text);
        create table if not exists workers (name text primary key, kind text, version text, addr text,
                                            last_seen real, note text);
        """)
        cols = {r[1] for r in c.execute("pragma table_info(files)")}
        for col, typ in (("worker", "text"), ("lease_until", "real"), ("layout_ok", "integer"),
                         ("acodecs", "text"), ("audio_status", "text"), ("audio_reason", "text"),
                         ("atracks", "text")):
            if col not in cols:
                c.execute(f"alter table files add column {col} {typ}")


def S(key):
    with db() as c:
        r = c.execute("select value from settings where key=?", (key,)).fetchone()
    return r["value"] if r else SETTINGS[key][0]


def S_bool(key):
    return str(S(key)).lower() in ("true", "1", "yes", "on")


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)
    try:
        with db() as c:
            c.execute("insert into events(ts, msg) values (?, ?)", (time.time(), msg))
    except Exception:
        pass


def gb(x):
    return f"{(x or 0) / 1e9:,.1f} GB"


# ------------------------------------------------------------------ probing + classification
def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300] or "ffprobe failed")
    return json.loads(out.stdout)


def main_video(info):
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    return vids[0] if vids else None


def hdr_kind(v):
    tags = (v.get("codec_tag_string") or "").lower()
    side = " ".join(str(sd.get("side_data_type", "")) for sd in v.get("side_data_list") or []).lower()
    if "dovi" in side or "dolby vision" in side or tags in ("dvh1", "dvhe", "dva1", "dvav"):
        return "Dolby Vision"
    if v.get("color_transfer") == "smpte2084":
        return "HDR10"
    if v.get("color_transfer") == "arib-std-b67":
        return "HLG"
    return ""


def decide(vcodec, hdr, duration, layout_ok, size):
    """Status from stored facts + current settings (so a settings change needs no re-probe)."""
    skip_codecs = {x.strip() for x in S("skip_codecs").split(",") if x.strip()}
    if not vcodec:
        return "skip", "no video stream", 0
    if vcodec in skip_codecs:
        return "skip", f"already {vcodec}", 0
    if hdr and S_bool("skip_hdr"):
        return "skip", f"{hdr} (kept as is)", 0
    if not duration:
        return "skip", "unknown duration", 0
    if not layout_ok:
        return "skip", "unusual stream layout (cover art before the video)", 0
    return "candidate", f"{vcodec} -> hevc", int(size * EST_SAVING.get(vcodec, 0.4))


def probe_fields(info):
    v = main_video(info)
    streams = info.get("streams", [])
    first_video = next((s for s in streams if s.get("codec_type") == "video"), None)
    try:
        duration = float((info.get("format") or {}).get("duration") or 0) or None
    except ValueError:
        duration = None
    return {"vcodec": v.get("codec_name") if v else None, "width": v.get("width") if v else None,
            "height": v.get("height") if v else None, "pix_fmt": v.get("pix_fmt") if v else None,
            "hdr": hdr_kind(v) if v else "", "duration": duration,
            "audio_n": sum(1 for s in streams if s.get("codec_type") == "audio"),
            "sub_n": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
            "layout_ok": 1 if (v is not None and first_video is v) else 0,
            "acodecs": ",".join(s.get("codec_name") or "?" for s in streams if s.get("codec_type") == "audio"),
            "atracks": json.dumps(track_facts(streams))}


def track_facts(streams):
    """Audio + subtitle tracks in file order, with what the audio step needs (stored as JSON in 'atracks')."""
    out = []
    for s in streams:
        if s.get("codec_type") not in ("audio", "subtitle"):
            continue
        disp, tags = s.get("disposition") or {}, s.get("tags") or {}
        out.append({"t": s["codec_type"][0], "codec": s.get("codec_name") or "?",
                    "lang": (tags.get("language") or tags.get("LANGUAGE") or "und").lower(),
                    "ch": int(s.get("channels") or 0), "title": tags.get("title") or tags.get("TITLE") or "",
                    "comment": bool(disp.get("comment")), "default": bool(disp.get("default"))})
    return out


def is_commentary(t):
    return t["comment"] or bool(COMMENTARY.search(t["title"]))


def lang_in(lang, wanted):
    return lang in wanted or (len(lang) == 2 and any(w.startswith(lang) for w in wanted))


def audio_work_enabled():
    return S("dolby_mode") == "add" or S_bool("drop_commentary") or bool(S("audio_languages").strip())


def audio_plan(path, tracks, allow_dolby):
    """What the audio step would change in one file: (plan, reason). plan is None when nothing changes.
    plan = {keep_a: audio indexes kept (in order), keep_s: subtitle indexes kept, dolby_src: audio index or None}"""
    if not path.lower().endswith(".mkv"):
        return None, "not MKV"
    auds = [t for t in tracks if t["t"] == "a"]
    subs = [t for t in tracks if t["t"] == "s"]
    if not auds:
        return None, "no audio track"
    drop_comm = S_bool("drop_commentary")
    keep = [i for i, t in enumerate(auds) if not (drop_comm and is_commentary(t))] or list(range(len(auds)))
    wanted = {x.strip().lower() for x in S("audio_languages").split(",") if x.strip()}
    if wanted:
        if any(auds[i]["lang"] != "und" and lang_in(auds[i]["lang"], wanted) for i in keep):
            keep = [i for i in keep if auds[i]["lang"] == "und" or lang_in(auds[i]["lang"], wanted)]
        else:   # none of the wanted languages: keep the original language (the default track's), unless unknown
            orig = auds[next((i for i in keep if auds[i]["default"]), keep[0])]["lang"]
            if orig != "und":
                keep = [i for i in keep if auds[i]["lang"] in (orig, "und")]
    keep_s = [j for j, t in enumerate(subs) if not (drop_comm and is_commentary(t))]
    dolby_src = None
    if allow_dolby:
        dts = [i for i in keep if auds[i]["codec"] == "dts"]
        if dts:
            src = next((i for i in dts if auds[i]["default"]), max(dts, key=lambda i: auds[i]["ch"]))
            need = min(auds[src]["ch"] or 6, 6)
            if not any(auds[i]["codec"] in DOLBY_CODECS and auds[i]["ch"] >= need for i in keep):
                dolby_src = src
    parts = []
    drop_a = [auds[i] for i in range(len(auds)) if i not in keep]
    if drop_a:
        n_c = sum(1 for t in drop_a if is_commentary(t))
        parts.append(f"remove {len(drop_a)} audio ({n_c} commentary, {len(drop_a) - n_c} other language)")
    if len(keep_s) < len(subs):
        parts.append(f"remove {len(subs) - len(keep_s)} commentary subtitle(s)")
    if dolby_src is not None:
        parts.append("add Dolby track from DTS")
    if not parts:
        return None, "nothing to change"
    return {"keep_a": keep, "keep_s": keep_s, "dolby_src": dolby_src}, "; ".join(parts)


def audio_decide(path, atracks):
    """(audio_status, reason) for the audio step, from stored facts."""
    if not atracks:
        return "skip", "audio tracks not read yet"
    plan, reason = audio_plan(path, json.loads(atracks), S("dolby_mode") != "off")
    return ("candidate" if plan else "skip"), reason


def audio_reclassify(c, rows):
    """Re-decide the audio step for stored rows (settings changed, or a wait/protected file is free again)."""
    for r in rows:
        st, reason = audio_decide(r["path"], r["atracks"])
        if st == "skip" and r["audio_status"] == "done":
            continue   # nothing more to do; keep the 'done' record
        c.execute("update files set audio_status=?, audio_reason=? where path=?", (st, reason, r["path"]))


def reclassify_all():
    """Apply changed settings to everything already probed (no re-probe needed)."""
    with db() as c:
        rows = c.execute("select path, size, vcodec, hdr, duration, layout_ok, reason from files "
                         "where status in ('candidate','skip') and vcodec is not null").fetchall()
        for r in rows:
            if r["reason"] and r["reason"].startswith(("smaller than", "sample/extras", "probe failed")):
                continue
            st, reason, est = decide(r["vcodec"], r["hdr"], r["duration"], r["layout_ok"] if r["layout_ok"] is not None else 1, r["size"])
            c.execute("update files set status=?, reason=?, est_saving=? where path=?", (st, reason, est, r["path"]))
        audio_reclassify(c, c.execute("select path, atracks, audio_status from files where atracks is not null "
                                      "and audio_status in ('candidate','skip','done')").fetchall())


# ------------------------------------------------------------------ qBittorrent
class Qbit:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _req(self, path, data=None, status=False):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(CONFIG["qbit_url"] + path, data=body, headers={"Referer": CONFIG["qbit_url"]})
        with self.op.open(r, timeout=60) as resp:
            text = resp.read().decode("utf-8", "replace")
            return (resp.status, text) if status else text

    def login(self):
        if not self.logged_in and CONFIG["qbit_password"]:
            # qBittorrent <5: 200 "Ok."; 5.x: 204 with an empty body. "Fails." = wrong login.
            code, text = self._req("/api/v2/auth/login",
                                   {"username": CONFIG["qbit_user"], "password": CONFIG["qbit_password"]}, status=True)
            if code not in (200, 204) or text.strip() == "Fails.":
                raise RuntimeError(f"qBittorrent login failed (HTTP {code})")
        self.logged_in = True

    def get(self, path):
        self.login()
        return json.loads(self._req(path))

    def post(self, path, data):
        self.login()
        return self._req(path, data)


def torrent_files():
    """Every file path any torrent uses. Raises on any problem (callers fail closed).
    Returns an empty set when the qBittorrent check is switched off in the settings."""
    if not S_bool("qbit_protection"):
        return set()
    qb = Qbit()
    paths = set()
    for t in qb.get("/api/v2/torrents/info"):
        save = t.get("save_path")
        if not save:
            raise RuntimeError(f"torrent without save path: {t.get('name')}")
        files = qb.get("/api/v2/torrents/files?hash=" + t["hash"])
        if not files:
            raise RuntimeError(f"torrent without file list: {t.get('name')}")
        for f in files:
            paths.add(os.path.normpath(os.path.join(save, f["name"])))
    return paths


def release_finished_seeds():
    qb = Qbit()
    for t in qb.get("/api/v2/torrents/info"):
        if t.get("state") not in ("stoppedUP", "pausedUP") or t.get("progress", 0) < 1:
            continue
        hosts = [urllib.parse.urlparse(x["url"]).hostname or "" for x in qb.get("/api/v2/torrents/trackers?hash=" + t["hash"])
                 if not x["url"].startswith("**")]
        name, rule = next(((n, r) for n, r in SEED_RULES.items() if any(h.endswith(rh) for h in hosts for rh in r["hosts"])), (None, None))
        if not rule:
            continue
        videos = [f for f in qb.get("/api/v2/torrents/files?hash=" + t["hash"]) if VIDEO_EXT.search(f["name"])]
        need = rule["pack"] if len(videos) > 1 else rule["single"]
        if t.get("ratio", 0) >= rule["ratio"] or t.get("seeding_time", 0) >= need:
            if S_bool("release_seeds"):
                qb.post("/api/v2/torrents/delete", {"hashes": t["hash"], "deleteFiles": "false"})
                log(f"RELEASED (seeding rule met, files kept): [{name}] {t['name']}")
            else:
                log(f"would release (seeding rule met): [{name}] {t['name']}")


# ------------------------------------------------------------------ scan + report
def scan():
    start, seen, probed = time.time(), 0, 0
    try:
        protected, qbit_ok = torrent_files(), True
    except Exception as e:
        protected, qbit_ok = set(), False
        log(f"qBittorrent not reachable ({e}): every file counts as protected this scan (fail-closed)")
    with db() as c:
        c.execute("update files set status='candidate', worker=null, lease_until=null "
                  "where status='working' and lease_until < ?", (time.time(),))
        audio_reclassify(c, c.execute("select path, atracks, audio_status from files where audio_status='wait'").fetchall())
        known = {r["path"]: (r["size"], r["mtime"], r["status"], r["atracks"], r["audio_status"])
                 for r in c.execute("select path,size,mtime,status,atracks,audio_status from files")}
    min_bytes = int(S("min_size_mb")) * 1024 * 1024
    on_disk = set()
    for lib in CONFIG["libraries"]:
        for root, dirs, names in os.walk(lib):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                if not VIDEO_EXT.search(n) or n.startswith(".leanarr-"):
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                on_disk.add(p)
                seen += 1
                prev = known.get(p)
                same = prev and prev[0] == st.st_size and prev[1] == st.st_mtime
                if same and prev[2] in ("done", "failed", "too-small-gain", "working") and prev[3] is not None:
                    continue
                cols = None
                if SKIP_PATH.search("/" + os.path.relpath(p, lib)):
                    cols = {"status": "skip", "reason": "sample/extras", "audio_status": "skip", "audio_reason": "sample/extras"}
                elif st.st_size < min_bytes and not same:
                    cols = {"status": "skip", "reason": f"smaller than {S('min_size_mb')} MB"}
                    try:   # small files still count for the audio step (e.g. TV episodes)
                        f = probe_fields(ffprobe(p))
                        probed += 1
                        a_st, a_reason = audio_decide(p, f["atracks"])
                        cols.update(acodecs=f["acodecs"], atracks=f["atracks"], duration=f["duration"],
                                    audio_n=f["audio_n"], sub_n=f["sub_n"], audio_status=a_st, audio_reason=a_reason)
                    except Exception as e:
                        cols.update(audio_status="skip", audio_reason=f"probe failed: {e}")
                elif not same:
                    try:
                        f = probe_fields(ffprobe(p))
                        probed += 1
                        status, reason, est = decide(f["vcodec"], f["hdr"], f["duration"], f["layout_ok"], st.st_size)
                        a_st, a_reason = audio_decide(p, f["atracks"])
                        cols = {**f, "status": status, "reason": reason, "est_saving": est,
                                "audio_status": a_st, "audio_reason": a_reason}
                    except Exception as e:
                        cols = {"status": "skip", "reason": f"probe failed: {e}"}
                elif prev[3] is None:
                    # known file from before the audio-track facts existed: read its tracks once
                    try:
                        f = probe_fields(ffprobe(p))
                        probed += 1
                        a_st, a_reason = audio_decide(p, f["atracks"])
                        cols = {"acodecs": f["acodecs"], "atracks": f["atracks"], "audio_n": f["audio_n"],
                                "sub_n": f["sub_n"], "duration": f["duration"]}
                        if not (a_st == "skip" and prev[4] == "done"):   # keep an earlier 'done' record
                            cols.update(audio_status=a_st, audio_reason=a_reason)
                    except Exception as e:
                        cols = {"acodecs": "", "atracks": "[]", "audio_status": "skip", "audio_reason": f"probe failed: {e}"}
                with db() as c:
                    if cols is not None:
                        cols.update(size=st.st_size, mtime=st.st_mtime, probed_at=time.time())
                        c.execute(f"insert into files(path,{','.join(cols)}) values (?{',?' * len(cols)}) "
                                  f"on conflict(path) do update set {','.join(k + '=excluded.' + k for k in cols)}",
                                  (p, *cols.values()))
                    row = c.execute("select status from files where path=?", (p,)).fetchone()
                    if row and row["status"] in ("candidate", "protected"):
                        if not qbit_ok or os.path.normpath(p) in protected:
                            c.execute("update files set status='protected', reason=? where path=?",
                                      ("used by a torrent" if qbit_ok else "qBittorrent unreachable", p))
                        elif row["status"] == "protected":
                            c.execute("update files set status='candidate', reason=vcodec||' -> hevc' where path=?", (p,))
                    arow = c.execute("select path, atracks, audio_status from files where path=?", (p,)).fetchone()
                    if arow and arow["audio_status"] in ("candidate", "protected"):
                        if not qbit_ok or os.path.normpath(p) in protected:
                            c.execute("update files set audio_status='protected', audio_reason=? where path=?",
                                      ("used by a torrent" if qbit_ok else "qBittorrent unreachable", p))
                        elif arow["audio_status"] == "protected":
                            audio_reclassify(c, [arow])
    with db() as c:
        for p in set(known) - on_disk:
            c.execute("delete from files where path=? and status!='done'", (p,))
    log(f"scan: {seen} video files, {probed} newly probed, {time.time() - start:.0f} s")


def report_data():
    with db() as c:
        by_status = [dict(r) for r in c.execute(
            "select status, count(*) n, sum(size) bytes, sum(est_saving) est, "
            "sum(case when new_size then size-new_size end) saved from files group by status order by n desc")]
        reasons = [dict(r) for r in c.execute(
            "select status, reason, count(*) n, sum(size) bytes from files group by status, reason order by n desc limit 40")]
        per_lib = []
        for lib in CONFIG["libraries"]:
            r = c.execute("select count(*) n, sum(size) bytes, sum(est_saving) est from files "
                          "where status='candidate' and path like ?", (lib.rstrip('/') + '/%',)).fetchone()
            per_lib.append({"library": lib, **dict(r)})
        top = [dict(r) for r in c.execute(
            "select path, size, vcodec, height, est_saving from files where status='candidate' order by est_saving desc limit 50")]
        working = [dict(r) for r in c.execute("select path, worker, lease_until from files where status='working'")]
        done = [dict(r) for r in c.execute(
            "select new_path, size, new_size, done_at, worker from files where status='done' order by done_at desc limit 15")]
        events = [dict(r) for r in c.execute("select ts, msg from events order by id desc limit 40")]
        workers = [dict(r) for r in c.execute("select * from workers order by last_seen desc")]
        dolby = [dict(r) for r in c.execute(
            "select audio_status status, count(*) n, sum(size) bytes from files where audio_status is not null "
            "and audio_status!='skip' group by audio_status order by n desc")]
        dolby_recent = [dict(r) for r in c.execute(
            "select path, audio_status, audio_reason from files where audio_status in ('done','failed','working') "
            "order by case audio_status when 'working' then 0 else 1 end, mtime desc limit 15")]
    return {"by_status": by_status, "reasons": reasons, "per_lib": per_lib, "top": top,
            "working": working, "done": done, "events": events, "workers": workers,
            "dolby": dolby, "dolby_recent": dolby_recent}


def print_report():
    d = report_data()
    print(f"\nLeanarr report (mode: {S('mode')})")
    for s in d["by_status"]:
        extra = f", est. saving {gb(s['est'])}" if s["status"] == "candidate" else (f", saved {gb(s['saved'])}" if s["saved"] else "")
        print(f"  {s['status']:<14} {s['n']:6} files  {gb(s['bytes'])}{extra}")
    for l in d["per_lib"]:
        print(f"  {l['library']:<16} {l['n'] or 0:6} candidates  {gb(l['bytes'])}  est. saving {gb(l['est'])}")


# ------------------------------------------------------------------ encoding helpers (shared idea with leanarr_worker.py)
def in_window(spec):
    w = (spec or "").strip().lower()
    if w in ("always", "", "*"):
        return True
    if w in ("never", "none", "off"):
        return False
    a, b = w.split("-")
    now = dt.datetime.now().time()
    ta, tb = dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())
    return ta <= now < tb if ta < tb else (now >= ta or now < tb)


def stream_args(info, ten_bit):
    """Map everything except data streams, copy audio/subs/attachments, MP4 text subs -> SRT."""
    args = ["-map", "0", "-map", "-0:d", "-c", "copy"]
    subs = [s for s in info["streams"] if s.get("codec_type") == "subtitle"]
    for i, s in enumerate(subs):
        if s.get("codec_name") == "mov_text":
            args += [f"-c:s:{i}", "srt"]
    return args


def x265_cmd(src, dst, row, info):
    ten = "10" in (row["pix_fmt"] or "")
    return (["nice", "-n", CONFIG["nice"], "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", src]
            + stream_args(info, ten)
            + ["-c:v:0", "libx265", "-preset", S("x265_preset"), "-crf", str(S("x265_crf")),
               "-x265-params", "log-level=error", "-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
            + shlex.split(S("x265_extra") or "") + ["-max_muxing_queue_size", "4096", dst])


def verify(row, out_path):
    info = ffprobe(out_path)
    v = main_video(info)
    if not v or v.get("codec_name") != "hevc":
        return "output has no HEVC video"
    dur = float((info.get("format") or {}).get("duration") or 0)
    if abs(dur - (row["duration"] or 0)) > max(1.0, 0.002 * (row["duration"] or 0)):
        return f"length differs ({dur:.1f}s vs {row['duration']:.1f}s)"
    a = sum(1 for s in info["streams"] if s.get("codec_type") == "audio")
    s = sum(1 for s in info["streams"] if s.get("codec_type") == "subtitle")
    if a != row["audio_n"] or s != row["sub_n"]:
        return f"tracks differ (audio {a}/{row['audio_n']}, subtitles {s}/{row['sub_n']})"
    new = os.path.getsize(out_path)
    if new > row["size"] * (1 - float(S("min_saving"))):
        return f"too-small-gain: {new / row['size']:.0%} of the original"
    return None


def arr_rescan(path):
    for kind, url, key, listp, cmd, idk in (
            ("sonarr", CONFIG["sonarr_url"], CONFIG["sonarr_key"], "/api/v3/series", "RescanSeries", "seriesId"),
            ("radarr", CONFIG["radarr_url"], CONFIG["radarr_key"], "/api/v3/movie", "RescanMovie", "movieId")):
        if not url or not key:
            continue
        try:
            items = json.load(urllib.request.urlopen(urllib.request.Request(url + listp, headers={"X-Api-Key": key}), timeout=60))
            for it in items:
                if path.startswith((it.get("path") or "").rstrip("/") + "/"):
                    body = json.dumps({"name": cmd, idk: it["id"]}).encode()
                    urllib.request.urlopen(urllib.request.Request(url + "/api/v3/command", data=body,
                                           headers={"X-Api-Key": key, "Content-Type": "application/json"}), timeout=60)
                    log(f"  {kind}: {cmd} {it.get('title')}")
                    return
        except Exception as e:
            log(f"  {kind} rescan failed: {e}")


# ------------------------------------------------------------------ job steps (used by local + remote workers)
def precheck(row):
    """None if the file may be encoded now, else the reason not to."""
    try:
        st = os.stat(row["path"])
    except OSError:
        return "skip: file gone"
    if st.st_size != row["size"] or st.st_mtime != row["mtime"]:
        return "skip: changed since scan"
    if time.time() - st.st_mtime < CONFIG["settle_minutes"] * 60:
        return "skip: too new (settling)"
    try:
        if os.path.normpath(row["path"]) in torrent_files():
            return "protected: used by a torrent"
    except Exception as e:
        return f"protected: qBittorrent check failed ({e})"
    return None


def finalize(row, tmp, t0, who):
    """Verify the encoded file, check protection again, replace the original safely."""
    src = row["path"]
    try:
        problem = verify(row, tmp)
        if problem:
            return problem if problem.startswith("too-small-gain") else f"failed: verify: {problem}"
        try:
            if os.path.normpath(src) in torrent_files():
                return "protected: became used by a torrent during the encode"
        except Exception as e:
            return f"protected: qBittorrent check failed before replace ({e})"
        if os.stat(src).st_mtime != row["mtime"]:
            return "skip: original changed during the encode"
        final = os.path.splitext(src)[0] + ".mkv"
        if final != src and os.path.exists(final):
            return f"failed: {os.path.basename(final)} already exists"
        part = os.path.join(os.path.dirname(src), ".leanarr-" + os.path.basename(final) + ".part")
        # the file gets rewritten anyway: do the audio step (Dolby track, unwanted tracks) on the way into the library
        audio_note, plan = "", None
        if audio_work_enabled():
            try:
                tinfo = ffprobe(tmp)
                plan, reason, auds = current_plan(final, tinfo)
                if plan:
                    p = subprocess.run(tidy_cmd(tmp, part, plan, auds), capture_output=True, text=True)
                    problem = f"ffmpeg: {p.stderr.strip()[-200:]}" if p.returncode != 0 else tidy_verify(tinfo, part, plan, auds)
                    if problem:
                        log(f"  audio step skipped during shrink ({problem}); the audio worker retries later")
                        plan = None
                    else:
                        audio_note = f" + audio: {reason}"
            except Exception as e:
                log(f"  audio step skipped during shrink ({e}); the audio worker retries later")
                plan = None
        if not plan:
            shutil.copyfile(tmp, part)
            if os.path.getsize(part) != os.path.getsize(tmp):
                os.remove(part)
                return "failed: copy to library incomplete"
        os.replace(part, final)
        if final != src:
            os.remove(src)
        new_size = os.path.getsize(final)
        with db() as c:
            c.execute("update files set status='done', reason=?, new_path=?, new_size=?, done_at=?, worker=?, lease_until=null "
                      "where path=?", (f"{row['vcodec']} -> hevc, {new_size / row['size']:.0%} of original",
                                       final, new_size, time.time(), who, src))
        log(f"DONE [{who}] {os.path.basename(final)}: {gb(row['size'])} -> {gb(new_size)} "
            f"(saved {gb(row['size'] - new_size)}, {(time.time() - t0) / 60:.0f} min){audio_note}")
        arr_rescan(final)
        return "done"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def record_result(row, result, who):
    if result == "done":
        return
    status = ("protected" if result.startswith("protected") else "too-small-gain" if result.startswith("too-small-gain")
              else "skip" if result.startswith("skip") else "failed")
    with db() as c:
        c.execute("update files set status=?, reason=?, error=?, worker=?, lease_until=null where path=?",
                  (status, result[:300], result[:300] if status == "failed" else None, who, row["path"]))
    log(f"{status.upper()} [{who}]: {row['path']}: {result}")


def next_candidate():
    with db() as c:
        return c.execute("select rowid as id, * from files where status='candidate' and coalesce(audio_status,'')!='working' "
                         "order by est_saving desc limit 1").fetchone()


def local_worker(once=False):
    while True:
        ok = S("mode") == "shrink" and S_bool("local_worker") and (once or in_window(S("window")))
        if not ok:
            if once:
                return
            time.sleep(60)
            continue
        with CLAIM_LOCK:
            row = next_candidate()
            if row:
                with db() as c:
                    c.execute("update files set status='working', worker='local', lease_until=? where path=?",
                              (time.time() + 48 * H, row["path"]))
        if not row:
            if once:
                return
            time.sleep(600)
            continue
        try:
            why = precheck(row)
            if why:
                result = why
            else:
                os.makedirs(CONFIG["work_dir"], exist_ok=True)
                tmp = os.path.join(CONFIG["work_dir"], f"local-{row['id']}.mkv")
                log(f"ENCODE [local x265 crf {S('x265_crf')} {S('x265_preset')}] {row['path']} ({gb(row['size'])})")
                t0 = time.time()
                p = subprocess.run(x265_cmd(row["path"], tmp, row, ffprobe(row["path"])), capture_output=True, text=True)
                result = (f"failed: ffmpeg: {p.stderr.strip()[-300:]}" if p.returncode != 0 else finalize(row, tmp, t0, "local"))
                if os.path.exists(tmp):
                    os.remove(tmp)
        except Exception as e:
            result = f"failed: {e}"
        record_result(row, result, "local")


# ------------------------------------------------------------------ audio step (Dolby track, unwanted tracks)
def tidy_cmd(src, dst, plan, auds):
    """ffmpeg command for one audio-step plan: optional new Dolby track first (made from the chosen DTS track),
    then the kept audio tracks, video, kept subtitles, attachments and chapters, all copied unchanged."""
    codec = S("dolby_codec")
    cmd = ["nice", "-n", CONFIG["nice"], "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", src,
           "-map", "0:v?"]
    d = plan["dolby_src"]
    if d is not None:
        cmd += ["-map", f"0:a:{d}"]
    for i in plan["keep_a"]:
        cmd += ["-map", f"0:a:{i}"]
    for j in plan["keep_s"]:
        cmd += ["-map", f"0:s:{j}"]
    cmd += ["-map", "0:t?", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    n_out = len(plan["keep_a"]) + (d is not None)
    if d is not None:
        ch = min(auds[d]["ch"] or 6, 6)
        name = {"eac3": "Dolby Digital Plus", "ac3": "Dolby Digital"}[codec]
        layout = "5.1" if ch >= 6 else f"{ch}.0" if ch > 2 else "2.0"
        cmd += ["-c:a:0", codec, "-b:a:0", S("dolby_bitrate"), "-ac:a:0", str(ch), "-ar:a:0", "48000",
                "-metadata:s:a:0", f"title={name} {layout} (from DTS)", "-metadata:s:a:0", f"language={auds[d]['lang']}"]
        default_out = 0
    else:   # keep the original default track as default, else the first kept one
        default_out = next((k for k, i in enumerate(plan["keep_a"]) if auds[i]["default"]), 0)
    for k in range(n_out):
        cmd += [f"-disposition:a:{k}", "default" if k == default_out else "0"]
    return cmd + ["-max_muxing_queue_size", "4096", "-f", "matroska", dst]


def tidy_verify(src_info, out_path, plan, auds):
    """None if the rewritten file has exactly the planned tracks, the same length and a decodable Dolby track."""
    info = ffprobe(out_path)
    codec = S("dolby_codec")
    count = lambda i, k: sum(1 for s in i["streams"] if s.get("codec_type") == k)  # noqa: E731
    want = ([codec] if plan["dolby_src"] is not None else []) + [auds[i]["codec"] for i in plan["keep_a"]]
    got = [s.get("codec_name") for s in info["streams"] if s.get("codec_type") == "audio"]
    if got != want:
        return f"audio tracks wrong ({','.join(got)} vs {','.join(want)})"
    if count(info, "subtitle") != len(plan["keep_s"]):
        return f"subtitle tracks wrong ({count(info, 'subtitle')} vs {len(plan['keep_s'])})"
    for k in ("video", "attachment"):
        if count(info, k) != count(src_info, k):
            return f"{k} tracks differ ({count(info, k)} vs {count(src_info, k)})"
    d_src = float((src_info.get("format") or {}).get("duration") or 0)
    d_out = float((info.get("format") or {}).get("duration") or 0)
    if abs(d_out - d_src) > max(2.0, 0.002 * d_src):
        return f"length differs ({d_out:.1f}s vs {d_src:.1f}s)"
    if plan["dolby_src"] is not None:
        # decode 30 s of the new track from the middle; only errors of that decoder count
        # (the null muxer and the video decoder print harmless warnings for many Blu-ray files)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", str(int(d_out / 2)), "-t", "30",
                            "-i", out_path, "-map", "0:a:0", "-vn", "-sn", "-dn", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=600)
        bad = [x for x in p.stderr.splitlines() if x.startswith(f"[{codec} @") or "Error while decoding" in x]
        if p.returncode != 0 or bad:
            return f"decode test failed: {(bad or p.stderr.splitlines() or ['?'])[0][:200]}"
    return None


def current_plan(path, info):
    """The audio-step plan for a file as it is now on disk (Dolby only when the Dolby mode is 'add')."""
    tracks = track_facts(info["streams"])
    plan, reason = audio_plan(path, tracks, S("dolby_mode") == "add")
    return plan, reason, [t for t in tracks if t["t"] == "a"]


def tidy_file(row):
    """One file: write the new MKV next to the original, verify, re-check seeding, replace. Returns a result text."""
    src = row["path"]
    why = precheck(row)
    if why:
        return why
    info = ffprobe(src)
    plan, reason, auds = current_plan(src, info)
    if not plan:
        return f"skip: {reason}"
    part = os.path.join(os.path.dirname(src), ".leanarr-" + os.path.basename(src) + ".part")
    try:
        log(f"AUDIO [{reason}] {src} ({gb(row['size'])})")
        t0 = time.time()
        p = subprocess.run(tidy_cmd(src, part, plan, auds), capture_output=True, text=True)
        if p.returncode != 0:
            return f"failed: ffmpeg: {p.stderr.strip()[-300:]}"
        problem = tidy_verify(info, part, plan, auds)
        if problem:
            return f"failed: verify: {problem}"
        try:
            if os.path.normpath(src) in torrent_files():
                return "protected: became used by a torrent meanwhile"
        except Exception as e:
            return f"protected: qBittorrent check failed before replace ({e})"
        st = os.stat(src)
        if st.st_mtime != row["mtime"] or st.st_size != row["size"]:
            return "skip: original changed meanwhile"
        shutil.copymode(src, part)
        os.replace(part, src)
        new = os.stat(src)
        f = probe_fields(ffprobe(src))
        with db() as c:
            c.execute("update files set size=?, mtime=?, acodecs=?, atracks=?, audio_n=?, sub_n=?, audio_status='done', "
                      "audio_reason=? where path=?", (new.st_size, new.st_mtime, f["acodecs"], f["atracks"],
                                                      f["audio_n"], f["sub_n"], reason.replace("remove", "removed")
                                                      .replace("add Dolby", "added Dolby"), src))
        log(f"AUDIO DONE {os.path.basename(src)} ({(time.time() - t0) / 60:.0f} min)")
        arr_rescan(src)
        return "done"
    finally:
        if os.path.exists(part):
            os.remove(part)


def audio_worker():
    with db() as c:   # a job that was running when Leanarr stopped goes back into the queue
        c.execute("update files set audio_status='candidate' where audio_status='working'")
    while True:
        if not audio_work_enabled() or not in_window(S("dolby_window")):
            time.sleep(60)
            continue
        with CLAIM_LOCK:
            with db() as c:
                # longest first = movies before TV episodes; never a file a shrink job is working on
                row = c.execute("select rowid as id, * from files where audio_status='candidate' and status!='working' "
                                "order by duration desc limit 1").fetchone()
                if row:
                    c.execute("update files set audio_status='working' where path=?", (row["path"],))
        if not row:
            time.sleep(600)
            continue
        try:
            result = tidy_file(row)
        except Exception as e:
            result = f"failed: {e}"
        if result != "done":
            # 'wait' = try again after the next scan (still settling / changed); scan() puts it back in the queue
            status = ("protected" if result.startswith("protected")
                      else "wait" if result.startswith(("skip: too new", "skip: changed", "skip: original changed"))
                      else "skip" if result.startswith("skip") else "failed")
            with db() as c:
                c.execute("update files set audio_status=?, audio_reason=? where path=?", (status, result[:300], row["path"]))
            log(f"AUDIO {status.upper()}: {row['path']}: {result}")


# ------------------------------------------------------------------ remote worker API
def worker_seen(name, kind, version, addr, note):
    with db() as c:
        c.execute("insert into workers(name, kind, version, addr, last_seen, note) values (?,?,?,?,?,?) "
                  "on conflict(name) do update set kind=excluded.kind, version=excluded.version, addr=excluded.addr, "
                  "last_seen=excluded.last_seen, note=excluded.note", (name, kind, version, addr, time.time(), note))


def claim(worker_name, kind):
    """Give a remote worker the next job (after the pre-checks), or None."""
    # a worker only asks when it has nothing running -> anything still 'working' for it was lost
    # (outage, reboot, crash): put it back in the queue instead of waiting for the lease to run out
    with db() as c:
        lost = [r["path"] for r in c.execute("select path from files where status='working' and worker=?", (worker_name,))]
        if lost:
            c.execute("update files set status='candidate', worker=null, lease_until=null where status='working' and worker=?",
                      (worker_name,))
    for p in lost:
        log(f"REQUEUED (lost job, {worker_name} asked for new work): {p}")
    if S("mode") != "shrink":
        return None, "report mode"
    if not in_window(S("remote_window")):
        return None, "outside the remote worker hours"
    with CLAIM_LOCK:
        for _ in range(25):
            row = next_candidate()
            if not row:
                return None, "no candidates"
            why = precheck(row)
            if why:
                record_result(row, why, worker_name)
                continue
            with db() as c:
                c.execute("update files set status='working', worker=?, lease_until=? where path=?",
                          (worker_name, time.time() + int(S("lease_hours")) * H, row["path"]))
            info = ffprobe(row["path"])
            subs = [s.get("codec_name") for s in info["streams"] if s.get("codec_type") == "subtitle"]
            job = {"id": row["id"], "name": os.path.basename(row["path"]), "size": row["size"],
                   "duration": row["duration"], "pix_fmt": row["pix_fmt"], "subtitle_codecs": subs, "kind": kind,
                   "nvenc": {"cq": S("nvenc_cq"), "preset": S("nvenc_preset"), "extra": S("nvenc_extra")},
                   "x265": {"crf": S("x265_crf"), "preset": S("x265_preset"), "extra": S("x265_extra")}}
            log(f"CLAIMED by {worker_name} ({kind}): {row['path']} ({gb(row['size'])})")
            return job, None
    return None, "nothing eligible right now"


def job_row(job_id):
    with db() as c:
        return c.execute("select rowid as id, * from files where rowid=? and status='working'", (job_id,)).fetchone()


# ------------------------------------------------------------------ web UI + API
CSS = """:root{--bg:#f6f7f9;--fg:#1d2433;--card:#fff;--mut:#667085;--acc:#0e7c66;--line:#e4e7ec;--warn:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e9ef;--card:#1b2029;--mut:#98a2b3;--acc:#3fc9a7;--line:#2a3140;--warn:#f97066}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:16px}
a{color:var(--acc)}h1{margin:.2em 0}h1 small{color:var(--mut);font-size:.5em}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.big{font-size:1.6em;font-weight:700;color:var(--acc)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;margin:8px 0 20px}
td,th{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600}
.mut{color:var(--mut)}.wrap{word-break:break-all}.scroll{overflow-x:auto}.warn{background:var(--warn);color:#fff;padding:10px;border-radius:8px;margin:10px 0}
form .row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
input[type=text],select{width:100%;max-width:520px;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;cursor:pointer}"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}</style></head><body><main>{body}"
            f"<p class='mut'>Leanarr {VERSION} by H0m3Lab · <a href='/'>status</a> · <a href='/settings'>settings</a> · "
            f"<a href='/worker-setup'>add a GPU worker</a> · <a href='/report.json'>report.json</a></p></main></body></html>")


def status_page():
    d = report_data()
    st = {s["status"]: s for s in d["by_status"]}
    cand, done = st.get("candidate", {}), st.get("done", {})
    warn = ("<div class='warn'>qBittorrent protection is OFF: files that are still seeding can be replaced "
            "(that breaks those torrents).</div>" if not S_bool("qbit_protection") else "")
    cards = "".join(f"<div class='card'><div class='mut'>{k}</div><div class='big'>{v}</div></div>" for k, v in [
        ("Candidates", f"{cand.get('n', 0):,}"), ("Estimated saving", gb(cand.get("est"))),
        ("Shrunk", f"{done.get('n', 0):,}"), ("Space saved", gb(done.get("saved"))),
        ("Protected (seeding)", f"{st.get('protected', {}).get('n', 0):,}"), ("Working", f"{st.get('working', {}).get('n', 0):,}")])
    esc = html.escape
    libs = "".join(f"<tr><td>{esc(l['library'])}</td><td>{l['n'] or 0:,}</td><td>{gb(l['bytes'])}</td><td>{gb(l['est'])}</td></tr>" for l in d["per_lib"])
    working = "".join(f"<tr><td class='wrap'>{esc(w['path'])}</td><td>{esc(w['worker'] or '')}</td></tr>" for w in d["working"]) or "<tr><td class='mut'>nothing right now</td></tr>"
    doneh = "".join(f"<tr><td class='wrap'>{esc(x['new_path'] or '')}</td><td>{gb(x['size'])} → {gb(x['new_size'])}</td><td>{esc(x['worker'] or '')}</td></tr>" for x in d["done"]) or "<tr><td class='mut'>none yet</td></tr>"
    reasons = "".join(f"<tr><td>{r['status']}</td><td>{esc(r['reason'] or '')}</td><td>{r['n']:,}</td><td>{gb(r['bytes'])}</td></tr>" for r in d["reasons"] if r["status"] != "candidate")
    top = "".join(f"<tr><td class='wrap'>{esc(t['path'])}</td><td>{t['vcodec']} {t['height'] or ''}p</td><td>{gb(t['size'])}</td><td>{gb(t['est_saving'])}</td></tr>" for t in d["top"][:25])
    events = "".join(f"<tr><td class='mut'>{dt.datetime.fromtimestamp(e['ts']):%m-%d %H:%M}</td><td class='wrap'>{esc(e['msg'])}</td></tr>" for e in d["events"])
    def ago(ts):
        s = int(time.time() - (ts or 0))
        return f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago"
    wk = "".join(f"<tr><td><b>{esc(w['name'])}</b></td><td>{esc(w['kind'] or '')}</td><td>{esc(w['addr'] or '')}</td>"
                 f"<td>{'🟢 ' if time.time() - (w['last_seen'] or 0) < 900 else '⚪ '}{ago(w['last_seen'])}</td>"
                 f"<td class='mut'>{esc(w['note'] or '')}</td></tr>" for w in d["workers"]) \
        or "<tr><td class='mut'>no remote worker has checked in yet</td></tr>"
    ds = {x["status"]: x["n"] for x in d["dolby"]}
    dolby = ""
    if S("dolby_mode") != "off" or audio_work_enabled():
        dcards = "".join(f"<div class='card'><div class='mut'>{k}</div><div class='big'>{v:,}</div></div>" for k, v in [
            ("To do", ds.get("candidate", 0) + ds.get("wait", 0) + ds.get("working", 0)),
            ("Done", ds.get("done", 0)), ("Waiting (seeding)", ds.get("protected", 0)),
            ("Failed", ds.get("failed", 0))])
        drows = "".join(f"<tr><td>{esc(x['audio_status'])}</td><td class='wrap'>{esc(x['path'])}</td>"
                        f"<td class='mut'>{esc(x['audio_reason'] or '')}</td></tr>" for x in d["dolby_recent"]) \
            or "<tr><td class='mut'>nothing yet</td></tr>"
        dolby = (f"<h2>Audio tracks <small class='mut'>(Dolby {S('dolby_mode')} {S('dolby_codec')} {S('dolby_bitrate')}, "
                 f"languages {esc(S('audio_languages') or 'all')}, commentary "
                 f"{'removed' if S_bool('drop_commentary') else 'kept'}, hours {esc(S('dolby_window'))})</small></h2>"
                 f"<div class='cards'>{dcards}</div>"
                 f"<div class='scroll'><table>{drows}</table></div>")
    body = (f"<h1>Leanarr <small>mode <b>{S('mode')}</b> · local CPU {'on' if S_bool('local_worker') else 'off'} "
            f"({S('window')}) · remote workers {'on' if CONFIG['worker_token'] else 'off'} ({S('remote_window')})</small></h1>{warn}"
            f"<div class='cards'>{cards}</div>{dolby}"
            f"<h2>Workers</h2><div class='scroll'><table>{wk}</table></div>"
            f"<h2>Working on</h2><div class='scroll'><table>{working}</table></div>"
            f"<h2>Recently shrunk</h2><div class='scroll'><table>{doneh}</table></div>"
            f"<h2>Candidates per library</h2><div class='scroll'><table><tr><th>Library</th><th>Files</th><th>Size</th><th>Est. saving</th></tr>{libs}</table></div>"
            f"<h2>Why files are skipped</h2><div class='scroll'><table><tr><th>Status</th><th>Reason</th><th>Files</th><th>Size</th></tr>{reasons}</table></div>"
            f"<h2>Biggest candidates</h2><div class='scroll'><table><tr><th>File</th><th>Codec</th><th>Size</th><th>Est. saving</th></tr>{top}</table></div>"
            f"<h2>Recent activity</h2><div class='scroll'><table>{events}</table></div>")
    return page("Leanarr", body)


def settings_page(msg=""):
    rows = []
    for key, (default, kind, label, help_) in SETTINGS.items():
        val = S(key)
        if isinstance(kind, list):
            ctl = f"<select name='{key}'>" + "".join(f"<option{' selected' if o == val else ''}>{o}</option>" for o in kind) + "</select>"
        elif kind == "bool":
            ctl = (f"<input type='hidden' name='{key}' value='false'><label><input type='checkbox' name='{key}' value='true'"
                   f"{' checked' if str(val).lower() == 'true' else ''}> on</label>")
        else:
            ctl = f"<input type='text' name='{key}' value='{html.escape(str(val))}'>"
        warn = " style='border-color:var(--warn)'" if key == "qbit_protection" else ""
        rows.append(f"<div class='row'{warn}><b>{html.escape(label)}</b><br>{ctl}<div class='mut'>{html.escape(help_)}</div></div>")
    note = f"<div class='card'>{html.escape(msg)}</div>" if msg else ""
    worker = "<p>Add a PC with an NVIDIA card as a helper: <a href='/worker-setup'>step-by-step instructions</a>.</p>"
    return page("Leanarr settings", f"<h1>Settings</h1>{note}<form method='post' action='/settings'>{''.join(rows)}"
                                     f"<p><button type='submit'>Save</button></p></form>{worker}")


def worker_setup_page(host):
    """Step-by-step page to open on the GPU PC: one command to copy into an admin terminal."""
    server = f"http://{host}"
    if not CONFIG["worker_token"]:
        return page("Add a GPU worker", "<h1>Add a GPU worker</h1><div class='warn'>Remote workers are off: "
                    "set LEANARR_WORKER_TOKEN on the server first.</div>")
    cmd = (f"$s = Invoke-RestMethod {server}/download/Setup-LeanarrWorker.ps1; "
           f"& ([scriptblock]::Create($s)) -Server '{server}' -Token '{CONFIG['worker_token']}'")
    esc = html.escape
    body = f"""<h1>Add a GPU worker</h1>
<p>Do this <b>on the PC with the NVIDIA graphics card</b>. It takes about 5 minutes and needs the internet once
(it downloads Python and ffmpeg). Afterwards the PC helps shrink videos in the background; nothing else changes on it.</p>
<div class='row card'><b>1. Open an administrator terminal</b><br>
Right-click the <b>Start</b> button → <b>Terminal (Admin)</b> (on older Windows 10: <b>Windows PowerShell (Admin)</b>) →
click <b>Yes</b> when Windows asks.</div>
<div class='row card'><b>2. Copy this command</b><br>
<textarea id='cmd' readonly rows='4' style='width:100%;font:13px monospace;margin:6px 0'>{esc(cmd)}</textarea>
<button onclick="navigator.clipboard.writeText(document.getElementById('cmd').value).then(()=>this.textContent='Copied!')">Copy</button>
<span class='mut'>(or select all the text and press Ctrl+C)</span></div>
<div class='row card'><b>3. Paste it into the terminal and press Enter</b><br>
Right-click in the terminal window (or Ctrl+V), then <b>Enter</b>. Wait until it says
<b style='color:var(--acc)'>Leanarr Worker installed and started.</b> Then close the window.</div>
<div class='row card'><b>4. Check it here</b><br>The worker shows up on the <a href='/'>status page</a> once it gets its
first job (only in <b>shrink</b> mode and inside the "Remote worker hours" on the <a href='/settings'>settings page</a>).
Its log on that PC: <code>C:\\LeanarrWorker\\worker.log</code>.</div>
<details class='row card'><summary><b>What the command does / how to remove it</b></summary>
<p>It downloads <code>Setup-LeanarrWorker.ps1</code> from this server and runs it: checks the NVIDIA driver, installs
Python 3.12 (winget), downloads ffmpeg (gyan.dev, checksum verified), does a 2-second GPU test, and creates the
background task <b>Leanarr Worker</b> (starts with Windows, no login needed) in <code>C:\\LeanarrWorker</code>.
The PC only downloads one video at a time from this server, encodes it on the GPU and sends it back. It never
touches the media library.</p>
<p>Remove (admin terminal): <code>Unregister-ScheduledTask -TaskName "Leanarr Worker" -Confirm:$false;
Remove-Item C:\\LeanarrWorker -Recurse -Force</code></p></details>
<p class='mut'>The command contains this server's worker key; it's only shown on your home network.</p>"""
    return page("Add a GPU worker", body)


def save_settings(form):
    errors = []
    with db() as c:
        for key, (default, kind, label, _) in SETTINGS.items():
            vals = form.get(key)
            if vals is None:
                continue
            v = vals[-1].strip()  # checkbox: hidden 'false' + checked 'true' -> last wins
            if isinstance(kind, list) and v not in kind:
                errors.append(f"{label}: must be one of {', '.join(kind)}"); continue
            if kind.__class__ is str and kind.startswith(("int:", "float:")):
                typ, lo, hi = kind.split(":")
                try:
                    num = (int if typ == "int" else float)(v)
                    if not float(lo) <= num <= float(hi):
                        raise ValueError
                except ValueError:
                    errors.append(f"{label}: must be a number between {lo} and {hi}"); continue
            if key in ("window", "remote_window", "dolby_window") and v.lower() not in ("always", "never", "none", "off")                     and not re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", v):
                errors.append(f"{label}: use HH:MM-HH:MM, 'always' or 'never'"); continue
            if key == "audio_languages":
                v = ",".join(x.strip().lower() for x in v.split(",") if x.strip())
                if v and not re.fullmatch(r"[a-z]{2,3}(,[a-z]{2,3})*", v):
                    errors.append(f"{label}: use language codes like eng or eng,jpn"); continue
            if key.endswith("_extra"):
                try:
                    shlex.split(v)
                except ValueError:
                    errors.append(f"{label}: unbalanced quotes"); continue
            c.execute("insert into settings(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, v))
    reclassify_all()
    log("settings saved" + (f" (with errors: {'; '.join(errors)})" if errors else ""))
    return "Saved." if not errors else "Saved, except: " + "; ".join(errors)


class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        tok = CONFIG["worker_token"]
        if not tok or self.headers.get("X-Leanarr-Token") != tok:
            self.send(403, "worker token missing or wrong")
            return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send(200, status_page(), "text/html; charset=utf-8")
        if self.path == "/settings":
            return self.send(200, settings_page(), "text/html; charset=utf-8")
        if self.path == "/worker-setup":
            return self.send(200, worker_setup_page(self.headers.get("Host") or f"localhost:{CONFIG['port']}"),
                             "text/html; charset=utf-8")
        if self.path.startswith("/report.json"):
            return self.send(200, json.dumps(report_data(), default=str), "application/json")
        if self.path == "/health":
            return self.send(200, "ok")
        if self.path in ("/download/leanarr_worker.py", "/download/Setup-LeanarrWorker.ps1"):
            # remote workers fetch (and update) themselves here; the Windows installer too
            name = self.path.rsplit("/", 1)[1]
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "rb") as f:
                ctype = "text/x-python" if name.endswith(".py") else "application/octet-stream"
                self.send_response(200)
                body = f.read()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f"attachment; filename={name}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        m = re.fullmatch(r"/api/worker/source/(\d+)", self.path)
        if m and self.authed():
            row = job_row(int(m.group(1)))
            if not row:
                return self.send(404, "no such job")
            size = os.path.getsize(row["path"])
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(row["path"], "rb") as f:
                shutil.copyfileobj(f, self.wfile, 4 * 1024 * 1024)
            return
        if not m:
            self.send(404, "not found")

    def do_POST(self):
        if self.path == "/settings":
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
            return self.send(200, settings_page(save_settings(form)), "text/html; charset=utf-8")
        if not self.authed():
            return
        if self.path == "/api/worker/claim":
            body = self.read_json()
            name, kind = str(body.get("name") or "remote")[:40], str(body.get("kind") or "nvenc")
            job, why = claim(name, kind)
            worker_seen(name, kind, str(body.get("version") or "")[:20], self.client_address[0],
                        f"job: {job['name']}" if job else f"idle ({why})")
            return self.send(200, json.dumps(job), "application/json") if job else self.send(204)
        m = re.fullmatch(r"/api/worker/fail/(\d+)", self.path)
        if m:
            body = self.read_json()
            row = job_row(int(m.group(1)))
            if row:
                err = str(body.get("error"))[:250]
                if re.search(r"(?i)download incomplete|timed out|connection|reset by peer|server not reachable", err):
                    # transfer problem (e.g. Leanarr restarted mid-download): not the file's fault -> back in the queue
                    with db() as c:
                        c.execute("update files set status='candidate', worker=null, lease_until=null where path=?", (row["path"],))
                    log(f"REQUEUED (transfer problem, {row['worker']}): {row['path']}: {err}")
                else:
                    record_result(row, "failed: worker: " + err, row["worker"] or "remote")
            return self.send(200, "ok")
        self.send(404, "not found")

    def do_PUT(self):
        m = re.fullmatch(r"/api/worker/result/(\d+)", self.path)
        if not m or not self.authed():
            return self.send(404, "not found") if not m else None
        row = job_row(int(m.group(1)))
        if not row:
            return self.send(404, "no such job (expired or already handled)")
        os.makedirs(CONFIG["work_dir"], exist_ok=True)
        tmp = os.path.join(CONFIG["work_dir"], f"remote-{row['id']}.mkv")
        left = int(self.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(left, 4 * 1024 * 1024))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(tmp)
            record_result(row, "failed: upload incomplete", row["worker"] or "remote")
            return self.send(400, json.dumps({"result": "upload incomplete"}), "application/json")
        result = finalize(row, tmp, row["lease_until"] - int(S("lease_hours")) * H, row["worker"] or "remote")
        record_result(row, result, row["worker"] or "remote")
        self.send(200, json.dumps({"result": result}), "application/json")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Leanarr - seed-aware library shrinker")
    ap.add_argument("--scan", action="store_true", help="scan once, print the report, exit")
    ap.add_argument("--once", action="store_true", help="scan once and shrink everything eligible now (local worker)")
    a = ap.parse_args()
    init_db()
    log(f"Leanarr {VERSION} starting: mode={S('mode')} libraries={CONFIG['libraries']} local={S('local_worker')} "
        f"window={S('window')} remote={'on' if CONFIG['worker_token'] else 'off'} qbit_protection={S('qbit_protection')}")
    if a.scan or a.once:
        scan()
        if a.once:
            local_worker(once=True)
        print_report()
        return
    srv = ThreadingHTTPServer(("0.0.0.0", CONFIG["port"]), Web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=local_worker, daemon=True).start()
    threading.Thread(target=audio_worker, daemon=True).start()
    while True:
        ok = True
        try:
            release_finished_seeds()
        except Exception as e:
            ok = False
            log(f"release check error: {e}")
        try:
            scan()
        except Exception as e:
            ok = False
            log(f"scan error: {e}")
        # after a reboot the other containers may not be up yet: retry in 5 min instead of waiting hours
        time.sleep(CONFIG["scan_hours"] * 3600 if ok else 300)


if __name__ == "__main__":
    main()
