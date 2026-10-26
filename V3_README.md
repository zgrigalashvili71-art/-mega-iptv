# ALL_WORKING_TV_V3 – consolidated, tested playlist

**AquaPlay URL**

```
https://raw.githubusercontent.com/zgrigalashvili71-art/-mega-iptv/main/ALL_WORKING_TV_V3.m3u
```

Built from local FFprobe/FFmpeg V3 health checks (33 result folders, 41,019 records).
Every stream that actually played is included. When several sources carry the same
channel, the best one comes first and the other working copies follow as
"Name (alt 2)", "Name (alt 3)"… `ALL_TV_PLUS_SPORTS.m3u` (V2) is untouched.

| File | What it is |
|---|---|
| `ALL_WORKING_TV_V3.m3u` | The playlist: every working stream |
| `ALL_WORKING_TV_V3_CLEAN.m3u` | Same channels, one entry each (best stream only) |
| `ALL_WORKING_TV_V3_GEORGIA.m3u` | Georgian TV + Georgian radio only (fast to load) |
| `ALL_WORKING_TV_V3_SPORTS.m3u` | All sports groups only |
| `ALL_WORKING_TV_V3_GEORGIA_SPORTS.m3u` | Georgian TV/radio + all sports |
| `config/epg_sources.txt` | TV guides matched against the channels (Georgian names transliterated) |
| `ALL_WORKING_TV_V3_REPORT.txt` | Counts: statuses, groups, per-source results, validation, smoke test |
| `ALL_WORKING_TV_V3_SOURCES.csv` / `.json` | Provenance per channel: source folders, successes, startup time, tvg-id, logo |
| `ALL_WORKING_TV_V3_BACKUPS.json` | Ranked backup URLs per channel (for failover later) |
| `ALL_WORKING_TV_V3_EXCLUDED.csv` | Everything not used, with reason (secret URLs redacted) |
| `ALL_WORKING_TV_V3_RECHECK_RESTRICTED.m3u` | 401/403/451 streams worth re-testing |
| `ALL_WORKING_TV_V3_RECHECK_OFFLINE_NOW.m3u` | Temporarily failing streams worth re-testing |
| `build_all_working_v3.py` | The builder (Python 3.10+, standard library only) |
| `config/denylist_hosts.txt` | Hosts / URL prefixes never published (pirate restreams); edit freely |
| `config/denylist_channels.txt` | Channel names never published (Georgian pay channels); edit freely |
| `health_data/` | Sanitized copy of the test results the build runs on |

## Rebuild after new tests

```
py -3.11 build_all_working_v3.py --input "C:\Users\<you>\Downloads\IPTV_TEST" --output . --export-health-data health_data --smoke 60
git add health_data ALL_WORKING_TV_V3* && git commit -m "Refresh V3 health data" && git push
```

Pushing `health_data/`, the denylist or the script also triggers
`.github/workflows/build-v3.yml`, which rebuilds the playlist on GitHub (no stream
testing there: US runners see different geo-blocks than a TV box in Georgia).

## Rules in short

- Accepted: V3 `WORKING_NOW` (2 of 3 ffprobe + 5 s ffmpeg playback), V1/V2 `HEALTHY`,
  or `FLAKY` with real audio/video evidence and no 404/410/DRM verdict elsewhere.
- Never published: DRM, user/password or Xtream/Stalker URLs, private or expiring tokens,
  cookies, denylisted hosts and pay channels. VOD files go to the "On Demand" group.
- Same channel from several sources: ranked by health, success ratio, failures,
  playback proof, startup time, direct vs proxied, metadata and source reliability.
- Results are point-in-time from the tester's PC; streams can change at any time.
