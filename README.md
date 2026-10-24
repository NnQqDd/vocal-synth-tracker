# Vocal synth tracker

Python 3.10+, no extra packages. Counts VocaDB-linked videos and their available lifetime views on YouTube, NicoNico, and Bilibili.

## Run

```powershell
cd "$HOME\Desktop\vocal-synth-tracker"
$env:YOUTUBE_API_KEY = "YOUR_YOUTUBE_DATA_API_V3_KEY"
python -X utf8 popularity_tracker.py --vocal miku teto --start 2025-01-01 --end 2025-12-31 --list
```

Enable YouTube Data API v3 and set its key for YouTube views; otherwise they show as `N/A`. NicoNico needs no key. Bilibili may block access; optionally supply your own `BILIBILI_COOKIE`.

## Flags

| Flag | Behavior |
| --- | --- |
| `--vocal NAME [NAME ...]` | Every named synth must have a vocal credit on the same song. Extra vocalists are allowed. |
| `--start`, `--end` | Inclusive **song release dates** in UTC; filter VocaDB before scanning. |
| `--count` | Print only `N candidate songs`, then exit without scanning, fetching views, or creating reports. No YouTube key needed. |
| `--list` | Print each upload, highest views first; unavailable counts last. |
| `--catalog-only` | Collect videos without fetching views. |
| `--original-only` | Include only VocaDB's Original video type. |
| `--out DIR` | Write reports to a new directory. |
| `--help` | Show all options, including cache and request limits. |

**`--count` and `--list` cannot be combined.** Using both exits with an argument error.

## Counting rules

- Names are case-insensitive: `teto`, `TETO`, and `"Kasane Teto"` identify the same character. Voicebank versions are grouped. Full VocaDB names, aliases, artist URLs, and `id:NUMBER` are accepted.
- Each `(platform, video ID)` counts once. Uploads on different platforms count independently.
- All linked uploads of matching songs qualify, regardless of upload date.
- Views are observed **lifetime counts**, not views earned within the date range. Missing counts remain unknown.
- Coverage is limited to VocaDB's catalog. Candidate totals precede local credit/video-type validation.

## Reports and cache

Normal runs create `summary.csv`, `monthly.csv` (song release month), `videos.csv`, and `report.json`. Check missing-view counts and catalog completeness before comparing totals.

Responses are cached in `.vocal_video_cache.sqlite3`: catalog data for 7 days, platform data for 6 hours. Rerun partial queries to reuse the cache. Use `--stats-cache-hours 0` for fresh views.

Exit codes: `0` success, `1` identity error, `2` partial results or argument error, `130` interrupted.

Tests: `python -X utf8 -B -m unittest test_popularity_tracker.py`.

[VocaDB API](https://vocadb.net/swagger/index.html) · [API usage guidance](https://wiki.vocadb.net/docs/public-api)
