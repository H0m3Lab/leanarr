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
import argparse, http.client, json, os, platform, shlex, shutil, subprocess, sys, tempfile, time, urllib.parse

VERSION = "0.6.0"


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
        try:
            return r.status, (json.loads(text) if text else None)
        except ValueError:   # plain-text answers ("ok") from the progress/report/fail endpoints
            return r.status, text.decode(errors="replace")

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

    def upload(self, job_id, path, timeout=3600, headers=None):
        size = os.path.getsize(path)
        c = self.conn(timeout=timeout)  # the server verifies + copies into the library before answering
        c.putrequest("PUT", f"/api/worker/result/{job_id}")
        c.putheader("X-Leanarr-Token", self.token)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
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


# ------------------------------------------------------------------ 4K worker (--kind uhd): x265 + VMAF on this PC
VMAF_MODEL = "vmaf_4k_v0.6.1"


def low_priority():
    """Popen arguments: below-normal priority, so the PC stays usable."""
    if os.name == "nt":
        return {"creationflags": 0x00004000}   # BELOW_NORMAL_PRIORITY_CLASS
    return {"preexec_fn": lambda: os.nice(15)}


def keep_awake(on):
    """Windows: don't let the PC go to sleep while a job runs (a 4K encode takes many hours)."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def ff(args, cmd, timeout=None, cwd=None):
    p = subprocess.run([args.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error"] + cmd, capture_output=True,
                       text=True, timeout=timeout, cwd=cwd, **low_priority())
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.strip()[-400:])
    return p


def x265_args(crf, plan):
    # -dolbyvision 0: ffmpeg 7+ would copy the source's Dolby Vision data into the encode by itself; the server adds
    # it afterwards with dovi_tool, so the picture we upload must be plain HDR10
    return ["-c:v:0", "libx265", "-preset", plan["preset"], "-crf", str(crf), "-x265-params", plan["x265_params"],
            "-dolbyvision", "0", "-pix_fmt", "yuv420p10le", "-fps_mode", "passthrough"]


def vmaf(args, src, t, length, dist, dist_t, tag):
    """VMAF (4K model) of `dist` against the source over [t, t+length], with ffmpeg's libvmaf: (mean, min, frames)."""
    name = f"vmaf-{tag}.json"   # relative to the work dir: Windows paths can't go into a filter string as-is
    # pair frames by their number, not their timestamps: Matroska rounds timestamps to the millisecond, so a sample
    # starting at 0 and the source starting at e.g. 12.137 s round differently and libvmaf would pair neighbours
    graph = ("[0:v]format=yuv420p10le,setpts=N/(24*TB)[d];[1:v]format=yuv420p10le,setpts=N/(24*TB)[r];"
             f"[d][r]libvmaf=model=version={VMAF_MODEL}:n_threads={os.cpu_count() or 4}:log_fmt=json:log_path={name}"
             ":shortest=1")
    # the reference runs 1 s longer and the distorted clip decides the end (cuts at the end can differ by a frame)
    ff(args, ["-ss", f"{dist_t:.3f}", "-t", f"{length:.3f}", "-i", dist, "-ss", f"{t:.3f}", "-t", f"{length + 1:.3f}", "-i", src,
              "-lavfi", graph, "-f", "null", "-"], timeout=6 * 3600, cwd=args.work_dir)
    path = os.path.join(args.work_dir, name)
    with open(path) as f:
        res = json.load(f)
    os.remove(path)
    pooled = res["pooled_metrics"]["vmaf"]
    if pooled["mean"] < 70:   # no real encode at these settings scores this low: the frames did not line up
        raise RuntimeError(f"VMAF {pooled['mean']:.0f} at {t:.0f} s: frames misaligned, measurement not usable")
    return pooled["mean"], pooled["min"], len(res.get("frames") or [])


def check_uhd_tools(args):
    enc = subprocess.run([args.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    flt = subprocess.run([args.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if "libx265" not in enc or "libvmaf" not in flt:
        sys.exit("this ffmpeg has no libx265 encoder or libvmaf filter (the 4K worker needs both)")
    os.makedirs(args.work_dir, exist_ok=True)
    test = os.path.join(args.work_dir, "vmaf-selftest.mkv")
    ff(args, ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-t", "1"]
       + x265_args(20, {"preset": "ultrafast", "x265_params": "log-level=error"}) + [test])
    mean, _, frames = vmaf(args, test, 0, 1, test, 0, "selftest")
    os.remove(test)
    log(f"4K tools OK: libx265 + libvmaf ({VMAF_MODEL}), self-test VMAF {mean:.1f} over {frames} frames")


def run_uhd(srv, job, args):
    jid, plan = job["id"], job["uhd"]
    src = os.path.join(args.work_dir, f"leanarr-src-{jid}{os.path.splitext(job['name'])[1] or '.mkv'}")
    out = os.path.join(args.work_dir, f"leanarr-out-{jid}.mkv")
    samples = [os.path.join(args.work_dir, f"leanarr-sample-{jid}-{i}.mkv") for i in range(len(plan["samples"]))]

    def tell(note=None, line=None):
        if line:
            log(f"job {jid}: {line}")
        try:
            srv.call("POST", f"/api/worker/progress/{jid}", {"note": note, "log": line})
        except Exception:
            pass

    keep_awake(True)
    try:
        free = shutil.disk_usage(args.work_dir).free
        if free < job["size"] * 1.8 + 20e9:
            raise RuntimeError(f"not enough space on the worker ({free / 1e9:.0f} GB free in {args.work_dir}, "
                               f"needs ~{(job['size'] * 1.8 + 20e9) / 1e9:.0f} GB)")
        tell(f"4K: downloading {job['name']}", f"{job['name']} ({job['size'] / 1e9:.1f} GB) - downloading")
        t0 = time.time()
        srv.download(jid, src)
        tell(None, f"downloaded in {time.time() - t0:.0f} s, testing quality (CRF {plan['crf_start']} down to {plan['crf_min']})")
        length, crf, chosen, scores = plan["length"], plan["crf_start"], None, []
        while crf >= plan["crf_min"]:
            scores, size = [], 0
            for i, t in enumerate(plan["samples"]):
                tell(f"4K: test CRF {crf}, sample {i + 1} of {len(plan['samples'])}")
                ff(args, ["-ss", f"{t:.3f}", "-i", src, "-t", f"{length:.3f}", "-map", "0:v:0", "-an", "-sn", "-dn"]
                   + x265_args(crf, plan) + ["-f", "matroska", samples[i]], timeout=12 * 3600)
                size += os.path.getsize(samples[i])
                mean, low, frames = vmaf(args, src, t, length, samples[i], 0.0, f"{jid}-s{i}")
                os.remove(samples[i])
                if frames < length * 20:
                    raise RuntimeError(f"VMAF compared only {frames} frames of a {length:.0f} s sample")
                scores.append(round(mean, 2))
            mbps = size * 8 / (len(plan["samples"]) * length) / 1e6
            tell(None, f"4K test CRF {crf}: VMAF {min(scores)}-{max(scores)} (target {plan['vmaf']:g}), video ~{mbps:.0f} Mbps")
            if min(scores) >= plan["vmaf"]:
                chosen, est = crf, int(mbps * 1e6 / 8 * plan["duration"])
                break
            crf -= 2
        if chosen is None:
            srv.call("POST", f"/api/worker/report/{jid}", {"result": f"kept: quality target VMAF {plan['vmaf']:g} not "
                                                                     f"reached even at CRF {plan['crf_min']} (samples {scores})"})
            log(f"job {jid}: kept (quality target not reached)")
            return
        whole = est + plan["nonvideo"]
        if whole > plan["size"] * (1 - plan["min_saving"]):
            srv.call("POST", f"/api/worker/report/{jid}", {"result": f"too-small-gain: samples say "
                                                                     f"{whole / plan['size']:.0%} of the original at CRF {chosen}"})
            log(f"job {jid}: too small a saving at CRF {chosen}")
            return
        tell(None, f"4K ENCODE CRF {chosen} (samples VMAF {scores}, expect ~{whole / 1e9:.1f} GB)")
        total = max(1, int(plan["duration"] * plan.get("fps", 23.976)))
        t1, last_note, last_line = time.time(), 0, 0
        with open(out + ".err", "w") as err:
            p = subprocess.Popen([args.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
                                  "-i", src, "-map", "0:v:0"] + x265_args(chosen, plan) + ["-f", "matroska", out],
                                 stdout=subprocess.PIPE, stderr=err, text=True, **low_priority())
            for line in p.stdout:
                if not line.startswith("frame="):
                    continue
                frame = int(line.split("=", 1)[1] or 0)
                el = time.time() - t1
                if frame and time.time() - last_note > 300:
                    fps = frame / el
                    pct = 100 * frame / total
                    left = (total - frame) / fps / 3600 if fps else 0
                    note = f"4K encode {pct:.1f}% at {fps:.2f} fps, ~{left:.1f} h left (CRF {chosen})"
                    tell(note, note if time.time() - last_line > 3 * 3600 else None)
                    last_line = time.time() if time.time() - last_line > 3 * 3600 else last_line
                    last_note = time.time()
            p.wait()
        if p.returncode != 0:
            raise RuntimeError("ffmpeg: " + open(out + ".err").read().strip()[-400:])
        hours = (time.time() - t1) / 3600
        tell(None, f"encoded in {hours:.1f} h ({total / (hours * 3600):.2f} fps average), "
                   f"{os.path.getsize(out) / 1e9:.1f} GB video - checking quality")
        final = []
        for i, t in enumerate(plan["checks"]):
            tell(f"4K: final check {i + 1} of {len(plan['checks'])}")
            mean, low, frames = vmaf(args, src, t, length, out, t, f"{jid}-f{i}")
            if frames < length * 20:
                raise RuntimeError(f"final VMAF compared only {frames} frames")
            final.append(round(mean, 2))
        if min(final) < plan["vmaf"] - 1:
            srv.call("POST", f"/api/worker/report/{jid}", {"result": f"kept: finished file scored VMAF {final} "
                                                                     f"(target {plan['vmaf']:g})"})
            log(f"job {jid}: kept (finished file VMAF {final})")
            return
        tell("4K: uploading", f"final VMAF {final} - uploading")
        os.remove(src)   # free the space before the upload
        status, res = srv.upload(jid, out, timeout=6 * 3600, headers={"X-Leanarr-Vmaf": str(final)})
        log(f"job {jid}: server says: {res.get('result') if isinstance(res, dict) else res} (HTTP {status})")
    except Exception as e:
        log(f"job {jid}: FAILED: {e}")
        try:
            srv.call("POST", f"/api/worker/fail/{jid}", {"error": str(e)[:250]})
        except Exception:
            pass
    finally:
        keep_awake(False)
        for f in [src, out, out + ".err"] + samples:
            try:
                os.remove(f)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Leanarr remote worker")
    ap.add_argument("--server", required=True, help="e.g. http://your-leanarr-host:8282")
    ap.add_argument("--token", required=True)
    ap.add_argument("--kind", choices=["nvenc", "x265", "uhd"], default="nvenc",
                    help="uhd = 4K worker: x265 + VMAF quality checks (needs ffmpeg with libx265 + libvmaf)")
    ap.add_argument("--name", default=platform.node() or "worker")
    ap.add_argument("--work-dir", default=os.path.join(tempfile.gettempdir(), "leanarr-worker"))
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=int, default=300)
    ap.add_argument("--log-file", help="also append the log to this file (rotates at 5 MB)")
    ap.add_argument("--selftest", action="store_true", help="check the tools (encoder, VMAF) and exit")
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
    if args.kind == "uhd":
        check_uhd_tools(args)
    if args.selftest:
        return
    while True:
        try:
            status, job = srv.call("POST", "/api/worker/claim", {"name": args.name, "kind": args.kind, "version": VERSION})
        except Exception as e:
            log(f"server not reachable: {e}")
            status, job = 0, None
        if status == 200 and job:
            (run_uhd if job.get("kind") == "uhd" else run_job)(srv, job, args)
            if args.once:
                return
            continue
        if args.once:
            log("no job available")
            return
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
