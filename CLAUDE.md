# CLAUDE.md: Leanarr

Guide for AI coding agents (and humans) working on this repository. Organization rules:
https://github.com/H0m3Lab/.github/blob/main/STANDARDS.md

## Purpose

Leanarr re-encodes old, inefficient video (H.264, MPEG-2, VC-1, Xvid, ...) in a Plex / Sonarr / Radarr
library to HEVC to save disk space, **without ever touching a file a qBittorrent torrent still uses**.
It starts in report mode and only rewrites files in `shrink` mode.

## Architecture

One Python process (`leanarr.py`), standard library only:

- **Scanner:** walks `LEANARR_LIBRARIES`, runs `ffprobe`, caches results in SQLite, and decides per file
  (`decide()`): old codec, not HDR/DV, not a sample/extra, large enough.
- **Seed protection:** `Qbit` / `torrent_files()` list every file of every torrent. If qBittorrent cannot
  be reached, everything counts as protected (fail-closed). `precheck()` repeats the check right
  before replacement. `release_finished_seeds()` (opt-in) removes finished torrents, files kept,
  using `seed_rules.json`.
- **Encoder:** `local_worker()` encodes with ffmpeg/libx265 inside `LEANARR_WINDOW` at low priority.
  Remote workers (`leanarr_worker.py`, NVENC or x265) claim jobs over HTTP (`claim()`), upload the
  result, and Leanarr still runs every safety step itself.
- **Verify and replace:** `verify()` checks codec, duration (±1 s), audio/subtitle track counts, and
  minimum saving; `finalize()` copies to a hidden `.part` next to the original, renames over it, and
  asks Sonarr/Radarr to rescan (`arr_rescan()`).
- **Audio step (optional):** `audio_plan()` / `audio_decide()` decide per MKV (from the `atracks` column) whether
  to add a Dolby track made from DTS and drop commentary / other-language tracks (`audio_status` column, separate
  from the shrink `status`); `audio_worker()` takes the longest first and `tidy_file()` writes a `.part` next to
  the original with `tidy_cmd()`, checks it with `tidy_verify()`, re-checks seeding, then renames it over the
  original. `finalize()` runs the same step while a shrunk file is copied in. It never runs on a file a shrink
  job is working on (and the other way round).
- **4K mode (optional):** `decide_uhd()` marks 4K files above the bitrate floor (`uhd` column = 1; only the local
  `uhd_worker()` and remote workers of kind `uhd` take them, `next_candidate(uhd=1)`). `uhd_prepare()` builds the plan
  (checks, x265 params incl. HDR10 metadata, sample points). Local `uhd_encode()`: `uhd_pick_crf()` (samples +
  `vmaf_score()` via FIFOs and the `vmaf` CLI), video-only x265 encode, final VMAF. Remote: `claim_uhd()` sends the plan,
  the worker's `run_uhd()` does the same with ffmpeg's libvmaf and uploads the video-only MKV (progress via
  `/api/worker/progress`, kept/too-small-gain via `/api/worker/report`). Both end in `uhd_assemble()` (Dolby Vision RPU
  with dovi_tool, mkvmerge with the original's other tracks, HDR/DV check) and `finalize()`. Results below target return
  `kept: ...` (status `kept`, never retried unless the file changes).
- **Web UI:** `Web` (`ThreadingHTTPServer`) serves the status page, `/report.json`, `/settings`,
  `/worker-setup`, `/health`, and the worker API on `LEANARR_PORT` (8282).

## Repository layout

```
leanarr.py                  the app: scanner, seed protection, encoder, verify/replace, web UI, worker API
leanarr_worker.py           remote worker (any OS with Python 3 + ffmpeg)
Setup-LeanarrWorker.ps1     one-line Windows worker installer served by /worker-setup
Dockerfile                  python:3.12-slim-trixie + ffmpeg + mkvtoolnix; builds vmaf and dovi_tool
docker-compose.example.yml  copy to docker-compose.yml (git-ignored)
seed_rules.example.json     copy to seed_rules.json (git-ignored); real tracker rules never go in git
.env.example                secrets template
```

## Commands

| Task | Command |
|---|---|
| Syntax check | `python -m py_compile leanarr.py leanarr_worker.py` |
| Lint | `pipx run ruff==0.8.4 check --select E9,F63,F7,F82,F401,F841 --target-version py312 .` |
| Report only | `python3 leanarr.py --scan` |
| Daemon | `python3 leanarr.py` |
| Shrink everything now | `python3 leanarr.py --once` (ignores the window; dangerous on real data) |
| Docker | `docker compose up -d --build` |

CI (`.github/workflows/ci.yml`) runs the compile check, ruff, a JSON check of the example rules,
the Docker build, and a gitleaks history scan. There are no unit tests yet. Test real changes in
`report` mode first, then `shrink` on a **copy** of a few files, never on a live library.

## Configuration

Environment variables `LEANARR_*`, `QBIT_*`, `SONARR_*`, `RADARR_*` (README settings table). Most can
also be changed on `/settings` and are stored in the `settings` table; `S()` / `S_bool()` read the
database value first, then the environment. A new setting needs a safe default, a README row, and a
CHANGELOG line.

## Conventions

- Standard library only. Do not add pip dependencies.
- Single-file app on purpose; keep functions small and named for what they do.
- Log through `log()` (also stored in the `events` table shown on the status page).
- HTML is built with f-strings; escape every value with `html.escape`.

## Data safety (do not break)

- Never write to, rename, or delete a file that any torrent references. Keep the protection fail-closed
  and keep the re-check in `precheck()` right before replacement.
- Never replace an original unless `verify()` passed. Replacement stays copy-to-`.part` then rename.
- `report` stays the default mode.
- Database schema (`init_db()`) changes must be additive so an existing `leanarr.db` keeps working.

## Security

- The worker API requires `LEANARR_WORKER_TOKEN`. Do not add unauthenticated write endpoints.
- No real tracker names, hosts, keys, or paths in code, tests, or docs; examples use placeholders.

## Releases

Semantic versioning. The version is `VERSION` in both `leanarr.py` and `leanarr_worker.py`; keep them
equal. Steps: CHANGELOG `[Unreleased]` → new version section, bump both constants, commit, tag
`vX.Y.Z`, GitHub release with that section as notes.
