"""Offline tests. Run: python3 -m unittest discover skills/gsc-index-triage/tests"""
import datetime as dt
import gzip
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import gsc_triage as g  # noqa: E402

URL = "https://example.com/blog/post/"
LOW = re.compile(g.DEFAULT_LOW_VALUE, re.I)
NOW = dt.date(2026, 10, 12)


class Classify(unittest.TestCase):
    def test_indexed(self):
        self.assertEqual(g.classify({"verdict": "PASS"}, URL), "indexed")

    def test_requestable_states(self):
        for cov in ("URL is unknown to Google", "Discovered - currently not indexed",
                    "Crawled - currently not indexed"):
            ir = {"verdict": "NEUTRAL", "coverageState": cov, "pageFetchState": "SUCCESSFUL"}
            self.assertEqual(g.classify(ir, URL), "request", cov)

    def test_fix_states(self):
        cases = [
            {"verdict": "NEUTRAL", "coverageState": "Page with redirect"},
            {"verdict": "FAIL", "coverageState": "Not found (404)", "pageFetchState": "NOT_FOUND"},
            {"verdict": "NEUTRAL", "coverageState": "Excluded by 'noindex' tag",
             "indexingState": "BLOCKED_BY_META_TAG"},
            {"verdict": "NEUTRAL", "robotsTxtState": "DISALLOWED"},
            {"verdict": "NEUTRAL", "coverageState": "Alternate page with proper canonical tag",
             "googleCanonical": "https://example.com/other/"},
        ]
        for ir in cases:
            self.assertEqual(g.classify(ir, URL), "fix", ir)
            self.assertTrue(g.fix_hint(ir, URL))

    def test_trailing_slash_canonical_is_not_a_fix(self):
        ir = {"verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed",
              "userCanonical": URL.rstrip("/")}
        self.assertEqual(g.classify(ir, URL), "request")


class Score(unittest.TestCase):
    def s(self, url=URL, cov="URL is unknown to Google", lastmod=None, perf=None, crawl=None):
        rec = {"coverageState": cov, "lastCrawlTime": crawl}
        return g.score(url, rec, lastmod, perf, LOW, NOW)[0]

    def test_unknown_beats_crawled(self):
        self.assertGreater(self.s(), self.s(cov="Crawled - currently not indexed"))

    def test_updated_after_crawl_boosts(self):
        stale = self.s(cov="Crawled - currently not indexed", crawl="2026-08-01T00:00:00Z",
                       lastmod="2026-07-01")
        fresh = self.s(cov="Crawled - currently not indexed", crawl="2026-08-01T00:00:00Z",
                       lastmod="2026-10-01")
        self.assertGreater(fresh, stale)

    def test_impressions_boost(self):
        self.assertGreater(self.s(perf={"impressions": 500, "position": 8}), self.s())

    def test_low_value_sinks(self):
        for path in ("/terms/", "/privacy", "/authors/jane/", "/tag/x/", "/blog/page/3/"):
            self.assertLess(self.s(url="https://example.com" + path), 0, path)
        self.assertGreater(self.s(url="https://example.com/termsheet-guide/"), 0)


class Sitemap(unittest.TestCase):
    URLSET = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc><lastmod>2026-10-01</lastmod></url>
  <url><loc> https://example.com/a </loc></url>
</urlset>"""
    INDEX = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
</sitemapindex>"""

    def test_urlset(self):
        urls, children = g.parse_sitemap(self.URLSET)
        self.assertEqual(urls, [("https://example.com/", "2026-10-01"), ("https://example.com/a", None)])
        self.assertEqual(children, [])

    def test_index_and_gzip(self):
        urls, children = g.parse_sitemap(gzip.compress(self.INDEX))
        self.assertEqual(urls, [])
        self.assertEqual(children, ["https://example.com/s1.xml"])


class RequestLog(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 13, 14, 0, tzinfo=dt.timezone.utc)

    def at(self, hours_ago):
        t = self.NOW - dt.timedelta(hours=hours_ago)
        return {"requested": str(t.date()), "requested_at": t.isoformat()}

    def test_quota_is_rolling_24h(self):
        log = {"a": self.at(25), "b": self.at(23), "c": self.at(1), "d": {"requested": "2026-10-13"}}
        q = g.quota_status(log, self.NOW, 2)
        self.assertEqual((q["used"], q["remaining"]), (2, 0))
        self.assertEqual(q["next_slot"], self.NOW + dt.timedelta(hours=1))

    def test_record_indexed_uses_crawl_date(self):
        log = {URL: {"requested": "2026-10-01", "verified": None}}
        rec = {"status": "indexed", "lastCrawlTime": "2026-10-03T08:00:00Z"}
        self.assertTrue(g.record_indexed(log, URL, rec, NOW))
        self.assertEqual(log[URL]["indexed_on"], "2026-10-03")
        self.assertFalse(g.record_indexed(log, URL, rec, NOW))  # already recorded

    def test_record_indexed_ignores_crawl_before_request(self):
        log = {URL: {"requested": "2026-10-05", "verified": None}}
        g.record_indexed(log, URL, {"status": "indexed", "lastCrawlTime": "2026-09-01T00:00:00Z"}, NOW)
        self.assertEqual(log[URL]["indexed_on"], str(NOW))

    def test_index_stats(self):
        log = {"a": {"requested": "2026-10-01", "indexed_on": "2026-10-03"},
               "b": {"requested": "2026-10-01", "indexed_on": "2026-10-11"},
               "c": {"requested": "2026-09-01"}, "d": {"requested": "2026-10-10"}}
        st = g.index_stats(log, NOW)
        self.assertEqual((st["indexed"], st["median_days"], st["within_week"], st["stuck"]),
                         (2, 6.0, 0.5, ["c"]))


class Links(unittest.TestCase):
    def test_norm_url(self):
        self.assertEqual(g.norm_url("https://Example.com/a/#x"), g.norm_url("http://example.com/a"))
        self.assertNotEqual(g.norm_url("https://example.com/a?p=1"), g.norm_url("https://example.com/a"))

    def test_page_links(self):
        body = b'<a href="/b/">b</a><a href="c">c</a><a href="#top">t</a><a href="mailto:x@y">m</a>'
        self.assertEqual(g.page_links(body, "https://example.com/a/"),
                         {"example.com/b", "example.com/a/c"})


class Demo(unittest.TestCase):
    """The --demo site doubles as an offline end-to-end test."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def run_cmd(self, *args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            g.main(["--demo", "--state", self.tmp, *args])
        return out.getvalue()

    def test_flow(self):
        self.assertIn("newly indexed since", self.run_cmd("inspect"))
        triage = self.run_cmd("triage")
        self.assertIn("2/10 manual requests used", triage)
        self.assertIn("react-i18n-guide", triage)
        self.assertIn("ORPHAN", self.run_cmd("links") + self.run_cmd("triage"))
        self.assertIn("median 2.5 days", self.run_cmd("stats", "--no-perf"))
        self.assertIn("3/10", self.run_cmd("mark", "https://demo.example/blog/react-i18n-guide/"))

    def test_fix_plan(self):
        self.run_cmd("inspect")
        _, snap = g.latest_snapshot(g.state_dir(g.DEMO_SITE, self.tmp))
        plan = {p["url"].replace(g.DEMO_BASE, ""): p for p in g.plan_fixes(g.DemoClient(), snap)}
        self.assertEqual(plan["/old-pricing/"]["action"], "remove")  # target already listed
        self.assertEqual(plan["/blog/i18n-checklist-2025/"]["target"], g.DEMO_BASE + "/blog/i18n-checklist/")
        self.assertEqual(plan["/blog/deleted-post/"]["action"], "remove")

    def test_indexnow(self):
        self.run_cmd("inspect")
        self.assertIn("IndexNow key", self.run_cmd("indexnow"))
        self.assertIn("URLs submitted", self.run_cmd("indexnow"))
        self.assertIn("nothing new", self.run_cmd("indexnow", "--changed"))


class Misc(unittest.TestCase):
    def test_slug_and_enc(self):
        self.assertEqual(g.slug("sc-domain:example.com"), "sc-domain-example-com")
        self.assertEqual(g.enc("https://example.com/"), "https%3A%2F%2Fexample.com%2F")

    def test_parser(self):
        a = g.build_parser().parse_args(["triage", "--quota", "5"])
        self.assertEqual((a.cmd, a.quota, a.cooldown), ("triage", 5, 14))
        self.assertIsNone(g.build_parser().parse_args(["triage"]).quota)


if __name__ == "__main__":
    unittest.main()
