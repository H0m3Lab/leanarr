#!/usr/bin/env python3
"""Leanarr remote worker (by H0m3Lab) - encode jobs on another machine, e.g. a PC with an NVIDIA GPU.

The worker never touches the library. It:
  1. asks the Leanarr server for a job      (POST /api/worker/claim)
  2. downloads the source file over HTTP     (GET  /api/worker/source/<id>)
  3. encodes it locally (NVENC on the GPU, or libx265 on the CPU)
  4. uploads the result                      (PUT  /api/worker/result/<id>)
The server then verifies it and, only if everything checks out, swaps it into the library
(all seeding/torrent protection stays on the server).

    python leanarr_worker.py --server http://YOUR-LEANARR-HOST:8282 --token TOKEN --kind nvenc
Options: --name (default: computer name), --work-dir, --ffmpeg/--ffprobe (paths),
         --once (one job, then exit), --poll (seconds between empty polls, default 300).
Only the Python standard library + ffmpeg are needed (Windows: an ffmpeg build with NVENC,
e.g. the gyan.dev "full" build).
"""
import argparse, http.client, json, os, platform, shlex, subprocess, sys, tempfile, time, urllib.parse

VERSION = "0.5.0"


LOG_FILE = None


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".old")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class Server:
    def __init__(self, url, token):
        u = urllib.parse.urlparse(url)
        self.host, self.port = u.hostname, u.port or 80
        self.token = token

    def conn(self, timeout=60):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def call(self, method, path, body=None, timeout=60):
        c = self.conn(timeout)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers={"X-Leanarr-Token": self.token, "Content-Type": "application/json"})
        r = c.getresponse()
        text = r.read()
        c.close()
        if r.status >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status} {text[:200]!r}")
        return r.status, (json.loads(text) if text else None)

    def download(self, job_id, dest):
        c = self.conn(timeout=600)
        c.request("GET", f"/api/worker/source/{job_id}", headers={"X-Leanarr-Token": self.token})
        r = c.getresponse()
        if r.status != 200:
            raise RuntimeError(f"download: HTTP {r.status}")
        expected = int(r.getheader("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(4 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        c.close()
        if expected and got != expected:
            raise RuntimeError(f"download incomplete ({got} of {expected} bytes)")

    def upload(self, job_id, path):
        size = os.path.getsize(path)
        c = self.conn(timeout=3600)  # the server verifies + copies into the library before answering
        c.putrequest("PUT", f"/api/worker/result/{job_id}")
        c.putheader("X-Leanarr-Token", self.token)
        c.putheader("Content-Type", "application/octet-stream")
        c.putheader("Content-Length", str(size))
        c.endheaders()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                c.send(chunk)
        r = c.getresponse()
        text = r.read()
        c.close()
        return r.status, (json.loads(text) if text else {})


def build_cmd(ffmpeg, src, dst, job, kind):
    ten = "10" in (job.get("pix_fmt") or "")
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if kind == "nvenc":
        cmd += ["-hwaccel", "cuda"]  # decode on the GPU too, keeps the PC's CPU free
    cmd += ["-i", src, "-map", "0", "-map", "-0:d", "-c", "copy"]
    for i, codec in enumerate(job.get("subtitle_codecs") or []):
        if codec == "mov_text":
            cmd += [f"-c:s:{i}", "srt"]  # MP4 text subtitles can't go into MKV as-is
    if kind == "nvenc":
        s = job["nvenc"]
        cmd += ["-c:v:0", "hevc_nvenc", "-preset", s["preset"], "-cq", str(s["cq"]),
                "-pix_fmt", "p010le" if ten else "yuv420p", "-profile:v", "main10" if ten else "main"]
        cmd += shlex.split(s.get("extra") or "")
    else:
        s = job["x265"]
        cmd += ["-c:v:0", "libx265", "-preset", s["preset"], "-crf", str(s["crf"]), "-x265-params", "log-level=error",
                "-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
        cmd += shlex.split(s.get("extra") or "")
    return cmd + ["-max_muxing_queue_size", "4096", dst]


def run_job(srv, job, args):
    jid = job["id"]
    ext = os.path.splitext(job["name"])[1] or ".mkv"
    src = os.path.join(args.work_dir, f"leanarr-src-{jid}{ext}")
    out = os.path.join(args.work_dir, f"leanarr-out-{jid}.mkv")
    try:
        log(f"job {jid}: {job['name']} ({job['size'] / 1e9:.1f} GB) - downloading")
        t0 = time.time()
        srv.download(jid, src)
        log(f"job {jid}: downloaded in {time.time() - t0:.0f} s - encoding ({args.kind})")
        t1 = time.time()
        p = subprocess.run(build_cmd(args.ffmpeg, src, out, job, args.kind), capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg: " + p.stderr.strip()[-400:])
        mins = (time.time() - t1) / 60
        log(f"job {jid}: encoded in {mins:.1f} min, {os.path.getsize(out) / 1e9:.2f} GB - uploading")
        status, res = srv.upload(jid, out)
        log(f"job {jid}: server says: {res.get('result') if isinstance(res, dict) else res} (HTTP {status})")
    except Exception as e:
        log(f"job {jid}: FAILED: {e}")
        try:
            srv.call("POST", f"/api/worker/fail/{jid}", {"error": str(e)[:250]})
        except Exception:
            pass
    finally:
        for f in (src, out):
            try:
                os.remove(f)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Leanarr remote worker")
    ap.add_argument("--server", required=True, help="e.g. http://your-leanarr-host:8282")
    ap.add_argument("--token", required=True)
    ap.add_argument("--kind", choices=["nvenc", "x265"], default="nvenc")
    ap.add_argument("--name", default=platform.node() or "worker")
    ap.add_argument("--work-dir", default=os.path.join(tempfile.gettempdir(), "leanarr-worker"))
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=int, default=300)
    ap.add_argument("--log-file", help="also append the log to this file (rotates at 5 MB)")
    args = ap.parse_args()
    global LOG_FILE
    LOG_FILE = args.log_file
    os.makedirs(args.work_dir, exist_ok=True)
    srv = Server(args.server, args.token)
    log(f"Leanarr worker {VERSION} '{args.name}' ({args.kind}) -> {args.server}, work dir {args.work_dir}")
    if args.kind == "nvenc":
        enc = subprocess.run([args.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "hevc_nvenc" not in enc:
            sys.exit("this ffmpeg has no hevc_nvenc encoder (install an ffmpeg build with NVIDIA support)")
    while True:
        try:
            status, job = srv.call("POST", "/api/worker/claim", {"name": args.name, "kind": args.kind, "version": VERSION})
        except Exception as e:
            log(f"server not reachable: {e}")
            status, job = 0, None
        if status == 200 and job:
            run_job(srv, job, args)
            if args.once:
                return
            continue
        if args.once:
            log("no job available")
            return
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
