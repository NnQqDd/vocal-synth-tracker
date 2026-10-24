"""Offline tests: python -B -m unittest -v test_popularity_tracker.py"""
import contextlib
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import popularity_tracker as t


def options(**overrides):
    data = dict(cache=":memory:", refresh=False, max_vocadb_requests=30,
                max_platform_requests=20, delay=0, stats_cache_hours=6,
                catalog_only=False, original_only=False,
                start=t.date(2025, 1, 1), end=t.date(2025, 12, 31))
    data.update(overrides)
    return SimpleNamespace(**data)


def artist(aid, name, kind="Vocaloid", parent=None):
    a = dict(id=aid, name=name, artistType=kind, names=[{"value": name}])
    if parent:
        a["baseVoicebank"] = {"id": parent}
    return a


ARTISTS = {1: artist(1, "Hatsune Miku"), 10: artist(10, "Miku V4X", parent=1),
           116: artist(116, "Kasane Teto", "UTAU", 140308),
           20: artist(20, "Teto SV", "SynthesizerV", 140308),
           140308: artist(140308, "Kasane Teto (Unknown)", "OtherVoiceSynthesizer")}


def credit(aid, category="Vocalist"):
    return {"artist": ARTISTS[aid], "categories": category, "isSupport": False}


def pv(service, vid, day="2025-01-01", kind="Original"):
    return {"service": service, "pvId": vid, "publishDate": day,
            "pvType": kind, "url": f"https://example.invalid/{vid}"}


def song(sid, name, credits, pvs, day="2025-01-01"):
    return {"id": sid, "name": name, "artists": credits, "pvs": pvs, "publishDate": day}


def row(platform="youtube", vid="abcdefghijk", views=None, name="Song A"):
    return dict(platform=platform, video_id=vid, url=f"https://example.invalid/{vid}",
                title=name, song_ids={1}, song_names={name}, pv_types={"Original"},
                song_release_dates={"2025-01-01T00:00:00+00:00"},
                pv_dates={"2025-01-01T00:00:00+00:00"}, views=views,
                published_at="", date_source="", stats_fetched_at="",
                status="not_fetched", catalog_fetched_at="", disabled_in_vocadb=False)


class FixtureClient:
    def __init__(self):
        self.artist_roots = {}
        self.calls = []
        self.songs = [
            song(100, "Song A", [credit(10), credit(20)],
                 [pv("Youtube", "abcdefghijk"), pv("Youtube", "abcdefghijk"),
                  pv("NicoNicoDouga", "sm9"), pv("Bilibili", "av170001"),
                  pv("SoundCloud", "123")]),
            song(200, "Solo", [credit(1)], [pv("Youtube", "solosolosol")]),
            song(300, "Song B", [credit(1), credit(116)], [pv("Youtube", "xxxxxxxxxxx")]),
        ]

    def get(self, url, params=None, **kwargs):
        params = params or {}
        self.calls.append((url, params, kwargs))
        if "/api/artists/" in url:
            return ARTISTS[int(url.rsplit("/", 1)[1])], ""
        if url.endswith("/api/artists"):
            aid = 116 if params["query"] == "Kasane Teto" else 1
            return {"items": [ARTISTS[aid]]}, ""
        if url.endswith("/api/songs"):
            return {"items": self.songs, "totalCount": len(self.songs)}, "2026-01-01T00:00:00Z"
        if "googleapis.com" in url:
            return {"items": [{"id": "abcdefghijk", "statistics": {"viewCount": "100"},
                    "snippet": {"publishedAt": "2025-01-01T12:00:00Z"}}]}, "2026-01-01T00:00:00Z"
        if url.endswith("/version"):
            return {"last_modified": "2026-01-01T05:00:00+09:00"}, ""
        if "nicovideo.jp" in url:
            return {"data": [{"contentId": "sm9", "viewCounter": 500,
                             "startTime": "2025-01-01T12:00:00+09:00"}]}, "2026-01-01T00:00:00Z"
        if "api.bilibili.com" in url:
            return {"data": {"bvid": "BV17x411w7KC", "stat": {"view": 50},
                             "pubdate": 1735732800}}, "2026-01-01T00:00:00Z"
        raise AssertionError(url)


class TrackerTests(unittest.TestCase):
    def test_case_and_alias_normalization(self):
        c = FixtureClient()
        vocals = t.resolve_vocals(c, ["teto", "TETO", "kasane Teto", "Miku", "HATSUNE MIKU"])
        self.assertEqual([v["id"] for v in vocals], [1, 140308])
        searches = [p for u, p, k in c.calls if u.endswith("/api/artists")]
        self.assertEqual(len(searches), 2)

    def test_unicode_and_spacing_normalization(self):
        self.assertEqual(t.normalize_name(" Kasane-TETO "), t.normalize_name("kasane teto"))
        self.assertEqual(t.normalize_name("TETO"), t.normalize_name("ＴＥＴＯ"))

    def test_id_and_url_inputs_resolve_same_family(self):
        vocals = t.resolve_vocals(FixtureClient(), ["id:116", "https://vocadb.net/Ar/20", "140308"])
        self.assertEqual([v["id"] for v in vocals], [140308])

    def test_unknown_or_ambiguous_name_is_an_error(self):
        class Empty(FixtureClient):
            def get(self, url, params=None, **kw):
                if url.endswith("/api/artists"):
                    return {"items": []}, ""
                return super().get(url, params, **kw)
        with self.assertRaises(t.ApiError):
            t.resolve_vocals(Empty(), ["not-a-vocal"])
        class Ambiguous(FixtureClient):
            def get(self, url, params=None, **kw):
                if url.endswith("/api/artists"):
                    return {"items": [artist(1, "Shared"), artist(140308, "Shared", "UTAU")]}, ""
                return super().get(url, params, **kw)
        with self.assertRaisesRegex(t.ApiError, "Ambiguous"):
            t.resolve_vocals(Ambiguous(), ["Shared"])

    def test_non_vocal_synth_id_is_rejected(self):
        class Producer(FixtureClient):
            def get(self, url, params=None, **kw):
                return artist(9, "Producer", "Producer"), ""
        with self.assertRaisesRegex(t.ApiError, "not a vocal synth"):
            t.root_artist(Producer(), 9)

    def test_song_and_credits_allow_additional_vocals(self):
        c = FixtureClient()
        both = song(1, "Duet", [credit(10), credit(20), credit(1)], [])
        self.assertTrue(t.song_matches(c, both, {1, 140308}))
        self.assertFalse(t.song_matches(c, song(2, "Solo", [credit(1)], []), {1, 140308}))

    def test_non_vocal_credit_cannot_satisfy_and(self):
        s = song(1, "Visual cameo", [credit(1), credit(116, "Other")], [])
        self.assertFalse(t.song_matches(FixtureClient(), s, {1, 140308}))


    def test_large_medley_prioritizes_requested_families(self):
        c = FixtureClient()
        unrelated = [{"artist": artist(i, f"Unrelated synth {i}", "UTAU"),
                      "categories": "Vocalist"} for i in range(1000, 1300)]
        miku = {"artist": artist(10, "Hatsune Miku V4X"), "categories": "Vocalist"}
        teto = {"artist": artist(20, "Kasane Teto SV", "SynthesizerV"), "categories": "Vocalist"}
        c.songs = [song(1, "Huge medley", unrelated + [miku, teto], [pv("Youtube", "abcdefghijk")])]
        records = {}
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, [], {})
        self.assertEqual(len(records), 1)
        lookups = [u for u, p, k in c.calls if "/api/artists/" in u]
        self.assertEqual(len(lookups), 2)

    def test_and_is_checked_per_song_not_merged_video(self):
        c = FixtureClient()
        c.songs = [song(1, "Miku solo", [credit(1)], [pv("Youtube", "abcdefghijk")]),
                   song(2, "Teto solo", [credit(116)], [pv("Youtube", "abcdefghijk")])]
        records, issues, coverage = {}, [], {}
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, issues, coverage)
        self.assertEqual(records, {})
        self.assertEqual(coverage["matching_songs"], 0)

    def test_catalog_sends_song_release_date_filter(self):
        c, records, coverage = FixtureClient(), {}, {}
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, [], coverage)
        params = next(p for u, p, k in c.calls if u.endswith("/api/songs"))
        self.assertEqual(params["artistId[]"], [1, 140308])
        self.assertEqual(params["childVoicebanks"], "true")
        self.assertEqual(params["afterDate"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(params["beforeDate"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(len(records), 4)
        self.assertTrue(coverage["finished"])

    def test_pagination_uses_returned_page_size(self):
        class Paged(FixtureClient):
            def get(self, url, params=None, **kw):
                if url.endswith("/api/songs"):
                    self.calls.append((url, params, kw))
                    start = params["start"]
                    items = [self.songs[0]] if start == 0 else [self.songs[2]]
                    return {"items": items, "totalCount": 2}, ""
                return super().get(url, params, **kw)
        c = Paged()
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), {}, [], {})
        self.assertEqual([p["start"] for u, p, k in c.calls if u.endswith("/api/songs")], [0, 1])

    def test_repeated_page_is_partial(self):
        class Repeated(FixtureClient):
            def get(self, url, params=None, **kw):
                if url.endswith("/api/songs"):
                    return {"items": [self.songs[0]], "totalCount": 10}, ""
                return super().get(url, params, **kw)
        coverage = {}
        with self.assertRaisesRegex(t.ApiError, "repeated"):
            t.collect_catalog(Repeated(), [ARTISTS[1], ARTISTS[140308]], options(), {}, [], coverage)
        self.assertFalse(coverage["finished"])

    def test_platforms_independent_repeated_links_deduped(self):
        records = {}
        s = song(1, "Song", [], [pv("Youtube", "abcdefghijk"), pv("Youtube", "abcdefghijk"),
                  pv("NicoNicoDouga", "sm9"), pv("Bilibili", "av170001")])
        t.add_song(records, s, "", options(), [])
        self.assertEqual(len(records), 3)

    def test_original_only_excludes_reprints(self):
        records = {}
        t.add_song(records, song(1, "Song", [], [pv("Youtube", "abcdefghijk", kind="Reprint")]),
                   "", options(original_only=True), [])
        self.assertFalse(records)

    def test_song_release_boundaries_are_inclusive(self):
        c = FixtureClient()
        credits = [credit(1), credit(116)]
        c.songs = [
            song(1, "Before", credits, [pv("Youtube", "aaaaaaaaaaa")], "2024-12-31T23:59:59Z"),
            song(2, "First", credits, [pv("Youtube", "bbbbbbbbbbb")], "2025-01-01T00:00:00Z"),
            song(3, "Last", credits, [pv("Youtube", "ccccccccccc")], "2025-12-31T23:59:59Z"),
            song(4, "After", credits, [pv("Youtube", "ddddddddddd")], "2026-01-01T00:00:00Z"),
        ]
        records, coverage = {}, {}
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, [], coverage)
        self.assertEqual({r["video_id"] for r in records.values()}, {"bbbbbbbbbbb", "ccccccccccc"})
        self.assertEqual(coverage["matching_songs"], 2)

    def test_upload_dates_do_not_exclude_matching_song(self):
        c = FixtureClient()
        c.songs = [song(1, "Released in range", [credit(1), credit(116)],
                       [pv("Youtube", "aaaaaaaaaaa", "2007-01-01"),
                        pv("NicoNicoDouga", "sm9", "2026-01-01"),
                        pv("Bilibili", "av170001", None)], "2025-12-31")]
        records = {}
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, [], {})
        rows = t.prepare_rows(t.merge_records(records))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["period"] == "2025-12" for r in rows))

    def test_unknown_song_release_is_excluded(self):
        c = FixtureClient()
        c.songs = [song(1, "Undated", [credit(1), credit(116)],
                       [pv("Youtube", "aaaaaaaaaaa")], None)]
        records, issues = {}, []
        t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), records, issues, {})
        self.assertFalse(records)
        self.assertTrue(any("no release date" in issue for issue in issues))

    def test_list_descending_views_zero_before_unknown_and_sanitized(self):
        rows = [row(views=None, name="Unknown"), row(views=0, name="Zero"),
                row("niconico", "sm9", 999, "Big\x1b\nSong"),
                row("youtube", "xxxxxxxxxxx", 100, "Small")]
        stream = io.StringIO()
        t.print_list(rows, stream)
        lines = stream.getvalue().splitlines()
        self.assertTrue(lines[2].startswith("999 | niconico"))
        self.assertTrue(lines[3].startswith("100 | youtube"))
        self.assertTrue(lines[4].startswith("0 | youtube"))
        self.assertTrue(lines[5].startswith("N/A | youtube"))
        self.assertNotIn("\x1b", stream.getvalue())

    def test_missing_counts_never_become_zero(self):
        self.assertIsNone(t.count(None))
        self.assertIsNone(t.count("hidden"))
        self.assertIsNone(t.count(True))
        self.assertEqual(t.count("0"), 0)
        total = t.summarize([row(views=100), row(vid="xxxxxxxxxxx")], {"finished": True})[-1]
        self.assertEqual((total["videos"], total["known_lifetime_views"], total["videos_missing_views"]), (2, 100, 1))

    def test_bilibili_alias_merge_after_enrichment(self):
        a, b = row("bilibili", "BV17x411w7KC", 100), row("bilibili", "BV17x411w7KC")
        b["song_ids"] = {2}
        merged = t.merge_records({1:a, 2:b})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["song_ids"], {1, 2})
        self.assertEqual(merged[0]["views"], 100)

    def test_bilibili_access_error_stays_unknown(self):
        class Blocked(FixtureClient):
            def get(self, *a, **kw):
                raise t.ApiError("bilibili: HTTP 412")
        r, issues = row("bilibili", "av170001"), []
        t.enrich(Blocked(), {1:r}, options(), issues)
        self.assertIsNone(r["views"])
        self.assertIn("bilibili: HTTP 412", issues)

    def test_repeated_artist_ids_encoded_and_cached(self):
        c = t.Client(options(max_vocadb_requests=1))
        seen = []
        class Opener:
            def open(self, request, **kw):
                seen.append(request.full_url)
                return io.BytesIO(b'{"items":[]}')
        c.opener = Opener()
        try:
            params = {"artistId[]": [1, 140308]}
            c.get("https://vocadb.net/api/songs", params, ttl=3600)
            c.get("https://vocadb.net/api/songs", params, ttl=3600)
            self.assertEqual(len(seen), 1)
            self.assertEqual(parse_qs(urlsplit(seen[0]).query)["artistId[]"], ["1", "140308"])
            with self.assertRaises(t.ApiError):
                c.get("https://vocadb.net/api/other")
        finally:
            c.db.close()

    def test_cli_accepts_lists_repeated_flags_and_data_alias(self):
        args = t.parser().parse_args(["--vocal", "miku", "teto", "--vocal", "GUMI",
                                      "--start", "2025-01-01", "--data", "2025-12-31", "--list"])
        self.assertEqual(args.vocal, ["miku", "teto", "GUMI"])
        self.assertTrue(args.list)

    def test_count_only_prints_one_line_and_skips_scan_views_and_reports(self):
        fake, output, errors = FixtureClient(), io.StringIO(), io.StringIO()
        with patch.object(t.Client, "get", side_effect=fake.get), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "mkdir") as mkdir, \
             patch.object(Path, "write_text") as write, \
             patch.object(t, "collect_catalog") as scan, \
             patch.object(t, "enrich") as enrich, \
             patch.object(t, "csv_write") as csv, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = t.main(["--vocal", "miku", "teto", "--start", "2025-01-01",
                             "--end", "2025-12-31", "--count",
                             "--cache", ":memory:", "--out", "already-existing"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "3 candidate songs\n")
        self.assertEqual(errors.getvalue(), "")
        for step in (mkdir, write, scan, enrich, csv):
            step.assert_not_called()
        queries = [p for u, p, k in fake.calls if u.endswith("/api/songs")]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["maxResults"], 0)
        self.assertEqual(queries[0]["fields"], "None")
        self.assertEqual(queries[0]["artistId[]"], [1, 140308])
        self.assertEqual(queries[0]["afterDate"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(queries[0]["beforeDate"], "2026-01-01T00:00:00+00:00")

    def test_count_and_list_are_mutually_exclusive_before_any_work(self):
        for flags in (["--count", "--list"], ["--list", "--count"]):
            with self.subTest(flags=flags):
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(t, "Client") as client, \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                     self.assertRaises(SystemExit) as raised:
                    t.main(["--vocal", "miku", "--start", "2025-01-01",
                            "--end", "2025-12-31", *flags])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("not allowed with argument", errors.getvalue())
                self.assertIn("--count", errors.getvalue())
                self.assertIn("--list", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                client.assert_not_called()

    def test_count_only_handles_zero_and_invalid_counts(self):
        for value, expected_code, expected_output in [
                (0, 0, "0 candidate songs\n"), (None, 2, "")]:
            with self.subTest(total=value):
                fake, output, errors = FixtureClient(), io.StringIO(), io.StringIO()
                def get(url, params=None, **kw):
                    if url.endswith("/api/songs"):
                        return {"items": [], "totalCount": value}, ""
                    return fake.get(url, params, **kw)
                with patch.object(t.Client, "get", side_effect=get), \
                     patch.object(Path, "mkdir") as mkdir, \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    result = t.main(["--vocal", "miku", "--start", "2025-01-01",
                                     "--end", "2025-12-31", "--count", "--cache", ":memory:"])
                self.assertEqual(result, expected_code)
                self.assertEqual(output.getvalue(), expected_output)
                self.assertEqual(bool(errors.getvalue()), expected_code != 0)
                mkdir.assert_not_called()

    def test_normal_run_candidate_message_is_one_short_line(self):
        c, output, errors = FixtureClient(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            t.collect_catalog(c, [ARTISTS[1], ARTISTS[140308]], options(), {}, [], {})
        self.assertEqual(output.getvalue(), "3 candidate songs\n")
        self.assertEqual(errors.getvalue(), "")

    def test_end_to_end_totals_and_list_without_disk_or_network(self):
        fake, output, reports = FixtureClient(), io.StringIO(), {}
        def csv_capture(path, rows, fields):
            reports[path.name] = list(rows)
        with patch.object(t.Client, "get", side_effect=fake.get), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "mkdir"), patch.object(Path, "write_text") as write_json, \
             patch.object(t, "csv_write", side_effect=csv_capture), \
             patch.dict("os.environ", {"YOUTUBE_API_KEY": "test-not-a-key"}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = t.main(["--vocal", "TETO", "kasane Teto", "miku",
                             "--start", "2025-01-01", "--end", "2025-12-31",
                             "--list", "--cache", ":memory:", "--delay", "0", "--out", "virtual-report"])
        self.assertTrue(output.getvalue().startswith("3 candidate songs\n"))
        self.assertNotIn("Song release dates:", output.getvalue())
        self.assertNotIn("Views: observed lifetime counts", output.getvalue())
        self.assertNotIn("Required vocal credits", output.getvalue())
        report = json.loads(write_json.call_args.args[0])
        self.assertEqual(result, 2)  # One video is unavailable on YouTube.
        self.assertEqual([v["id"] for v in report["vocals_resolved"]], [1, 140308])
        self.assertEqual(report["summary"][-1]["videos"], 4)
        self.assertEqual(report["summary"][-1]["known_lifetime_views"], 650)
        self.assertEqual(report["summary"][-1]["videos_missing_views"], 1)
        self.assertEqual(report["date_basis"], "song_release_utc")
        self.assertNotIn("unknown_dates.csv", reports)
        self.assertEqual([r["views"] for r in reports["videos.csv"]], [500, 100, 50, None])
        listing = output.getvalue().split("Views | Platform | Song | Video URL", 1)[1]
        self.assertLess(listing.index("500 |"), listing.index("100 |"))
        self.assertLess(listing.index("100 |"), listing.index("50 |"))
        self.assertLess(listing.index("50 |"), listing.index("N/A |"))


if __name__ == "__main__":
    unittest.main()
