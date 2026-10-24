#!/usr/bin/env python3
"""Count videos and lifetime views for songs released in a date range and crediting ALL requested vocal synths.

  python popularity_tracker.py --vocal miku teto --start 2025-01-01 --end 2025-12-31 --list

Python 3.10+, standard library only. See README.md for credentials and coverage.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

UTC = timezone.utc
SERVICES = {"Youtube": "youtube", "NicoNicoDouga": "niconico", "Bilibili": "bilibili"}
VDB = "https://vocadb.net"
NICO = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class ApiError(Exception):
    pass


def iso(value):
    """Treat timezone-less VocaDB dates as UTC calendar dates."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def utcnow():
    return datetime.now(UTC).isoformat(timespec="seconds")


def count(value):
    """Never turn absent/hidden counts into zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(str(value))
        return n if n >= 0 else None
    except (ValueError, TypeError):
        return None


def canonical_id(service, pv):
    text = str(pv.get("pvId") or "")
    url = str(pv.get("url") or "")
    if service == "Youtube":
        m = re.fullmatch(r"[A-Za-z0-9_-]{11}", text)
        return m.group(0) if m else None
    if service == "NicoNicoDouga":
        m = re.search(r"(?:sm|nm|so)\d+", text + " " + url)
        return m.group(0) if m else None
    if service == "Bilibili":
        # Strip part/page suffixes: Bilibili stat.view is for the whole video.
        m = re.search(r"BV[A-Za-z0-9]{10}", text + " " + url)
        if m:
            return m.group(0)
        m = re.search(r"(?i)av(\d+)", text + " " + url)
        if m:
            return "av" + m.group(1)
        if text.isdigit():
            return "av" + text
    return None


class Client:
    def __init__(self, args):
        self.args = args
        self.db = sqlite3.connect(args.cache)
        self.db.execute("CREATE TABLE IF NOT EXISTS responses "
                        "(cache_key TEXT PRIMARY KEY, fetched TEXT, payload TEXT)")
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.last_request = {}
        self.requests = defaultdict(int)
        self.hits = defaultdict(int)
        self.blocked = {}
        self.artist_roots = {}

    def get(self, url, params=None, *, ttl=0, platform="vocadb"):
        params = params or {}
        public_params = {k: v for k, v in params.items() if k != "key"}
        cache_key = hashlib.sha256(
            (url + "?" + urlencode(sorted(public_params.items()), doseq=True)).encode()).hexdigest()
        if ttl and not self.args.refresh:
            row = self.db.execute(
                "SELECT fetched,payload FROM responses WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if row and (datetime.now(UTC) - iso(row[0])).total_seconds() < ttl:
                self.hits[platform] += 1
                return json.loads(row[1]), row[0]
        if platform in self.blocked:
            raise ApiError(self.blocked[platform])
        limit = (self.args.max_vocadb_requests if platform == "vocadb"
                 else self.args.max_platform_requests)
        headers = {"User-Agent": "VocalVideoTracker/2.0", "Accept": "application/json"}
        if platform == "bilibili":
            headers.update({"User-Agent": BROWSER_UA, "Referer": "https://www.bilibili.com/"})
            if os.getenv("BILIBILI_COOKIE"):
                headers["Cookie"] = os.environ["BILIBILI_COOKIE"]
        full_url = url + ("?" + urlencode(params, doseq=True) if params else "")
        for attempt in range(3):
            if self.requests[platform] >= limit:
                raise ApiError(f"{platform}: request budget reached ({limit}); rerun to use cache")
            delay = self.args.delay - (time.monotonic() - self.last_request.get(platform, 0))
            if delay > 0:
                time.sleep(delay)
            self.requests[platform] += 1
            self.last_request[platform] = time.monotonic()
            try:
                with self.opener.open(Request(full_url, headers=headers), timeout=30) as r:
                    payload = json.load(r)
                if platform == "youtube" and "error" in payload:
                    raise ApiError("youtube: API error")
                if platform == "bilibili" and payload.get("code") != 0:
                    code = payload.get("code")
                    msg = f"bilibili: API code {code}"
                    if code in (-352, -412, -403, -101):
                        self.blocked[platform] = msg
                    raise ApiError(msg)
                if platform == "niconico" and "meta" in payload:
                    if payload["meta"].get("status") != 200:
                        raise ApiError("niconico: snapshot API returned an error")
                fetched = utcnow()
                self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                                (cache_key, fetched, json.dumps(payload, ensure_ascii=False)))
                self.db.commit()
                return payload, fetched
            except HTTPError as e:
                reason = ""
                if platform == "youtube":
                    try:
                        err = json.loads(e.read())
                        reason = err["error"]["errors"][0]["reason"]
                    except (ValueError, KeyError, IndexError, TypeError):
                        pass
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_after = e.headers.get("Retry-After", "")
                    wait = min(float(retry_after), 60) if retry_after.isdigit() else 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                msg = f"{platform}: HTTP {e.code}" + (f" ({reason})" if reason else "")
                if e.code in (401, 403, 412, 429):
                    self.blocked[platform] = msg
                raise ApiError(msg) from None
            except (URLError, TimeoutError, OSError) as e:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ApiError(f"{platform}: network error ({type(e).__name__})") from None
            except (ValueError, UnicodeError):
                raise ApiError(f"{platform}: invalid JSON response") from None


SYNTH_TYPES = {
    "Vocaloid", "UTAU", "CeVIO", "OtherVoiceSynthesizer", "SynthesizerV",
    "NEUTRINO", "VoiSona", "NewType", "Voiceroid", "VOICEVOX",
    "ACEVirtualSinger", "AIVOICE",
}
# Other names are resolved against VocaDB's own names and aliases.
ALIASES = {
    "miku": "Hatsune Miku", "hatsunemiku": "Hatsune Miku",
    "mikuhatsune": "Hatsune Miku", "teto": "Kasane Teto",
    "kasaneteto": "Kasane Teto", "tetokasane": "Kasane Teto",
}


def normalize_name(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if c.isalnum())


def root_artist(client, artist_id):
    if not hasattr(client, "artist_roots"):
        client.artist_roots = {}
    seen = set()
    while artist_id not in seen:
        if artist_id in client.artist_roots:
            root = client.artist_roots[artist_id]
            break
        seen.add(artist_id)
        a, _ = client.get(f"{VDB}/api/artists/{artist_id}",
                         {"fields": "BaseVoicebank,Names", "lang": "English"},
                         ttl=7 * 86400)
        if a.get("deleted") or a.get("mergedTo"):
            raise ApiError(f"VocaDB artist {artist_id} was deleted or merged")
        if a.get("artistType") not in SYNTH_TYPES:
            raise ApiError(f"VocaDB artist {artist_id} is not a vocal synth")
        parent = a.get("baseVoicebank")
        if not parent:
            root = a
            break
        artist_id = parent["id"]
    else:
        raise ApiError("VocaDB base-voicebank cycle")
    for visited in seen:
        client.artist_roots[visited] = root
    return root


def resolve_vocals(client, values):
    """Case-insensitive aliases; duplicate names/voicebanks collapse by root ID."""
    roots, resolved_inputs = {}, {}
    for raw in values:
        name = raw.strip()
        norm = normalize_name(name)
        if not norm:
            raise ApiError("empty --vocal name")
        query = ALIASES.get(norm, name)
        lookup = normalize_name(query)
        if lookup in resolved_inputs:
            continue
        match = re.fullmatch(r"(?:id:)?(\d+)", name, re.I)
        url_match = re.fullmatch(r"https?://(?:www\.)?vocadb\.net/Ar/(\d+)/?", name, re.I)
        if match or url_match:
            root = root_artist(client, int((match or url_match).group(1)))
        else:
            data, _ = client.get(f"{VDB}/api/artists",
                {"query": query, "nameMatchMode": "Exact", "maxResults": 100,
                 "fields": "Names", "artistTypes": ",".join(sorted(SYNTH_TYPES)),
                 "lang": "English"}, ttl=7 * 86400)
            candidates = []
            for artist in data.get("items", []):
                names = [artist.get("name", ""), artist.get("defaultName", "")]
                names += [n["value"] for n in artist.get("names") or []]
                if artist.get("artistType") in SYNTH_TYPES and any(
                        normalize_name(n) == lookup for n in names):
                    candidates.append(artist)
            families = {}
            for artist in candidates:
                root = root_artist(client, artist["id"])
                families[root["id"]] = root
            if not families:
                raise ApiError(f"No vocal synth matched {name!r}; use its full VocaDB name or id:NUMBER")
            if len(families) != 1:
                choices = ", ".join(f"{a['name']} (id:{a['id']})" for a in families.values())
                raise ApiError(f"Ambiguous vocal {name!r}: {choices}")
            root = next(iter(families.values()))
        roots[root["id"]] = root
        resolved_inputs[lookup] = root["id"]
    return sorted(roots.values(), key=lambda a: a["id"])


def song_matches(client, song, required):
    """Check AND on one song's vocal credits before touching its video links."""
    matched = set()
    cache = getattr(client, "artist_roots", {})
    tokens = set()
    for root in cache.values():
        if root["id"] in required:
            names = [root["name"].replace(" (Unknown)", "")]
            names += [n["value"] for n in root.get("names") or []]
            tokens.update(normalize_name(n.replace(" (Unknown)", "")) for n in names)
    tokens.discard("")
    def priority(credit):
        artist = credit.get("artist") or {}
        known = cache.get(artist.get("id"))
        if known and known["id"] in required:
            return 0
        label = normalize_name(" ".join(str(artist.get(k) or "") for k in
                                        ("name", "defaultName", "additionalNames")))
        return 1 if any(token in label for token in tokens) else 2
    for credit in sorted(song.get("artists") or [], key=priority):
        if "Vocalist" not in {c.strip() for c in str(credit.get("categories", "")).split(",")}:
            continue
        artist = credit.get("artist") or {}
        if artist.get("artistType") not in SYNTH_TYPES:
            continue
        matched.add(root_artist(client, artist["id"])["id"])
        if required <= matched:
            return True
    return False


def add_song(records, song, fetched, args, issues):
    for pv in song.get("pvs") or []:
        service = pv.get("service")
        if service not in SERVICES or (args.original_only and pv.get("pvType") != "Original"):
            continue
        vid = canonical_id(service, pv)
        if not vid:
            issues.append(f"Unrecognized {service} PV id on song {song['id']}")
            continue
        key = (SERVICES[service], vid)
        row = records.setdefault(key, {
            "platform": key[0], "video_id": vid, "url": pv.get("url", ""),
            "title": pv.get("name") or song["name"], "song_ids": set(),
            "song_names": set(), "song_release_dates": set(), "pv_dates": set(), "pv_types": set(),
            "views": None, "stats_fetched_at": "", "published_at": "",
            "date_source": "", "status": "not_fetched", "catalog_fetched_at": fetched,
            "disabled_in_vocadb": False,
        })
        row["song_ids"].add(song["id"])
        row["song_names"].add(song["name"])
        if iso(song.get("publishDate")):
            row["song_release_dates"].add(iso(song["publishDate"]).isoformat())
        row["pv_types"].add(pv.get("pvType") or "Unknown")
        if iso(pv.get("publishDate")):
            row["pv_dates"].add(iso(pv["publishDate"]).isoformat())
        row["disabled_in_vocadb"] |= bool(pv.get("disabled"))


def catalog_params(vocals, args, offset=0, *, count_only=False):
    start = datetime.combine(args.start, datetime.min.time(), UTC)
    stop = datetime.combine(args.end + timedelta(days=1), datetime.min.time(), UTC)
    return {"artistId[]": sorted({v["id"] for v in vocals}), "childVoicebanks": "true",
            "pvServices": ",".join(SERVICES), "onlyWithPvs": "true",
            "maxResults": 0 if count_only else 100, "start": offset, "sort": "PublishDate",
            "getTotalCount": "true" if offset == 0 else "false",
            "fields": "None" if count_only else "PVs,Artists", "lang": "English",
            "afterDate": start.isoformat(), "beforeDate": stop.isoformat()}


def candidate_count(client, vocals, args):
    data, _ = client.get(f"{VDB}/api/songs", catalog_params(vocals, args, count_only=True),
                         ttl=7 * 86400)
    total = count(data.get("totalCount"))
    if total is None:
        raise ApiError("VocaDB response is missing a valid totalCount")
    return total


def collect_catalog(client, vocals, args, records, issues, coverage):
    """Request only songs in the inclusive release-date range from VocaDB."""
    start = datetime.combine(args.start, datetime.min.time(), UTC)
    stop = datetime.combine(args.end + timedelta(days=1), datetime.min.time(), UTC)
    required = {v["id"] for v in vocals}
    if not hasattr(client, "artist_roots"):
        client.artist_roots = {}
    client.artist_roots.update({v["id"]: v for v in vocals})
    offset, expected, seen = 0, None, set()
    coverage.update(finished=False, songs_returned=0, matching_songs=0)
    while True:
        params = catalog_params(vocals, args, offset)
        data, fetched = client.get(f"{VDB}/api/songs", params, ttl=7 * 86400)
        batch = data.get("items")
        if not isinstance(batch, list):
            raise ApiError("vocadb: song response missing items")
        if expected is None:
            expected = count(data.get("totalCount"))
            if expected is None:
                raise ApiError("VocaDB response is missing a valid totalCount")
            coverage["advertised_song_count"] = expected
            print(f"{expected} candidate songs", flush=True)
        if not batch:
            if expected is not None and offset < expected:
                raise ApiError("VocaDB pagination ended before the advertised song count")
            break
        new_ids = {s["id"] for s in batch} - seen
        if not new_ids:
            raise ApiError("VocaDB repeated a page; catalog is partial")
        if any(s["id"] in seen for s in batch):
            issues.append("VocaDB changed during pagination; overlapping pages were detected")
        for song in batch:
            if song["id"] in seen:
                continue
            # Do not mark a song processed until its credit validation succeeds.
            released = iso(song.get("publishDate"))
            if released is None:
                issues.append(f"Song {song['id']} has no release date and was excluded")
            elif start <= released < stop and song_matches(client, song, required):
                add_song(records, song, fetched, args, issues)
                coverage["matching_songs"] += 1
            seen.add(song["id"])
        coverage["songs_returned"] = len(seen)
        offset += len(batch)
        if expected is not None and offset >= expected:
            break
    coverage["finished"] = True


def update_stats(row, views, published, fetched):
    row["views"] = count(views)
    row["stats_fetched_at"] = fetched
    d = iso(published)
    if d:
        row["published_at"] = d.isoformat()
        row["date_source"] = "platform"
    row["status"] = "ok" if row["views"] is not None else "view_count_unavailable"


def enrich(client, records, args, issues):
    rows = list(records.values())
    if args.catalog_only:
        return
    yt = [r for r in rows if r["platform"] == "youtube"]
    key = os.getenv("YOUTUBE_API_KEY", "")
    if yt and not key:
        issues.append("youtube: YOUTUBE_API_KEY is unset; view counts are unavailable")
    for pos in range(0, len(yt), 50):
        chunk = yt[pos:pos + 50]
        if not key:
            for row in chunk:
                row["status"] = "missing_api_key"
            continue
        try:
            data, fetched = client.get("https://www.googleapis.com/youtube/v3/videos",
                {"part": "snippet,statistics", "id": ",".join(r["video_id"] for r in chunk),
                 "key": key}, ttl=args.stats_cache_hours * 3600, platform="youtube")
            items = {i["id"]: i for i in data.get("items", [])}
            for row in chunk:
                item = items.get(row["video_id"])
                if item:
                    update_stats(row, item.get("statistics", {}).get("viewCount"),
                                 item.get("snippet", {}).get("publishedAt"), fetched)
                else:
                    row["status"] = "not_returned_by_platform"
        except ApiError as e:
            issues.append(str(e))
            for row in chunk:
                row["status"] = str(e)
    nico = [r for r in rows if r["platform"] == "niconico"]
    if nico:
        try:
            version, _ = client.get(NICO + "version", ttl=args.stats_cache_hours * 3600,
                                    platform="niconico")
            client.nico_snapshot_version = version.get("last_modified")
        except ApiError as e:
            issues.append(str(e))
    for pos in range(0, len(nico), 100):
        chunk = nico[pos:pos + 100]
        params = {"q": "", "targets": "title", "fields": "contentId,viewCounter,startTime",
                  "_sort": "+startTime", "_offset": 0, "_limit": 100,
                  "_context": "VocalVideoTracker"}
        params.update({f"filters[contentId][{i}]": r["video_id"]
                       for i, r in enumerate(chunk)})
        try:
            data, fetched = client.get(NICO + "video/contents/search", params,
                ttl=args.stats_cache_hours * 3600, platform="niconico")
            items = {i["contentId"]: i for i in data.get("data", [])}
            for row in chunk:
                item = items.get(row["video_id"])
                if item:
                    update_stats(row, item.get("viewCounter"), item.get("startTime"), fetched)
                else:
                    row["status"] = "not_in_niconico_snapshot"
        except ApiError as e:
            issues.append(str(e))
            for row in chunk:
                row["status"] = str(e)
    bili = [r for r in rows if r["platform"] == "bilibili"]
    for row in bili:
        vid = row["video_id"]
        params = {"aid": vid[2:]} if vid.startswith("av") else {"bvid": vid}
        try:
            data, fetched = client.get("https://api.bilibili.com/x/web-interface/view",
                params, ttl=args.stats_cache_hours * 3600, platform="bilibili")
            data = data.get("data") or {}
            # Merge av/BV identifiers after enrichment before aggregation.
            if data.get("bvid"):
                row["video_id"] = data["bvid"]
                row["url"] = "https://www.bilibili.com/video/" + data["bvid"]
            pub = datetime.fromtimestamp(data["pubdate"], UTC).isoformat() if data.get("pubdate") else None
            update_stats(row, data.get("stat", {}).get("view"), pub, fetched)
        except ApiError as e:
            issues.append(str(e))
            row["status"] = str(e)


def merge_records(records):
    merged = {}
    for row in records.values():
        key = (row["platform"], row["video_id"])
        if key not in merged:
            merged[key] = row
            continue
        dst = merged[key]
        for field in ("song_ids", "song_names", "song_release_dates", "pv_dates", "pv_types"):
            dst[field].update(row[field])
        dst["disabled_in_vocadb"] |= row["disabled_in_vocadb"]
        if row["views"] is not None and (dst["views"] is None or
                                        row["stats_fetched_at"] > dst["stats_fetched_at"]):
            for field in ("views", "stats_fetched_at", "published_at", "date_source", "status", "url"):
                dst[field] = row[field]
    return list(merged.values())


def prepare_rows(rows):
    """Upload dates are metadata; monthly buckets follow song release dates."""
    for row in rows:
        if not row["published_at"] and row["pv_dates"]:
            row["published_at"] = min(row["pv_dates"])
            row["date_source"] = "vocadb_pv"
        row["period"] = min(row["song_release_dates"])[:7] if row["song_release_dates"] else "unknown"
    return rows


def ranking_key(row):
    return (row["views"] is None, -(row["views"] or 0),
            " / ".join(sorted(row["song_names"])).casefold(), row["platform"], row["video_id"])


def print_list(rows, stream=None):
    stream = stream or sys.stdout
    print("\nViews | Platform | Song | Video URL", file=stream)
    for row in sorted(rows, key=ranking_key):
        views = f"{row['views']:,}" if row["views"] is not None else "N/A"
        song = " / ".join(sorted(row["song_names"])) or row["title"]
        song = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", song)
        print(f"{views} | {row['platform']} | {song} | {row['url']}", file=stream)


def summarize(rows, coverage):
    summaries = []
    for platform in ("youtube", "niconico", "bilibili", "ALL"):
        group = [r for r in rows if platform == "ALL" or r["platform"] == platform]
        known = [r for r in group if r["views"] is not None]
        summaries.append({
            "platform": platform, "videos": len(group),
            "songs_with_videos": len(set().union(*(r["song_ids"] for r in group))),
            "known_lifetime_views": sum(r["views"] for r in known) if known or not group else None,
            "videos_with_views": len(known), "videos_missing_views": len(group) - len(known),
            "catalog_complete": coverage.get("finished", False),
        })
    return summaries


def csv_write(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            out = {}
            for field in fields:
                value = row.get(field)
                if isinstance(value, set):
                    value = ";".join(map(str, sorted(value)))
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                out[field] = value
            w.writerow(out)


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vocal", nargs="+", action="extend", required=True,
                   help="vocal synth names/aliases or id:NUMBER; ALL must be credited on each song")
    p.add_argument("--start", type=date.fromisoformat, required=True, help="first song release date, inclusive YYYY-MM-DD (UTC)")
    p.add_argument("--end", "--date", "--data", dest="end", type=date.fromisoformat,
                   required=True, help="last song release date, inclusive YYYY-MM-DD (UTC)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--count", action="store_true", help="print only the candidate song count and stop")
    mode.add_argument("--list", action="store_true", help="print every selected upload in descending view order; N/A last")
    p.add_argument("--catalog-only", action="store_true", help="count cataloged uploads without fetching platform views")
    p.add_argument("--original-only", action="store_true", help="include only VocaDB PV type Original")
    p.add_argument("--out", type=Path, help="new report directory; default is timestamped")
    p.add_argument("--cache", default=".vocal_video_cache.sqlite3")
    p.add_argument("--refresh", action="store_true", help="bypass cached responses")
    p.add_argument("--stats-cache-hours", type=float, default=6)
    p.add_argument("--delay", type=float, default=1, help="minimum seconds between requests to each API")
    p.add_argument("--max-vocadb-requests", type=int, default=400, help="network calls per run; cache hits are free")
    p.add_argument("--max-platform-requests", type=int, default=1000, help="network calls per video platform per run")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.start > args.end:
        p.error("--start must be on or before --end")
    if args.end == date.max:
        p.error("--end must be earlier than 9999-12-31")
    if args.delay < 0 or args.stats_cache_hours < 0 or min(args.max_vocadb_requests, args.max_platform_requests) < 1:
        p.error("delays/cache hours must be nonnegative and request budgets positive")
    vocal_names = [v.strip() for item in args.vocal for v in item.split(",") if v.strip()]
    if not vocal_names:
        p.error("--vocal must contain at least one name")
    out = args.out or Path("vocal_views_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ"))
    if not args.count and out.exists():
        p.error(f"output directory already exists: {out}; use a new --out")
    client, records, issues, coverage = Client(args), {}, [], {}
    started = utcnow()
    try:
        try:
            vocals = resolve_vocals(client, vocal_names)
        except ApiError as e:
            print(f"Could not resolve --vocal: {e}", file=sys.stderr)
            return 1
        if args.count:
            try:
                total = candidate_count(client, vocals, args)
            except ApiError as e:
                print(str(e), file=sys.stderr)
                return 2
            print(f"{total} candidate songs", flush=True)
            return 0
        out.mkdir(parents=True)
        try:
            collect_catalog(client, vocals, args, records, issues, coverage)
        except ApiError as e:
            issues.append(str(e))
            coverage["error"] = str(e)
        enrich(client, records, args, issues)
        rows = prepare_rows(merge_records(records))
        rows.sort(key=ranking_key)
        summaries = summarize(rows, coverage)
        fields = ["platform", "video_id", "song_names", "song_ids", "song_release_dates", "url", "title",
                  "published_at", "date_source", "period", "views", "stats_fetched_at",
                  "status", "pv_types", "disabled_in_vocadb", "catalog_fetched_at"]
        csv_write(out / "videos.csv", rows, fields)
        csv_write(out / "summary.csv", summaries, list(summaries[0]))
        monthly = []
        for period in sorted({r["period"] for r in rows}):
            for s in summarize([r for r in rows if r["period"] == period], coverage):
                monthly.append({"period": period, **s})
        csv_write(out / "monthly.csv", monthly, ["period", *summaries[0]])
        total = summaries[-1]
        issues = sorted(set(issues))
        partial = bool(issues or not coverage.get("finished") or
                       (total["videos_missing_views"] and not args.catalog_only))
        report = {
            "started_at": started, "finished_at": utcnow(), "vocals_input": vocal_names,
            "vocals_resolved": [{"id": v["id"], "name": v["name"]} for v in vocals],
            "match": "ALL requested vocal synth identities credited as vocalists on the same song",
            "start_inclusive": str(args.start), "end_inclusive": str(args.end),
            "date_basis": "song_release_utc",
            "view_definition": "observed cumulative lifetime views",
            "scope": "VocaDB catalog links to YouTube, NicoNico, Bilibili only",
            "deduplication": "(platform, video ID); cross-platform uploads count independently",
            "release_date_filter": "VocaDB afterDate inclusive; beforeDate exclusive next day",
            "monthly_attribution": "each upload is assigned to its earliest matching song release month",
            "video_upload_dates": "metadata only; all linked uploads of matching songs qualify",
            "date_precedence": "platform timestamp when available, otherwise VocaDB PV date",
            "catalog_only": args.catalog_only, "partial": partial, "coverage": coverage,
            "catalog_cache_days": 7, "stats_cache_hours": args.stats_cache_hours,
            "niconico_snapshot_version": getattr(client, "nico_snapshot_version", None),
            "network_requests": dict(client.requests), "cache_hits": dict(client.hits),
            "issues": issues, "summary": summaries,
        }
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nPlatform       Videos        Known lifetime views   Missing views")
        for s in summaries:
            views = f"{s['known_lifetime_views']:,}" if s["known_lifetime_views"] is not None else "N/A"
            print(f"{s['platform']:<13} {s['videos']:>7,} {views:>27} {s['videos_missing_views']:>15,}")
        if args.list:
            print_list(rows)
        print(f"\nReports: {out.resolve()}")
        if partial:
            print("Partial results; see report.json for coverage and unavailable counts.", file=sys.stderr)
        return 2 if partial else 0
    finally:
        client.db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted. Successful responses remain cached; rerun the same command.", file=sys.stderr)
        raise SystemExit(130)
