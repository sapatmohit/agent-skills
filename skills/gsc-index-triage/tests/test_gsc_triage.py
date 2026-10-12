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


class Misc(unittest.TestCase):
    def test_slug_and_enc(self):
        self.assertEqual(g.slug("sc-domain:example.com"), "sc-domain-example-com")
        self.assertEqual(g.enc("https://example.com/"), "https%3A%2F%2Fexample.com%2F")

    def test_parser(self):
        a = g.build_parser().parse_args(["triage", "--quota", "5"])
        self.assertEqual((a.cmd, a.quota, a.cooldown), ("triage", 5, 14))


if __name__ == "__main__":
    unittest.main()
