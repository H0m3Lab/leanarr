# Leanarr

**Seed-aware library shrinker for Plex / Sonarr / Radarr setups.** By H0m3Lab.

Leanarr re-encodes old, inefficient video (H.264, MPEG-2, VC-1, Xvid, …) to HEVC to win back disk space.
It's built for libraries where **torrents seed straight from the library folders**. Changing a file a torrent
still needs breaks the torrent (and can earn you a hit & run on private trackers), so Leanarr
**never touches a file any torrent in qBittorrent still uses.**

## What it does

1. **Scans** your libraries and reads every video with `ffprobe` (results are cached, so rescans are quick).
2. **Picks candidates:** old codecs only. Files already HEVC/AV1/VP9, every HDR10 / HLG / Dolby Vision file,
   samples, extras and small files are left alone.
3. **Protects seeding files (fail-closed):** it asks qBittorrent for every file of every torrent. If qBittorrent
   can't be reached, *everything* counts as protected. The check runs again right before a file is replaced.
4. **Optionally releases finished seeds:** a stopped torrent that met its tracker's seeding rule is removed from
   qBittorrent (files kept), so its files become eligible. This runs on every scan, in report mode too.
5. **Encodes** with ffmpeg/libx265 (default CRF 22, preset slow) at the lowest CPU priority, only inside a time
   window. Audio, subtitles, chapters and attachments are copied as they are; MP4 text subtitles become SRT.
6. **Verifies** before replacing: HEVC video, same length (±1 s), same number of audio and subtitle tracks, and
   at least `LEANARR_MIN_SAVING` smaller. Anything else and the original stays.
7. **Replaces safely:** the new file is copied next to the original as a hidden `.part`, then renamed over it
   (MP4/AVI become `.mkv`). Sonarr/Radarr get a rescan for that show/movie.
8. **Report mode (default)** changes nothing and shows what it *would* shrink and the estimated saving.

Status page: `http://<host>:8282` (also `/report.json`).

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEANARR_MODE` | `report` | `report` (list only) or `shrink` |
| `LEANARR_LIBRARIES` | `/media/tv,/media/movies` | folders to scan |
| `LEANARR_WINDOW` | `01:00-09:00` | when shrinking may run (`always` = any time) |
| `LEANARR_CRF` / `LEANARR_PRESET` | `22` / `slow` | x265 quality / speed |
| `LEANARR_MIN_SAVING` | `0.10` | keep the result only if at least 10% smaller |
| `LEANARR_MIN_SIZE_MB` | `200` | ignore smaller files |
| `LEANARR_RELEASE_SEEDS` | `false` | remove torrents that met their seeding rule (files kept) |
| `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD` | | qBittorrent WebUI (4.x and 5.x) |
| `SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY` | | for the rescan after a replacement |
| `LEANARR_DB`, `LEANARR_WORK_DIR`, `LEANARR_PORT` | `/config/leanarr.db`, `/work`, `8282` | storage / port |

**Seeding rules** (used only when releasing seeds) live in `seed_rules.json` next to the database
(`/config/seed_rules.json`, or set `LEANARR_SEED_RULES` to another path). One entry per tracker: the tracker
host names, the ratio that counts as done, and the seeding time for single episodes and for season packs.
A torrent counts as finished when it reaches the ratio **or** the time. Copy `seed_rules.example.json` and use
**your trackers' actual rules** (check each site's wiki; private trackers punish hit & runs).
Without the file, Leanarr never releases anything.

**Paths must match:** mount the media at the same path qBittorrent, Sonarr and Radarr use (e.g. `/media`).
Otherwise the torrent protection can't recognise the files.

Most of these can also be changed live on the **Settings** page (`/settings`), stored in the database.

## Remote workers (optional)

Other PCs can encode for Leanarr, e.g. one with an NVIDIA GPU (NVENC) or spare CPU cores. A worker asks
Leanarr for a job over HTTP, downloads the source, encodes, and uploads the result. Leanarr itself still does
every safety step: the seeding check, the verification and the replacement. Set `LEANARR_WORKER_TOKEN`, open
`/worker-setup` on the status page, and run the one-line installer it shows in an administrator PowerShell
on the Windows PC (it installs Python + ffmpeg, tests NVENC and creates a scheduled task). Any other OS:
`python3 leanarr_worker.py --server http://<leanarr-host>:8282 --token <token> --kind nvenc|x265`.

## Install

```bash
git clone https://github.com/H0m3Lab/leanarr.git && cd leanarr
cp docker-compose.example.yml docker-compose.yml   # adjust the paths
cp .env.example .env && chmod 600 .env             # qBittorrent password, Sonarr/Radarr keys
docker compose up -d --build
```

Start in `report` mode, check the status page, and switch to `shrink` once you trust what it picks.

## Commands

```
python3 leanarr.py            # daemon: status page + scanner + night-time worker
python3 leanarr.py --scan     # one scan, print the report, exit
python3 leanarr.py --once     # one scan + shrink everything eligible now (ignores the window)
```

## Troubleshooting

- **Nothing gets shrunk:** check that `LEANARR_MODE` is `shrink` (the default `report` changes nothing)
  and that the current time is inside `LEANARR_WINDOW`.
- **Every file shows as protected:** Leanarr can't reach qBittorrent, so it fails closed. Check
  `QBIT_URL`, `QBIT_USER`, and `QBIT_PASSWORD`.
- **Seeding files are not recognised as protected:** the media paths inside Leanarr must be the same as
  in qBittorrent, Sonarr, and Radarr (e.g. all `/media/...`).
- **A file was skipped after encoding:** verification failed or the saving was below
  `LEANARR_MIN_SAVING`; the original stays. The status page shows the reason.
- Logs: the status page event list, or `docker compose logs leanarr`.

## Roadmap

- Per-library settings, pause/resume from the status page

## Releases and contributing

Versions and changes: [Releases](https://github.com/H0m3Lab/leanarr/releases) and [CHANGELOG.md](CHANGELOG.md).
Developer and AI-agent guide: [CLAUDE.md](CLAUDE.md). Bug reports and ideas are welcome as issues; see the
[contribution guide](https://github.com/H0m3Lab/.github/blob/main/CONTRIBUTING.md). Report security
problems privately through the **Security** tab.

License: MIT · Status: early (0.2). Back up your library before letting any tool rewrite it.
