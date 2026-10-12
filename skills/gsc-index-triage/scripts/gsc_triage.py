#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["google-auth>=2.20", "requests>=2.28"]
# ///
"""gsc-triage: find which of your pages Google hasn't indexed, decide which
ones are worth a manual "Request indexing" today, and check back later.

Commands
  sites                     properties this credential can see
  inspect                   URL-inspect every sitemap URL, save a snapshot
  triage                    rank not-indexed URLs: request today / fix / skip
  mark URL [URL ...]        record URLs you requested in the Search Console UI
  verify                    re-inspect requested URLs, report which got indexed
  report                    clicks/impressions vs previous period, striking-distance queries
  sitemaps [--submit]       list (or resubmit) the property's sitemaps

Auth: a service-account key via --key or GSC_KEY, otherwise Application
Default Credentials. Property via --site or GSC_SITE (auto-picked when the
credential only sees one).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import os
import pathlib
import re
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

SCOPES_RO = ["https://www.googleapis.com/auth/webmasters.readonly"]
SCOPES_RW = ["https://www.googleapis.com/auth/webmasters"]
WM = "https://www.googleapis.com/webmasters/v3"
INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
INSPECT_DAILY_QUOTA = 2000  # per property, per day

# Pages that rarely deserve one of the few manual requests per day.
DEFAULT_LOW_VALUE = (
    r"/(terms|privacy|legal|cookies?|imprint|impressum|disclaimer|gdpr|"
    r"login|log-in|signin|sign-in|signup|sign-up|register|logout|account|cart|checkout|"
    r"tags?|categor(y|ies)|authors?|team|careers|jobs|press|thank-?you|search)(/|$)"
    r"|/page/\d+/?$|[?&](page|p)=\d+"
)


# ---------------------------------------------------------------- utilities

def today() -> dt.date:
    return dt.date.today()


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def tilde(p) -> str:
    s, home = str(p), str(pathlib.Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def slug(site: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", site.lower()).strip("-")


def state_dir(site: str, override: str | None) -> pathlib.Path:
    base = override or os.environ.get("GSC_TRIAGE_STATE")
    if base:
        root = pathlib.Path(base).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local/state")
        root = pathlib.Path(xdg) / "gsc-triage"
    d = root / slug(site)
    (d / "snapshots").mkdir(parents=True, exist_ok=True)
    return d


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------- auth

class Client:
    """Thin REST client. One AuthorizedSession per thread."""

    def __init__(self, key: str | None, write: bool = False):
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError:
            die("missing dependencies; run with `uv run gsc_triage.py ...` "
                "or `pip install google-auth requests`")
        scopes = SCOPES_RW if write else SCOPES_RO
        key = key or os.environ.get("GSC_KEY")
        if key:
            path = pathlib.Path(key).expanduser()
            if not path.exists():
                die(f"key file not found: {path}")
            self._creds = service_account.Credentials.from_service_account_file(
                str(path), scopes=scopes)
        else:
            try:
                self._creds, _ = google.auth.default(scopes=scopes)
            except Exception as e:  # DefaultCredentialsError
                die(f"no credentials ({e}). Pass --key or set GSC_KEY; "
                    "see the skill's setup section.")
        self._session_cls = AuthorizedSession
        self._local = threading.local()

    @property
    def session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = self._session_cls(self._creds)
        return s

    def call(self, method: str, url: str, body=None, retries: int = 4):
        for attempt in range(retries + 1):
            r = self.session.request(method, url, json=body, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(30, 2 ** attempt + 0.5 * attempt))
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json()["error"]["message"]
                except Exception:
                    msg = r.text[:300]
                raise ApiError(r.status_code, msg)
            return r.json() if r.content else {}


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def enc(site: str) -> str:
    return urllib.parse.quote(site, safe="")


def resolve_site(client: Client, site: str | None) -> str:
    site = site or os.environ.get("GSC_SITE")
    if site:
        return site
    entries = client.call("GET", f"{WM}/sites").get("siteEntry", [])
    usable = [e["siteUrl"] for e in entries if e.get("permissionLevel") != "siteUnverifiedUser"]
    if len(usable) == 1:
        return usable[0]
    if not usable:
        die("this credential sees no Search Console properties. Add its email as a user "
            "(Settings > Users and permissions) and try again.")
    die("several properties visible, pick one with --site:\n  " + "\n  ".join(usable))


# ----------------------------------------------------------------- sitemaps

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml_bytes: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return ([(url, lastmod)], [child sitemap urls])."""
    if xml_bytes[:2] == b"\x1f\x8b":
        xml_bytes = gzip.decompress(xml_bytes)
    root = ET.fromstring(xml_bytes)
    urls, children = [], []
    kind = _local(root.tag)
    for node in root:
        loc = lastmod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip() or None
        if not loc:
            continue
        if kind == "sitemapindex":
            children.append(loc)
        else:
            urls.append((loc, lastmod))
    return urls, children


def fetch_sitemaps(roots: list[str]) -> dict[str, str | None]:
    import requests

    seen, out, queue = set(), {}, list(roots)
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = requests.get(sm, timeout=30, headers={"User-Agent": "gsc-triage"})
            r.raise_for_status()
            urls, children = parse_sitemap(r.content)
        except Exception as e:
            print(f"warning: skipping sitemap {sm}: {e}", file=sys.stderr)
            continue
        queue.extend(children)
        for u, lm in urls:
            out.setdefault(u, lm)
    return out


def property_sitemaps(client: Client, site: str) -> list[str]:
    data = client.call("GET", f"{WM}/sites/{enc(site)}/sitemaps")
    # Index sitemaps are followed by fetch_sitemaps; duplicates are de-duped there.
    return [s["path"] for s in data.get("sitemap", [])]


# --------------------------------------------------------------- inspection

def classify(ir: dict, url: str) -> str:
    """indexed | request | fix. `ir` is an indexStatusResult."""
    if ir.get("verdict") == "PASS":
        return "indexed"
    coverage = (ir.get("coverageState") or "").lower()
    fetch = ir.get("pageFetchState") or ""
    if ir.get("robotsTxtState") == "DISALLOWED":
        return "fix"
    if (ir.get("indexingState") or "").startswith("BLOCKED"):
        return "fix"
    if fetch and fetch not in ("SUCCESSFUL", "PAGE_FETCH_STATE_UNSPECIFIED"):
        return "fix"
    gc, uc = ir.get("googleCanonical"), ir.get("userCanonical")
    if gc and gc.rstrip("/") != url.rstrip("/"):
        return "fix"
    if uc and uc.rstrip("/") != url.rstrip("/"):
        return "fix"
    if any(k in coverage for k in ("redirect", "404", "duplicate", "alternate", "noindex",
                                   "blocked", "soft", "server error", "excluded")):
        return "fix"
    return "request"


FIX_HINTS = {
    "redirect": "sitemap lists a redirecting URL; list the final URL instead",
    "404": "page is gone; remove it from the sitemap or restore it",
    "noindex": "page has noindex; remove it from the sitemap, or drop the noindex if it should rank",
    "robots": "robots.txt blocks it; unblock or remove from the sitemap",
    "canonical": "canonical points elsewhere; list the canonical URL in the sitemap",
    "duplicate": "Google treats it as a duplicate; set a canonical or consolidate content",
    "soft": "soft 404: page looks empty to Google; add real content or return 404",
    "server": "server error when Google fetched it; check logs and uptime",
}


def fix_hint(ir: dict, url: str) -> str:
    c = (ir.get("coverageState") or "").lower()
    if ir.get("robotsTxtState") == "DISALLOWED" or "robots" in c:
        return FIX_HINTS["robots"]
    if (ir.get("indexingState") or "").startswith("BLOCKED") or "noindex" in c:
        return FIX_HINTS["noindex"]
    for key in ("redirect", "404", "soft", "server", "duplicate"):
        if key in c:
            return FIX_HINTS[key]
    fetch = ir.get("pageFetchState") or ""
    if fetch == "NOT_FOUND":
        return FIX_HINTS["404"]
    if fetch == "SOFT_404":
        return FIX_HINTS["soft"]
    if fetch in ("REDIRECT_ERROR",):
        return FIX_HINTS["redirect"]
    if fetch == "SERVER_ERROR":
        return FIX_HINTS["server"]
    return FIX_HINTS["canonical"]


def inspect_url(client: Client, site: str, url: str) -> dict:
    try:
        res = client.call("POST", INSPECT, {"inspectionUrl": url, "siteUrl": site})
        ir = res.get("inspectionResult", {}).get("indexStatusResult", {})
        keep = ("verdict", "coverageState", "robotsTxtState", "indexingState",
                "pageFetchState", "lastCrawlTime", "googleCanonical", "userCanonical")
        rec = {k: ir[k] for k in keep if k in ir}
        rec["status"] = classify(ir, url)
        if rec["status"] == "fix":
            rec["hint"] = fix_hint(ir, url)
        return rec
    except ApiError as e:
        return {"status": "error", "error": str(e)}


def inspect_many(client, site, urls, workers=8, progress=True) -> dict[str, dict]:
    out, done = {}, 0
    with ThreadPoolExecutor(workers) as pool:
        for url, rec in zip(urls, pool.map(lambda u: inspect_url(client, site, u), urls)):
            out[url] = rec
            done += 1
            if progress and sys.stderr.isatty():
                print(f"\r  inspected {done}/{len(urls)}", end="", file=sys.stderr, flush=True)
    if progress and sys.stderr.isatty():
        print(file=sys.stderr)
    return out


def latest_snapshot(sd: pathlib.Path) -> tuple[str | None, dict]:
    snaps = sorted((sd / "snapshots").glob("*.json"))
    if not snaps:
        return None, {}
    return snaps[-1].stem, load_json(snaps[-1], {})


# ------------------------------------------------------------------ scoring

def page_impressions(client: Client, site: str, days: int = 90) -> dict[str, dict]:
    end = today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=days - 1)
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["page"],
            "rowLimit": 25000}
    rows = client.call("POST", f"{WM}/sites/{enc(site)}/searchAnalytics/query", body).get("rows", [])
    return {r["keys"][0]: r for r in rows}


def score(url: str, rec: dict, lastmod: str | None, perf: dict | None,
          low_value: re.Pattern, now: dt.date) -> tuple[float, list[str]]:
    reasons, s = [], 0.0
    cov = (rec.get("coverageState") or "").lower()
    crawled = parse_date(rec.get("lastCrawlTime"))
    lm = parse_date(lastmod)

    if "unknown" in cov:
        s += 40; reasons.append("Google has never seen it")
    elif "discovered" in cov:
        s += 35; reasons.append("discovered but never crawled")
    elif "crawled" in cov:
        s += 15; reasons.append("crawled but not indexed (quality call)")
        if lm and crawled and lm > crawled:
            s += 25; reasons.append(f"updated {lm} after last crawl {crawled}")
    else:
        s += 20

    if perf and perf.get("impressions", 0) > 0:
        imp = perf["impressions"]
        s += min(30.0, 10 * math.log10(1 + imp))
        reasons.append(f"{imp:.0f} impressions / 90d, pos {perf.get('position', 0):.1f}")

    if lm:
        age = (now - lm).days
        if age <= 14:
            s += 15; reasons.append(f"fresh (lastmod {lm})")
        elif age <= 60:
            s += 8

    path = urllib.parse.urlsplit(url).path
    depth = len([p for p in path.split("/") if p])
    s += max(0, 4 - depth)

    if low_value.search(url):
        s -= 50; reasons.append("low-value page type")
    return round(s, 1), reasons


# ----------------------------------------------------------------- commands

def cmd_sites(a, client):
    for e in client.call("GET", f"{WM}/sites").get("siteEntry", []):
        print(f"{e.get('permissionLevel', '?'):<20} {e['siteUrl']}")


def cmd_inspect(a, client, site, sd):
    roots = a.sitemap or property_sitemaps(client, site)
    if not roots and not a.urls_file:
        die("no sitemaps submitted for this property; pass --sitemap URL or --urls-file")
    urls: dict[str, str | None] = fetch_sitemaps(roots) if roots else {}
    if a.urls_file:
        for line in pathlib.Path(a.urls_file).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                urls.setdefault(line.strip(), None)

    prev_name, prev = latest_snapshot(sd)
    todo = list(urls)
    if a.skip_indexed and prev:
        todo = [u for u in todo if prev.get("urls", {}).get(u, {}).get("status") != "indexed"]
    if a.max:
        todo = todo[: a.max]
    if len(todo) > INSPECT_DAILY_QUOTA:
        print(f"warning: {len(todo)} URLs exceeds the {INSPECT_DAILY_QUOTA}/day inspection quota; "
              f"inspecting the first {INSPECT_DAILY_QUOTA}. Use --skip-indexed or --max.",
              file=sys.stderr)
        todo = todo[:INSPECT_DAILY_QUOTA]

    print(f"{site}: {len(urls)} URLs from {len(roots)} sitemap(s); inspecting {len(todo)}")
    results = inspect_many(client, site, todo, workers=a.workers)
    # Carry forward unchanged results for URLs skipped this run.
    merged = {u: dict(prev.get("urls", {}).get(u, {}), carried=True)
              for u in urls if u not in results and u in prev.get("urls", {})}
    for u, rec in results.items():
        rec["lastmod"] = urls.get(u)
        merged[u] = rec
    snap = {"site": site, "date": str(today()), "sitemaps": roots, "urls": merged}
    save_json(sd / "snapshots" / f"{today()}.json", snap)
    summarize(snap, prev if prev_name != str(today()) else {})
    print(f"\nsnapshot: {tilde(sd / 'snapshots' / (str(today()) + '.json'))}")
    print("next: `triage` to pick today's indexing requests")


def summarize(snap: dict, prev: dict) -> None:
    counts: dict[str, int] = {}
    for rec in snap["urls"].values():
        counts[rec.get("status", "?")] = counts.get(rec.get("status", "?"), 0) + 1
    total = len(snap["urls"])
    parts = [f"{counts.get(k, 0)} {k}" for k in ("indexed", "request", "fix", "error")]
    print(f"{total} URLs: " + ", ".join(parts))
    if prev:
        pu = prev.get("urls", {})
        newly = [u for u, r in snap["urls"].items()
                 if r.get("status") == "indexed" and pu.get(u, {}).get("status") not in (None, "indexed")]
        lost = [u for u, r in snap["urls"].items()
                if r.get("status") != "indexed" and pu.get(u, {}).get("status") == "indexed"]
        if newly:
            print(f"newly indexed since {prev.get('date')}: {len(newly)}")
            for u in newly:
                print(f"  + {u}")
        if lost:
            print(f"dropped out of the index since {prev.get('date')}: {len(lost)}")
            for u in lost:
                print(f"  - {u}  ({snap['urls'][u].get('coverageState', '?')})")


def cmd_triage(a, client, site, sd):
    name, snap = latest_snapshot(sd)
    if not snap:
        die("no snapshot yet; run `inspect` first")
    age = (today() - dt.date.fromisoformat(name)).days
    if age > 7:
        print(f"note: snapshot is {age} days old; consider re-running `inspect`", file=sys.stderr)
    requests_log = load_json(sd / "requests.json", {})
    perf = {} if a.no_perf else page_impressions(client, site)
    low = re.compile(a.low_value or DEFAULT_LOW_VALUE, re.I)
    now = today()

    ranked, fixes, waiting = [], [], []
    for url, rec in snap["urls"].items():
        st = rec.get("status")
        if st == "fix":
            fixes.append((url, rec))
        elif st == "request":
            req = requests_log.get(url)
            if req and (now - dt.date.fromisoformat(req["requested"])).days < a.cooldown:
                waiting.append((url, req["requested"]))
                continue
            sc, why = score(url, rec, rec.get("lastmod"), perf.get(url), low, now)
            ranked.append((sc, url, why))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    picks = [r for r in ranked if r[0] > 0][: a.quota]
    rest = [r for r in ranked if r not in picks]

    if a.json:
        print(json.dumps({
            "snapshot": name, "request_today": [{"url": u, "score": s, "why": w} for s, u, w in picks],
            "later": [{"url": u, "score": s, "why": w} for s, u, w in rest],
            "fix_not_request": [{"url": u, "coverage": r.get("coverageState"), "hint": r.get("hint")}
                                for u, r in fixes],
            "waiting": [{"url": u, "requested": d} for u, d in waiting]}, indent=2))
        return

    print(f"Snapshot {name}. Request indexing for these today "
          f"(Search Console > URL inspection > Request indexing):\n")
    for i, (s, u, why) in enumerate(picks, 1):
        print(f"{i:>2}. {u}\n      score {s}: {'; '.join(why)}")
    if not picks:
        print("  nothing worth a manual request right now")
    if fixes:
        print(f"\nFix, don't request ({len(fixes)}): requesting won't help until these are fixed")
        for u, r in fixes:
            print(f"  - {u}\n      {r.get('coverageState', '?')}: {r.get('hint')}")
    if rest:
        print(f"\nLater ({len(rest)}):")
        for s, u, _ in rest[: a.show_later]:
            print(f"  {s:>5}  {u}")
        if len(rest) > a.show_later:
            print(f"  ... {len(rest) - a.show_later} more")
    if waiting:
        print(f"\nAlready requested, waiting ({len(waiting)}): run `verify` after a few days")
    print("\nAfter requesting, record them: mark URL [URL ...]")


def cmd_mark(a, client, site, sd):
    log = load_json(sd / "requests.json", {})
    for u in a.urls:
        log[u] = {"requested": a.date or str(today()), "verified": None}
        print(f"marked {u}")
    save_json(sd / "requests.json", log)


def cmd_verify(a, client, site, sd):
    log = load_json(sd / "requests.json", {})
    now = today()
    due = [u for u, r in log.items() if r.get("verified") != "indexed"
           and (now - dt.date.fromisoformat(r["requested"])).days >= a.min_age]
    if not due:
        print(f"nothing to verify (requests must be at least {a.min_age} days old)")
        return
    res = inspect_many(client, site, due, workers=a.workers)
    landed = 0
    for u in due:
        rec = res[u]
        st = rec.get("status")
        log[u]["verified"] = "indexed" if st == "indexed" else rec.get("coverageState", st)
        log[u]["checked"] = str(now)
        days = (now - dt.date.fromisoformat(log[u]["requested"])).days
        if st == "indexed":
            landed += 1
            print(f"  indexed   {days:>2}d  {u}")
        else:
            print(f"  not yet   {days:>2}d  {u}  ({rec.get('coverageState', rec.get('error', '?'))})")
    print(f"{landed}/{len(due)} requested URLs indexed")
    save_json(sd / "requests.json", log)


def cmd_report(a, client, site, sd):
    end = today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=a.days - 1)
    pend = start - dt.timedelta(days=1)
    pstart = pend - dt.timedelta(days=a.days - 1)
    q = f"{WM}/sites/{enc(site)}/searchAnalytics/query"

    def totals(s, e):
        rows = client.call("POST", q, {"startDate": str(s), "endDate": str(e)}).get("rows", [])
        return rows[0] if rows else {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}

    cur, prev = totals(start, end), totals(pstart, pend)

    def delta(k, fmt):
        c, p = cur.get(k, 0), prev.get(k, 0)
        return f"{fmt(c)} (prev {fmt(p)})"

    print(f"{site}  {start} .. {end}  vs  {pstart} .. {pend}")
    print(f"  clicks       {delta('clicks', lambda v: f'{v:.0f}')}")
    print(f"  impressions  {delta('impressions', lambda v: f'{v:.0f}')}")
    print(f"  CTR          {delta('ctr', lambda v: f'{v * 100:.2f}%')}")
    print(f"  avg position {delta('position', lambda v: f'{v:.1f}')}  (lower is better)")

    rows = client.call("POST", q, {"startDate": str(start), "endDate": str(end),
                                   "dimensions": ["query", "page"], "rowLimit": 5000}).get("rows", [])
    if a.exclude_query:
        brand = re.compile(a.exclude_query, re.I)
        rows = [r for r in rows if not brand.search(r["keys"][0])]
    striking = [r for r in rows if 4 <= r["position"] <= 20 and r["impressions"] >= a.min_impressions]
    striking.sort(key=lambda r: -r["impressions"])
    print(f"\nStriking distance (position 4-20, >= {a.min_impressions} impressions): "
          "improve these pages first")
    for r in striking[: a.limit]:
        print(f"  {r['position']:>5.1f}  {r['impressions']:>6.0f} imp  {r['clicks']:>4.0f} clk  "
              f"{r['keys'][0]}  ->  {r['keys'][1]}")
    if not striking:
        print("  none yet")

    zero = [r for r in rows if r["clicks"] == 0 and r["position"] <= 10
            and r["impressions"] >= a.min_impressions]
    zero.sort(key=lambda r: -r["impressions"])
    if zero:
        print("\nPage 1 but zero clicks: rewrite the title/description for these")
        for r in zero[: a.limit]:
            print(f"  {r['position']:>5.1f}  {r['impressions']:>6.0f} imp  {r['keys'][0]}  ->  {r['keys'][1]}")


def cmd_sitemaps(a, client, site, sd):
    if a.submit:
        for sm in a.submit:
            try:
                client.call("PUT", f"{WM}/sites/{enc(site)}/sitemaps/{enc(sm)}")
                print(f"submitted {sm}")
            except ApiError as e:
                if e.status == 403:
                    die("submitting needs Owner or Full permission on the property "
                        "(Restricted users can only read)")
                raise
    for s in client.call("GET", f"{WM}/sites/{enc(site)}/sitemaps").get("sitemap", []):
        sub = sum(int(c.get("submitted", 0)) for c in s.get("contents", []))
        print(f"{(s.get('lastDownloaded') or '-')[:10]}  {sub:>5} URLs  "
              f"errors={s.get('errors', 0)} warnings={s.get('warnings', 0)}  {s['path']}")


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsc_triage.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", help="property, e.g. sc-domain:example.com or https://example.com/")
    p.add_argument("--key", help="service-account JSON key (default: $GSC_KEY, then ADC)")
    p.add_argument("--state", help="state directory (default: ~/.local/state/gsc-triage)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sites")

    i = sub.add_parser("inspect")
    i.add_argument("--sitemap", action="append", help="sitemap URL (repeatable); default: "
                   "the sitemaps submitted to the property")
    i.add_argument("--urls-file", help="extra URLs, one per line")
    i.add_argument("--skip-indexed", action="store_true",
                   help="skip URLs indexed in the previous snapshot (saves quota)")
    i.add_argument("--max", type=int, help="inspect at most N URLs")
    i.add_argument("--workers", type=int, default=8)

    t = sub.add_parser("triage")
    t.add_argument("--quota", type=int, default=10,
                   help="manual requests you can make today (default 10)")
    t.add_argument("--cooldown", type=int, default=14,
                   help="days before a requested URL is suggested again (default 14)")
    t.add_argument("--low-value", help="regex for pages to deprioritize")
    t.add_argument("--no-perf", action="store_true", help="skip the search analytics lookup")
    t.add_argument("--show-later", type=int, default=15)
    t.add_argument("--json", action="store_true")

    m = sub.add_parser("mark")
    m.add_argument("urls", nargs="+")
    m.add_argument("--date", help="YYYY-MM-DD (default today)")

    v = sub.add_parser("verify")
    v.add_argument("--min-age", type=int, default=2,
                   help="only check requests at least N days old (default 2)")
    v.add_argument("--workers", type=int, default=8)

    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=28)
    r.add_argument("--min-impressions", type=int, default=10)
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--exclude-query", metavar="REGEX",
                   help="drop matching queries from the lists, e.g. your brand name")

    s = sub.add_parser("sitemaps")
    s.add_argument("--submit", nargs="+", metavar="URL", help="(re)submit these sitemaps")
    return p


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    write = a.cmd == "sitemaps" and bool(getattr(a, "submit", None))
    client = Client(a.key, write=write)
    if a.cmd == "sites":
        return cmd_sites(a, client)
    site = resolve_site(client, a.site)
    sd = state_dir(site, a.state)
    {"inspect": cmd_inspect, "triage": cmd_triage, "mark": cmd_mark, "verify": cmd_verify,
     "report": cmd_report, "sitemaps": cmd_sitemaps}[a.cmd](a, client, site, sd)


if __name__ == "__main__":
    main()
