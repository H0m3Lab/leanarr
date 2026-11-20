# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/H0m3Lab/leanarr/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/H0m3Lab/leanarr/releases/tag/v0.2.0
