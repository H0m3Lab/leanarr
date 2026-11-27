# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-26

### Added
- 4K mode (`uhd_mode`: off / report / shrink, default off). 4K files above `uhd_min_mbps` (default 30, i.e.
  remux / Blu-ray size) are re-encoded with x265 on the server's CPU, never by remote GPU workers. Quality
  first: test samples are encoded from `uhd_crf_start` down to `uhd_crf_min` until every sample reaches the
  VMAF 4K target (`uhd_vmaf`, default 95); after the full encode three other points are measured again. Below
  target, or a saving under `uhd_min_saving`, the original stays. HDR10 signalling and static metadata are
  carried over; Dolby Vision via dovi_tool (profile 7 → 8.1, profile 8 as is; profile 5 and HLG are skipped),
  frame counts of picture and Dolby Vision data must match. Own hours (`uhd_window`).
- The image now builds Netflix vmaf (v3.2.1) and dovi_tool (2.3.4) from their sources and adds mkvmerge.

### Changed
- Base image Debian 13 (trixie): ffmpeg 7.1 instead of 5.1.

## [0.4.0] - 2026-11-26

### Added
- Remove unwanted audio tracks from MKVs: `drop_commentary` (audio + subtitle tracks marked or titled as
  commentary) and `audio_languages` (e.g. `eng`: other-language audio is removed; a file with none of these
  languages keeps its original language; untagged tracks always stay; a file is never left without audio).
  Both off by default.
- The audio step runs during a shrink too, while the encoded file is copied into the library, so a shrunk
  file is not rewritten a second time.
- The scan stores per-track facts (codec, language, channels, title, commentary/default flags); files scanned
  by an older version are read once more.

### Fixed
- The Dolby track was skipped when the only Dolby track was a stereo commentary. It is now added unless a
  kept (non-commentary, wanted-language) Dolby track has at least the DTS track's channels (max 5.1).
- `sample.mkv` files directly in a movie folder were not treated as samples.

## [0.3.0] - 2026-11-25

### Added
- Dolby track for DTS-only files (`dolby_mode`: off / report / add, default off). MKVs whose audio is DTS with
  no AC-3/E-AC-3/TrueHD track get a Dolby track (E-AC-3 or AC-3, up to 5.1, default 640k) made from the DTS
  track, as the first and default audio track; everything else is copied unchanged. Same seeding protection
  (fail-closed, re-checked right before the replace), verification (track counts, length, decode test of the
  new track) and safe replace as shrinking. Longest files first (movies before episodes). Own time window
  (`dolby_window`), own section on the status page and in `/report.json`. Independent of the shrink mode.
- The scan now records each file's audio codecs; files scanned by an older version are read once more.

## [0.2.1] - 2026-11-21

### Changed
- Releasing finished seeds (`release_seeds`) no longer requires shrink mode: it runs on every scan,
  in report mode too. It only removes the torrent from qBittorrent; files on disk are never touched.

## [0.2.0] - 2026-11-19

First public release.

### Added
- Library scan with cached `ffprobe` results; candidates are old codecs only (HDR10, HLG, Dolby Vision,
  HEVC/AV1/VP9, samples, extras, and small files are skipped).
- Fail-closed seed protection through qBittorrent (4.x and 5.x), re-checked right before replacement.
- Optional release of finished seeds based on per-tracker rules in `seed_rules.json`.
- x265 encoding at low priority inside a time window; streams, chapters, and attachments copied.
- Verification before replacement (codec, duration, track counts, minimum saving) and safe
  copy-then-rename replacement, followed by a Sonarr/Radarr rescan.
- Report mode (default), status page, `/report.json`, and a Settings page.
- Remote workers (NVENC or x265) with a one-line Windows installer.
- Docker image, example Compose file, `.env.example`, MIT license.
- Project docs (CLAUDE.md, this changelog) and CI (lint, Docker build, secret scan).

[Unreleased]: https://github.com/H0m3Lab/leanarr/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/H0m3Lab/leanarr/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/H0m3Lab/leanarr/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/H0m3Lab/leanarr/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/H0m3Lab/leanarr/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/H0m3Lab/leanarr/releases/tag/v0.2.0
