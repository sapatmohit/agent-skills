#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["google-auth>=2.20", "requests>=2.28", "pillow>=10.1"]
# ///
"""gsc-triage: find which of your pages Google hasn't indexed, decide which
ones are worth a manual "Request indexing" today, and check back later.

Commands
  doctor                    check the setup, say exactly what to fix
  sites                     properties this credential can see
  inspect                   URL-inspect every sitemap URL, save a snapshot
  triage                    rank not-indexed URLs: request today / fix / skip
  mark URL [URL ...]        record URLs you requested in the Search Console UI
  quota                     requests used in the rolling 24h window, next free slot
  verify                    re-inspect requested URLs, report which got indexed
  stats                     time to index, stuck requests, traffic earned since
  daily                     inspect --skip-indexed + verify + triage, for cron
  links                     find sitemap pages no other page links to (orphans)
  fixes [--write PATH]      turn "fix" URLs into sitemap edits
  indexnow                  submit URLs to Bing, Yandex & co. via IndexNow
  card                      shareable PNG of your index coverage
  report                    clicks/impressions vs previous period, striking-distance queries
  sitemaps [--submit]       list (or resubmit) the property's sitemaps

Try it without any setup: gsc_triage.py --demo inspect

Auth: a service-account key via --key or GSC_KEY, otherwise Application
Default Credentials. Property via --site or GSC_SITE (auto-picked when the
credential only sees one).
"""
from __future__ import annotations

__version__ = "1.2.0"

import argparse
import datetime as dt
import gzip
import html.parser
import json
import math
import os
import pathlib
import re
import secrets
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
REQUEST_DAILY_LIMIT = 10    # manual "Request indexing" clicks; resets on a rolling 24h
INDEXNOW = "https://api.indexnow.org/indexnow"
UA = "gsc-triage (+https://github.com/sapatmohit/agent-skills)"

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

    # Plain (unauthenticated) HTTP for sitemaps, pages and IndexNow.
    def get_url(self, url: str, timeout: int = 30) -> tuple[int, str, bytes]:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        return r.status_code, r.url, r.content

    def post_url(self, url: str, body: dict) -> tuple[int, str]:
        import requests
        r = requests.post(url, json=body, timeout=60, headers={"User-Agent": UA})
        return r.status_code, r.text[:300]


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


def fetch_sitemaps(client, roots: list[str],
                   sources: dict[str, list[str]] | None = None) -> dict[str, str | None]:
    """URL -> lastmod across all sitemaps (following indexes). A URL listed in
    several sitemaps is kept once with its newest lastmod; `sources`, if given,
    collects which sitemaps list each URL."""
    seen, out, queue = set(), {}, list(roots)
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            status, _, body = client.get_url(sm)
            if status >= 400:
                raise ValueError(f"HTTP {status}")
            urls, children = parse_sitemap(body)
        except Exception as e:
            print(f"warning: skipping sitemap {sm}: {e}", file=sys.stderr)
            continue
        queue.extend(children)
        for u, lm in urls:
            old = out.get(u)
            if u not in out or (parse_date(lm) or dt.date.min) > (parse_date(old) or dt.date.min):
                out[u] = lm
            if sources is not None and sm not in sources.setdefault(u, []):
                sources[u].append(sm)
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
        if ir.get("referringUrls"):
            rec["referrers"] = ir["referringUrls"][:5]
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


# ------------------------------------------------------------- request log

def now_dt() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def parse_dt(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def quota_status(log: dict, now: dt.datetime, limit: int) -> dict:
    """Manual requests used in the last 24h (Search Console's window is rolling,
    not midnight-to-midnight) and when the oldest one frees up."""
    window = now - dt.timedelta(hours=24)
    used = sorted(t for t in (parse_dt(r.get("requested_at")) for r in log.values())
                  if t and t > window)
    return {"used": len(used), "limit": limit, "remaining": max(0, limit - len(used)),
            "next_slot": used[0] + dt.timedelta(hours=24) if used else None}


def fmt_when(t: dt.datetime, now: dt.datetime) -> str:
    days = (t.date() - now.date()).days
    day = {0: "today", 1: "tomorrow"}.get(days, str(t.date()))
    return f"{day} {t:%H:%M}"


def quota_line(q: dict, now: dt.datetime) -> str:
    s = f"{q['used']}/{q['limit']} manual requests used in the last 24h, {q['remaining']} left"
    if q["remaining"] == 0 and q["next_slot"]:
        s += f"; next one frees up {fmt_when(q['next_slot'], now)}"
    return s


def record_indexed(log: dict, url: str, rec: dict, now: dt.date) -> bool:
    """Mark a requested URL as indexed. The index date is the first crawl on or
    after the request when Google reports one, else the day we noticed."""
    r = log.get(url)
    if not r or r.get("verified") == "indexed" or rec.get("status") != "indexed":
        return False
    requested = dt.date.fromisoformat(r["requested"])
    crawled = parse_date(rec.get("lastCrawlTime"))
    r.update(verified="indexed", checked=str(now),
             indexed_on=str(crawled if crawled and requested <= crawled <= now else now))
    return True


def index_stats(log: dict, now: dt.date, stuck_after: int = 14) -> dict:
    days, stuck = [], []
    for u, r in log.items():
        req = dt.date.fromisoformat(r["requested"])
        if r.get("indexed_on"):
            days.append((dt.date.fromisoformat(r["indexed_on"]) - req).days)
        elif (now - req).days >= stuck_after:
            stuck.append(u)
    days.sort()
    n = len(days)
    median = (days[n // 2] if n % 2 else (days[n // 2 - 1] + days[n // 2]) / 2) if n else None
    return {"requested": len(log), "indexed": n, "median_days": median,
            "within_week": sum(d <= 7 for d in days) / n if n else None, "stuck": stuck}


def snapshot_before(sd: pathlib.Path, latest: str, days: int = 6) -> dict:
    """Newest snapshot at least `days` older than `latest`, else the oldest one."""
    cutoff = dt.date.fromisoformat(latest) - dt.timedelta(days=days)
    snaps = sorted(p for p in (sd / "snapshots").glob("*.json") if p.stem != latest)
    older = [p for p in snaps if dt.date.fromisoformat(p.stem) <= cutoff]
    pick = older[-1] if older else (snaps[0] if snaps else None)
    return load_json(pick, {}) if pick else {}


def newly_indexed(snap: dict, prev: dict) -> list[str]:
    pu = prev.get("urls", {})
    return [u for u, r in snap.get("urls", {}).items()
            if r.get("status") == "indexed" and pu.get(u, {}).get("status") not in (None, "indexed")]


# ----------------------------------------------------------------- commands

def cmd_sites(a, client):
    for e in client.call("GET", f"{WM}/sites").get("siteEntry", []):
        print(f"{e.get('permissionLevel', '?'):<20} {e['siteUrl']}")


def cmd_inspect(a, client, site, sd):
    roots = a.sitemap or property_sitemaps(client, site)
    if not roots and not a.urls_file:
        die("no sitemaps submitted for this property; pass --sitemap URL or --urls-file")
    sources: dict[str, list[str]] = {}
    urls: dict[str, str | None] = fetch_sitemaps(client, roots, sources) if roots else {}
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

    n_maps = len({sm for v in sources.values() for sm in v}) or len(roots)
    print(f"{site}: {len(urls)} URLs from {n_maps} sitemap(s); inspecting {len(todo)}")
    shared = sum(len(v) > 1 for v in sources.values())
    if shared:
        print(f"  {shared} URLs are listed in more than one sitemap: inspected once, newest lastmod kept")
    results = inspect_many(client, site, todo, workers=a.workers)
    # Carry forward unchanged results for URLs skipped this run.
    merged = {u: dict(prev.get("urls", {}).get(u, {}), carried=True)
              for u in urls if u not in results and u in prev.get("urls", {})}
    for u, rec in results.items():
        rec["lastmod"] = urls.get(u)
        merged[u] = rec
    for u, rec in merged.items():
        if sources.get(u):
            rec["listed_in"] = sources[u]
    snap = {"site": site, "date": str(today()), "sitemaps": roots, "urls": merged}
    groups = sitemap_variants(snap)
    if groups:
        print(f"  {len(groups)} pages are listed under more than one URL "
              "(trailing slash, www, http/https); see `fixes`")
    save_json(sd / "snapshots" / f"{today()}.json", snap)
    # Requested URLs that turned up indexed count as verified; no extra inspections.
    log = load_json(sd / "requests.json", {})
    if sum(record_indexed(log, u, rec, today()) for u, rec in results.items()):
        save_json(sd / "requests.json", log)
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
        newly = newly_indexed(snap, prev)
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
    q = quota_status(requests_log, now_dt(), a.daily_limit)
    quota = a.quota if a.quota is not None else (q["remaining"] or a.daily_limit)
    links = load_json(sd / "links.json", {})
    inbound = links.get("inbound", {})

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
            if links:
                why = why + [link_note(url, inbound)]
            ranked.append((sc, url, why))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    picks = [r for r in ranked if r[0] > 0][:quota]
    rest = [r for r in ranked if r not in picks]

    if a.json:
        print(json.dumps({
            "snapshot": name, "quota": {**q, "next_slot": q["next_slot"] and q["next_slot"].isoformat()},
            "request_today": [{"url": u, "score": s, "why": w} for s, u, w in picks],
            "later": [{"url": u, "score": s, "why": w} for s, u, w in rest],
            "fix_not_request": [{"url": u, "coverage": r.get("coverageState"), "hint": r.get("hint")}
                                for u, r in fixes],
            "waiting": [{"url": u, "requested": d} for u, d in waiting]}, indent=2))
        return

    if q["used"] or a.quota is None:
        print(quota_line(q, now_dt()))
    when = "today" if q["remaining"] or a.quota is not None else "once the quota frees up"
    print(f"Snapshot {name}. Request indexing for these {when} "
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
    if not links:
        print("\nTip: `links` checks which of these no other page links to (orphans).")
    print("\nAfter requesting, record them: mark URL [URL ...]")


def link_note(url: str, inbound: dict) -> str:
    src = inbound.get(norm_url(url), [])
    if not src:
        return "ORPHAN: no page links here; link it from a related page"
    return f"linked from {len(src)} page(s)"


def cmd_mark(a, client, site, sd):
    log = load_json(sd / "requests.json", {})
    now = now_dt()
    at = now
    if a.at:
        at = parse_dt(a.at if "T" in a.at else f"{now.date()}T{a.at}")
        if not at:
            die("--at takes HH:MM or an ISO datetime")
        at = at if at.tzinfo else at.astimezone()
    for u in a.urls:
        entry = {"requested": a.date or str(at.date()), "verified": None}
        if not a.date:
            entry["requested_at"] = at.isoformat()
        log[u] = entry
        print(f"marked {u}")
    save_json(sd / "requests.json", log)
    q = quota_status(log, now, a.daily_limit)
    print(quota_line(q, now))
    if q["used"] > q["limit"]:
        print(f"note: that's more than the {q['limit']}/24h limit; if Search Console accepted them "
              f"all, raise it with --daily-limit", file=sys.stderr)


def cmd_quota(a, client, site, sd):
    log = load_json(sd / "requests.json", {})
    now = now_dt()
    q = quota_status(log, now, a.daily_limit)
    print(quota_line(q, now))
    if q["next_slot"] and q["remaining"]:
        print(f"the oldest of those frees up {fmt_when(q['next_slot'], now)}")
    print("The window is a rolling 24 hours from each request, not midnight. "
          "Counts only what you recorded with `mark`.")


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
        if record_indexed(log, u, rec, now):
            landed += 1
            days = (dt.date.fromisoformat(log[u]["indexed_on"])
                    - dt.date.fromisoformat(log[u]["requested"])).days
            print(f"  indexed   {days:>2}d  {u}")
        else:
            log[u].update(verified=rec.get("coverageState", rec.get("status")), checked=str(now))
            days = (now - dt.date.fromisoformat(log[u]["requested"])).days
            print(f"  not yet   {days:>2}d  {u}  ({rec.get('coverageState', rec.get('error', '?'))})")
    print(f"{landed}/{len(due)} requested URLs indexed")
    save_json(sd / "requests.json", log)
    st = index_stats(log, now)
    if st["median_days"] is not None:
        print(f"all-time: {st['indexed']}/{st['requested']} indexed, median {st['median_days']} days "
              "after the request. `stats` for more")


def cmd_stats(a, client, site, sd):
    log = load_json(sd / "requests.json", {})
    if not log:
        die("no requests recorded yet; `mark` the URLs you request, then `verify` later")
    now = today()
    st = index_stats(log, now, a.stuck_after)
    print(f"{site}: {st['requested']} URLs requested, {st['indexed']} indexed")
    if st["median_days"] is not None:
        print(f"  time to index: median {st['median_days']} days, "
              f"{st['within_week']:.0%} within a week")
    else:
        print("  none confirmed yet: `verify` (or the next `inspect`) records them once indexed")
    if st["stuck"]:
        print(f"\nNot indexed {a.stuck_after}+ days after the request ({len(st['stuck'])}): "
              "a request alone won't do it; improve the page and link to it")
        for u in st["stuck"]:
            print(f"  - {u}  ({log[u].get('verified') or 'not verified yet'})")

    done = {u: r["indexed_on"] for u, r in log.items() if r.get("indexed_on")}
    if not done or a.no_perf:
        return
    end = today() - dt.timedelta(days=3)
    start = min(dt.date.fromisoformat(d) for d in done.values())
    if start > end:
        print("\ntraffic: too early, Search Console data lags about 3 days")
        return
    rows = client.call("POST", f"{WM}/sites/{enc(site)}/searchAnalytics/query",
                       {"startDate": str(start), "endDate": str(end),
                        "dimensions": ["page", "date"], "rowLimit": 25000}).get("rows", [])
    earned: dict[str, list[float]] = {}
    for r in rows:
        page, day = r["keys"]
        if page in done and day >= done[page]:
            e = earned.setdefault(page, [0.0, 0.0])
            e[0] += r["clicks"]
            e[1] += r["impressions"]
    clicks = sum(e[0] for e in earned.values())
    imps = sum(e[1] for e in earned.values())
    print(f"\nSince they got indexed, requested pages earned {imps:.0f} impressions "
          f"and {clicks:.0f} clicks ({start} .. {end})")
    for page, (c, i) in sorted(earned.items(), key=lambda kv: -kv[1][1])[: a.limit]:
        print(f"  {i:>6.0f} imp  {c:>4.0f} clk  {page}")


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


# ------------------------------------------------------------ internal links

class _Anchors(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs += [v for k, v in attrs if k == "href" and v]


def norm_url(u: str) -> str:
    """Compare URLs ignoring scheme, host case, trailing slash and fragment."""
    s = urllib.parse.urlsplit(u.strip())
    return s.netloc.lower() + (s.path.rstrip("/") or "/") + (f"?{s.query}" if s.query else "")


def page_links(body: bytes, base: str) -> set[str]:
    p = _Anchors()
    try:
        p.feed(body.decode("utf-8", "replace"))
    except Exception:
        return set()
    out = set()
    for h in p.hrefs:
        if h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        u = urllib.parse.urljoin(base, h)
        if u.startswith("http"):
            out.add(norm_url(u))
    return out


def snapshot_urls(client, site: str, sd: pathlib.Path) -> tuple[dict, dict[str, str | None]]:
    """Latest snapshot (may be empty) and its URLs with lastmod; falls back to the sitemaps."""
    _, snap = latest_snapshot(sd)
    if snap:
        return snap, {u: r.get("lastmod") for u, r in snap["urls"].items()}
    return {}, fetch_sitemaps(client, property_sitemaps(client, site))


def cmd_links(a, client, site, sd):
    snap, urls = snapshot_urls(client, site, sd)
    pages = list(urls)[: a.max]
    print(f"crawling {len(pages)} of {len(urls)} sitemap URLs for internal links")
    inbound: dict[str, list[str]] = {}

    def crawl(u):
        try:
            status, final, body = client.get_url(u)
            return (u, page_links(body, final)) if status < 400 else (u, set())
        except Exception:
            return u, set()

    with ThreadPoolExecutor(a.workers) as pool:
        for src, targets in pool.map(crawl, pages):
            for t in targets - {norm_url(src)}:
                inbound.setdefault(t, []).append(src)
    save_json(sd / "links.json", {"date": str(today()), "crawled": len(pages),
                                  "inbound": {k: v[:20] for k, v in inbound.items()}})

    status = {u: snap.get("urls", {}).get(u, {}).get("status") for u in urls}
    orphans = [u for u in urls if norm_url(u) not in inbound and status[u] != "fix"]
    unindexed = [u for u in orphans if status[u] in ("request", None)]
    print(f"{len(orphans)} of {len(urls)} sitemap URLs have no links from the crawled pages"
          " (URLs that need a fix are left out; see `fixes`)")
    if unindexed:
        print(f"\nNot indexed and orphaned ({len(unindexed)}): link to these from a related page or "
              "hub before (or right after) requesting indexing. Google finds and values pages "
              "through links; a sitemap entry alone is a weak signal")
        for u in unindexed:
            print(f"  - {u}")
    rest = [u for u in orphans if u not in unindexed]
    if rest:
        print(f"\nOther orphans ({len(rest)}):")
        for u in rest[: a.limit]:
            print(f"  - {u}")
    if len(pages) < len(urls):
        print(f"\nnote: only {len(pages)} pages crawled (--max); some orphans may be false alarms")
    print("note: reads links in the served HTML, so links added by JavaScript aren't seen. "
          "triage now shows link counts.")


# ----------------------------------------------------------------- fix plan

def plan_fixes(client, snap: dict, follow: bool = True) -> list[dict]:
    """Turn 'fix' URLs into sitemap edits: remove, replace (old -> new) or check."""
    urls = snap.get("urls", {})
    listed = {norm_url(u) for u in urls}
    plan = []
    for u, r in urls.items():
        if r.get("status") != "fix":
            continue
        cov = (r.get("coverageState") or "").lower()
        fetch = r.get("pageFetchState") or ""
        hint = r.get("hint") or fix_hint(r, u)
        target, action = None, "check"
        if (r.get("robotsTxtState") == "DISALLOWED" or "noindex" in cov
                or (r.get("indexingState") or "").startswith("BLOCKED")):
            action = "remove"
        elif "404" in cov or "soft" in cov or fetch in ("NOT_FOUND", "SOFT_404"):
            action = "remove"
        elif "redirect" in cov or fetch == "REDIRECT_ERROR":
            if follow:
                try:
                    status, final, _ = client.get_url(u)
                    if status < 400 and norm_url(final) != norm_url(u):
                        target = final
                except Exception:
                    pass
        elif "server" not in cov:
            for c in (r.get("googleCanonical"), r.get("userCanonical")):
                if c and norm_url(c) != norm_url(u):
                    target = c
                    break
        if target:
            action = "replace"
            if norm_url(target) in listed:
                action, hint = "remove", f"{hint.split(';')[0]}; the target is already listed, so drop this one"
        plan.append({"url": u, "action": action, "target": target,
                     "coverage": r.get("coverageState"), "hint": hint})
    return plan


def variant_key(u: str) -> str:
    """Same page, ignoring scheme, www., trailing slash and fragment."""
    k = norm_url(u)
    return k[4:] if k.startswith("www.") else k


def sitemap_variants(snap: dict) -> list[dict]:
    """Pages the sitemaps list under several URLs (e.g. /a and /a/, www and
    bare host), with the one to keep when the inspection data says which."""
    urls = snap.get("urls", {})
    by_key: dict[str, list[str]] = {}
    for u in urls:
        by_key.setdefault(variant_key(u), []).append(u)
    out = []
    for us in by_key.values():
        if len(us) < 2:
            continue
        keep = next((u for u in us if urls[u].get("status") == "indexed"), None)
        if not keep:  # the variant Google picked as canonical, if it's one of them
            canon = {norm_url(c) for u in us for c in (urls[u].get("googleCanonical"),
                                                       urls[u].get("userCanonical")) if c}
            keep = next((u for u in us if norm_url(u) in canon), None)
        out.append({"keep": keep, "variants": [
            {"url": u, "status": urls[u].get("status"), "coverage": urls[u].get("coverageState"),
             "listed_in": urls[u].get("listed_in", [])} for u in us]})
    return out


def sitemap_xml(entries: list[tuple[str, str | None]]) -> str:
    from xml.sax.saxutils import escape
    rows = []
    for u, lm in entries:
        rows.append(f"  <url><loc>{escape(u)}</loc>" + (f"<lastmod>{escape(lm)}</lastmod>" if lm else "")
                    + "</url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows) + "\n</urlset>\n")


def cmd_fixes(a, client, site, sd):
    name, snap = latest_snapshot(sd)
    if not snap:
        die("no snapshot yet; run `inspect` first")
    plan = plan_fixes(client, snap, follow=not a.no_follow)
    variants = sitemap_variants(snap)
    drop = {v["url"] for g in variants if g["keep"] for v in g["variants"] if v["url"] != g["keep"]}
    plan = [p for p in plan if p["url"] not in drop]  # shown under "Sitemaps disagree" instead
    if a.write:
        by_url = {p["url"]: p for p in plan}
        out, seen = [], set()
        for u, r in snap["urls"].items():
            p = by_url.get(u)
            if (p and p["action"] == "remove") or u in drop:
                continue
            loc = p["target"] if p and p["action"] == "replace" else u
            if norm_url(loc) not in seen:
                seen.add(norm_url(loc))
                out.append((loc, r.get("lastmod")))
        pathlib.Path(a.write).write_text(sitemap_xml(out))
        print(f"wrote {a.write}: {len(out)} URLs (was {len(snap['urls'])})", file=sys.stderr)
    if a.json:
        print(json.dumps({"fixes": plan, "variants": variants}, indent=2))
        return
    if not plan and not variants:
        print(f"Snapshot {name}: nothing to fix")
        return
    if variants:
        print(f"Sitemaps disagree ({len(variants)}): the same page is listed under different URLs. "
              "List one form only, the one Google indexes")
        for g in variants:
            for v in g["variants"]:
                tag = "keep" if v["url"] == g["keep"] else "drop" if g["keep"] else "    "
                where = ", ".join(sm.rsplit("/", 1)[-1] or sm for sm in v["listed_in"]) or "?"
                print(f"  {tag}  {v['url']}  ({v['coverage'] or v['status']}; in {where})")
            if not g["keep"]:
                print("        pick one canonical form, redirect the other to it")
        print()
    if not plan:
        return
    groups = [("remove", "Remove from the sitemap (or fix the page if it should rank)"),
              ("replace", "Replace in the sitemap"), ("check", "Check by hand")]
    print(f"Snapshot {name}: {len(plan)} sitemap URLs need a fix, not an indexing request")
    for key, title in groups:
        items = [p for p in plan if p["action"] == key]
        if not items:
            continue
        print(f"\n{title} ({len(items)}):")
        for p in items:
            arrow = f"  ->  {p['target']}" if p["target"] else ""
            print(f"  - {p['url']}{arrow}\n      {p['coverage'] or '?'}: {p['hint']}")
    print("\nSitemaps are usually generated: change the generator (or the page) so these "
          "don't come back. `fixes --write sitemap.xml` writes a cleaned copy for static sites.")


# ----------------------------------------------------------------- IndexNow

def cmd_indexnow(a, client, site, sd):
    path = sd / "indexnow.json"
    cfg = load_json(path, {})
    if a.use_key or a.key_path:
        if a.use_key:
            if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", a.use_key):
                die("an IndexNow key is 8-128 characters: letters, digits and dashes")
            cfg["key"] = a.use_key
        if a.key_path:
            cfg["key_path"] = "/" + a.key_path.lstrip("/")
        save_json(path, cfg)
        a.setup = True
    if not cfg.get("key"):
        cfg["key"] = secrets.token_hex(16)
        save_json(path, cfg)
        a.setup = True
    key = cfg["key"]
    key_path = cfg.get("key_path") or f"/{key}.txt"
    if a.setup:
        print(f"IndexNow key: {key}\n\nServe a plain-text file at {key_path} on every host in the "
              f"sitemap, containing only the key:\n  https://<host>{key_path}\n(in most frameworks: "
              f"public{key_path} or static{key_path}). Deploy it, then run `indexnow --dry-run`.\n"
              "Already have a key? `indexnow --use-key KEY --key-path /indexnow-key.txt`.\n\n"
              "IndexNow reaches Bing, Yandex, Seznam, Naver and Yep (and the AI assistants that "
              "search through Bing). Google doesn't use it.")
        return

    snap, urls = snapshot_urls(client, site, sd)
    fixes = {u for u, r in snap.get("urls", {}).items() if r.get("status") == "fix"}
    sent = cfg.setdefault("submitted", {})
    todo = [u for u in urls if u not in fixes and (
        not a.changed or u not in sent or (parse_date(urls[u]) or dt.date.min) > dt.date.fromisoformat(sent[u]))]
    if not todo:
        print("nothing new to submit" + (" (--changed: no lastmod newer than the last submission)"
                                         if a.changed else ""))
        return
    by_host: dict[tuple[str, str], list[str]] = {}
    for u in todo:
        s = urllib.parse.urlsplit(u)
        by_host.setdefault((s.scheme, s.netloc), []).append(u)

    total = 0
    for (scheme, host), us in by_host.items():
        key_url = f"{scheme}://{host}{key_path}"
        try:
            status, _, body = client.get_url(key_url)
            found = status == 200 and body.decode("utf-8", "replace").strip() == key
        except Exception:
            found = False
        if not found:
            print(f"  skip  {host}: {key_url} isn't serving the key yet (see `indexnow --setup`)")
            continue
        if a.dry_run:
            print(f"  would submit {len(us)} URLs for {host}")
            continue
        for i in range(0, len(us), 10000):
            chunk = us[i:i + 10000]
            status, text = client.post_url(INDEXNOW, {"host": host, "key": key,
                                                      "keyLocation": key_url, "urlList": chunk})
            if status in (200, 202):
                total += len(chunk)
                for u in chunk:
                    sent[u] = str(today())
                print(f"  sent  {len(chunk)} URLs for {host} (HTTP {status})")
            else:
                why = {400: "bad request", 403: "key not valid for this host",
                       422: "URLs don't belong to the host or key mismatch",
                       429: "too many requests; try later"}.get(status, text)
                print(f"  fail  {host}: HTTP {status}, {why}")
    if total:
        save_json(path, cfg)
        print(f"{total} URLs submitted. Next time use `indexnow --changed` to send only updated pages.")


# ------------------------------------------------------------------- doctor

def cmd_doctor(a) -> None:
    failed = False

    def line(ok, msg, fix=None):
        nonlocal failed
        failed |= ok is False
        print(f"  {'ok  ' if ok else '--  ' if ok is None else 'FAIL'}  {msg}")
        if fix:
            print(f"        -> {fix}")

    print(f"python {sys.version.split()[0]}")
    try:
        import google.auth  # noqa: F401
        import requests  # noqa: F401
        line(True, "google-auth and requests installed")
    except ImportError:
        line(False, "google-auth / requests missing", "run with `uv run`, or `pip install google-auth requests`")
        sys.exit(1)
    try:
        import PIL  # noqa: F401
        line(True, "pillow installed (for `card`)")
    except ImportError:
        line(None, "pillow not installed; only `card` needs it", "`pip install pillow`, or run with uv")

    email = project = None
    key = a.key or os.environ.get("GSC_KEY")
    if key:
        path = pathlib.Path(key).expanduser()
        if not path.exists():
            line(False, f"key file not found: {tilde(path)}",
                 "download a JSON key (IAM & Admin > Service accounts > Keys) and save it there")
            sys.exit(1)
        data = load_json(path, {})
        email, project = data.get("client_email"), data.get("project_id")
        if data.get("type") != "service_account":
            line(False, f"{tilde(path)} isn't a service-account key", "download a JSON key for a service account")
            sys.exit(1)
        line(True, f"key {tilde(path)} for {email}")
        if path.stat().st_mode & 0o077:
            line(None, "key is readable by other users", f"chmod 600 {tilde(path)}")
    else:
        default = pathlib.Path("~/.config/gsc/key.json").expanduser()
        try:
            google.auth.default(scopes=SCOPES_RO)
            line(None, "no --key or GSC_KEY; using Application Default Credentials")
        except Exception:
            line(False, "no credentials found",
                 f"export GSC_KEY={tilde(default)}" if default.exists() else
                 "create a service-account key (see the setup section) and set GSC_KEY, or run "
                 "`gcloud auth application-default login --scopes=" + ",".join(SCOPES_RO)
                 + ",https://www.googleapis.com/auth/cloud-platform`")
            sys.exit(1)

    try:
        client = Client(a.key)
        from google.auth.transport.requests import Request
        client._creds.refresh(Request())
        line(True, "got an access token")
    except SystemExit:
        sys.exit(1)
    except Exception as e:
        line(False, f"couldn't get an access token: {e}",
             "the key may be deleted or revoked; create a new one. Also check the system clock")
        sys.exit(1)

    try:
        entries = client.call("GET", f"{WM}/sites").get("siteEntry", [])
    except ApiError as e:
        if e.status == 403 and ("has not been used" in str(e) or "disabled" in str(e)):
            url = "https://console.cloud.google.com/apis/library/searchconsole.googleapis.com"
            line(False, "the Search Console API isn't enabled for this Google Cloud project",
                 f"enable it: {url}" + (f"?project={project}" if project else "")
                 + " (free; takes a minute to apply)")
        else:
            line(False, f"listing properties failed: {e}")
        sys.exit(1)
    usable = [e["siteUrl"] for e in entries if e.get("permissionLevel") != "siteUnverifiedUser"]
    if not usable:
        line(False, "this credential sees no Search Console properties",
             "Search Console > Settings > Users and permissions > Add user: "
             f"{email or 'the account email'}, permission Restricted")
        sys.exit(1)
    line(True, f"sees {len(usable)} propert{'y' if len(usable) == 1 else 'ies'}: {', '.join(usable)}")

    site = a.site or os.environ.get("GSC_SITE") or (usable[0] if len(usable) == 1 else None)
    if not site:
        line(None, "several properties; pass --site (or set GSC_SITE) on every command")
    elif site not in usable:
        line(False, f"{site} isn't one of them", "copy the property exactly as listed above")
    else:
        sms = client.call("GET", f"{WM}/sites/{enc(site)}/sitemaps").get("sitemap", [])
        if sms:
            line(True, f"{site}: {len(sms)} sitemap(s) submitted")
        else:
            line(None, f"{site}: no sitemaps submitted",
                 "submit one in Search Console, or pass `inspect --sitemap URL`")
    if failed:
        sys.exit(1)
    print("\nall set. next: `inspect`")


# --------------------------------------------------------------------- card

FONTS = {
    True: ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    False: ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"],
}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for path in FONTS[bold]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def card_data(site: str, sd: pathlib.Path) -> dict:
    name, snap = latest_snapshot(sd)
    if not snap:
        die("no snapshot yet; run `inspect` first")
    prev = snapshot_before(sd, name)
    counts: dict[str, int] = {}
    for r in snap["urls"].values():
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    host = site.split(":", 1)[1] if site.startswith("sc-domain:") else urllib.parse.urlsplit(site).netloc
    return {"host": host, "date": name, "total": len(snap["urls"]), "indexed": counts.get("indexed", 0),
            "request": counts.get("request", 0), "fix": counts.get("fix", 0),
            "gained": len(newly_indexed(snap, prev)) if prev else None, "since": prev.get("date"),
            "stats": index_stats(load_json(sd / "requests.json", {}), today())}


def render_card(d: dict, out: pathlib.Path) -> None:
    from PIL import Image, ImageDraw
    S = 2  # draw at 2x for crisp text; 2400x1350 is X/LinkedIn's 16:9
    W, H, M = 1200 * S, 675 * S, 72 * S
    bg, panel, fg, muted, accent, warn = "#0b0d10", "#16191f", "#f5f7fa", "#8b93a1", "#4ade80", "#fbbf24"
    img = Image.new("RGB", (W, H), bg)
    g = ImageDraw.Draw(img)

    g.text((M, M), d["host"], font=_font(30 * S, True), fill=fg)
    date = _font(24 * S)
    g.text((W - M - g.textlength(d["date"], font=date), M + 4 * S), d["date"], font=date, fill=muted)

    big, sub = _font(150 * S, True), _font(38 * S)
    y = 150 * S
    num = f"{d['indexed']}"
    g.text((M, y), num, font=big, fill=fg)
    x = M + g.textlength(num, font=big) + 24 * S
    g.text((x, y + 70 * S), f"of {d['total']} pages", font=sub, fill=fg)
    g.text((x, y + 116 * S), "indexed on Google", font=sub, fill=muted)

    pct = d["indexed"] / d["total"] if d["total"] else 0
    by, bh, bw = 370 * S, 22 * S, W - 2 * M - 110 * S
    g.rounded_rectangle((M, by, M + bw, by + bh), radius=bh // 2, fill=panel)
    if pct:
        g.rounded_rectangle((M, by, M + max(bh, int(bw * pct)), by + bh), radius=bh // 2, fill=accent)
    lab = _font(30 * S, True)
    g.text((W - M - g.textlength(f"{pct:.0%}", font=lab), by - 6 * S), f"{pct:.0%}", font=lab, fill=fg)

    tiles = []
    if d["gained"] is not None:
        since = dt.date.fromisoformat(d["since"]).strftime("%b %-d") if os.name != "nt" else d["since"]
        tiles.append((f"+{d['gained']}", f"indexed since {since}", accent))
    tiles.append((str(d["request"]), "waiting to be indexed", fg))
    tiles.append((str(d["fix"]), "need a fix first", warn if d["fix"] else fg))
    st = d["stats"]
    if st["median_days"] is not None:
        md = st["median_days"]
        tiles.append((f"{md:g}d", "median time to index", fg))
    gap, ty, th = 20 * S, 440 * S, 120 * S
    tw = (W - 2 * M - gap * (len(tiles) - 1)) / len(tiles)
    val, cap = _font(52 * S, True), _font(22 * S)
    for i, (v, label, color) in enumerate(tiles):
        tx = M + i * (tw + gap)
        g.rounded_rectangle((tx, ty, tx + tw, ty + th), radius=16 * S, fill=panel)
        g.text((tx + 24 * S, ty + 16 * S), v, font=val, fill=color)
        g.text((tx + 24 * S, ty + 82 * S), label, font=cap, fill=muted)

    foot = _font(22 * S)
    g.text((M, H - M + 6 * S), "gsc-index-triage  ·  npx skills add sapatmohit/agent-skills",
           font=foot, fill=muted)
    img.save(out, optimize=True)


def cmd_card(a, client, site, sd):
    try:
        import PIL  # noqa: F401
    except ImportError:
        die("`card` needs pillow: run with `uv run`, or `pip install pillow`")
    d = card_data(site, sd)
    out = pathlib.Path(a.out or f"gsc-card-{slug(d['host'])}-{d['date']}.png")
    render_card(d, out)
    print(f"wrote {out.resolve()}")


# -------------------------------------------------------------------- daily

def cmd_daily(a, client, site, sd):
    p = build_parser()
    base = ["--daily-limit", str(a.daily_limit)]
    print("== inspect: what changed since the last run ==")
    cmd_inspect(p.parse_args(base + ["inspect", "--skip-indexed"]), client, site, sd)
    print("\n== verify: did past requests land? ==")
    cmd_verify(p.parse_args(base + ["verify"]), client, site, sd)
    print("\n== triage: today's requests ==")
    cmd_triage(p.parse_args(base + ["triage"] + (["--no-perf"] if a.no_perf else [])), client, site, sd)


# --------------------------------------------------------------------- demo

DEMO_SITE = "sc-domain:demo.example"
DEMO_BASE = "https://demo.example"
# path, state, lastmod days ago, impressions in 90d, extras
DEMO_PAGES = [
    ("/", "indexed", 3, 420, {}),
    ("/pricing/", "indexed", 20, 180, {}),
    ("/docs/", "indexed", 30, 90, {}),
    ("/blog/", "indexed", 2, 60, {}),
    ("/docs/getting-started/", "indexed", 40, 140, {}),
    ("/docs/integrations/nextjs/", "indexed", 25, 75, {}),
    ("/docs/cli/", "indexed", 35, 30, {}),
    ("/blog/next-intl-vs-react-i18next/", "landed", 9, 0, {"requested": 6, "lag": 2}),
    ("/docs/cli/check/", "landed", 12, 0, {"requested": 6, "lag": 3}),
    ("/blog/pluralization-rules/", "landed", 10, 0, {"requested": 8, "lag": 4}),
    ("/docs/integrations/django/", "landed", 15, 0, {"requested": 8, "lag": 1}),
    ("/blog/react-i18n-guide/", "unknown", 1, 0, {}),
    ("/docs/cli/sync/", "unknown", 4, 0, {"orphan": True}),
    ("/blog/icu-messageformat/", "discovered", 6, 0, {}),
    ("/docs/integrations/remix/", "discovered", 20, 0, {"today": True}),
    ("/tag/react/", "discovered", 2, 0, {}),
    ("/blog/rtl-layouts/", "crawled", 3, 0, {"crawl": 30, "orphan": True}),
    ("/blog/what-is-l10n/", "crawled", 70, 55, {"crawl": 20, "requested": 16}),
    ("/blog/translation-memory/", "crawled", 45, 12, {"crawl": 10, "today": True}),
    ("/terms/", "crawled", 200, 0, {"crawl": 40}),
    ("/privacy/", "crawled", 200, 0, {"crawl": 40}),
    ("/old-pricing/", "redirect", 300, 0, {"to": "/pricing/"}),
    ("/blog/i18n-checklist-2025/", "redirect", 100, 0, {"to": "/blog/i18n-checklist/"}),
    ("/drafts/launch-post/", "noindex", 5, 0, {}),
    ("/blog/deleted-post/", "gone", 150, 0, {}),
    ("/docs/cli/?tab=install", "canonical", 30, 0, {"to": "/docs/cli/"}),
] + [(f"/blog/{p}/", "indexed", 20 + 9 * i, 15 + 37 * i % 160, {}) for i, p in enumerate((
    "locale-detection", "date-formatting", "currency-formatting", "translation-keys",
    "machine-translation-review", "i18n-testing", "string-extraction", "language-switcher",
    "seo-for-multilingual-sites", "hreflang-guide", "lazy-loading-translations", "ci-for-i18n",
    "glossaries", "right-to-left-css", "number-formatting"))]
DEMO_QUERIES = [("icu messageformat", "/blog/icu-messageformat/", 8.6, 15, 0),
                ("react i18n", "/blog/next-intl-vs-react-i18next/", 11.2, 64, 1),
                ("hreflang tags", "/blog/hreflang-guide/", 6.4, 48, 0),
                ("next-intl vs react-i18next", "/blog/next-intl-vs-react-i18next/", 4.8, 37, 3),
                ("pluralization rules", "/blog/pluralization-rules/", 14.5, 22, 0),
                ("currency format javascript", "/blog/currency-formatting/", 3.1, 120, 9)]
# A second sitemap that overlaps the first: same blog URLs (one with an older
# lastmod) plus two variants of pages already listed.
DEMO_VARIANTS = {"https://demo.example/blog/hreflang-guide": DEMO_BASE + "/blog/hreflang-guide/",
                 "https://www.demo.example/pricing/": DEMO_BASE + "/pricing/"}


class DemoClient:
    """Offline stand-in for Client: a made-up site, so anyone can try every
    command without a key. Nothing is sent anywhere."""

    def __init__(self, before: bool = False):
        self.before = before  # inspect as of a week ago, to seed history
        self.pages = {DEMO_BASE + p: (p, st, lm, imp, ex) for p, st, lm, imp, ex in DEMO_PAGES}

    def _day(self, ago: int) -> str:
        return str(today() - dt.timedelta(days=ago))

    def inspect(self, url: str) -> dict:
        if url in DEMO_VARIANTS:
            return {"verdict": "NEUTRAL", "coverageState": "Page with redirect",
                    "googleCanonical": DEMO_VARIANTS[url]}
        _, st, lm, _, ex = self.pages.get(url, ("", "unknown", 0, 0, {}))
        if st == "landed":
            if self.before:
                return {"verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed"}
            return {"verdict": "PASS", "coverageState": "Submitted and indexed",
                    "lastCrawlTime": self._day(ex["requested"] - ex["lag"]) + "T08:00:00Z",
                    "pageFetchState": "SUCCESSFUL", "referringUrls": [DEMO_BASE + "/blog/"]}
        if st == "indexed":
            return {"verdict": "PASS", "coverageState": "Submitted and indexed",
                    "lastCrawlTime": self._day(min(lm, 9)) + "T08:00:00Z",
                    "pageFetchState": "SUCCESSFUL", "referringUrls": [DEMO_BASE + "/"]}
        if st in ("unknown", "discovered"):
            return {"verdict": "NEUTRAL", "coverageState": "URL is unknown to Google"
                    if st == "unknown" else "Discovered - currently not indexed"}
        if st == "crawled":
            return {"verdict": "NEUTRAL", "coverageState": "Crawled - currently not indexed",
                    "lastCrawlTime": self._day(ex["crawl"]) + "T08:00:00Z", "pageFetchState": "SUCCESSFUL"}
        if st == "redirect":
            return {"verdict": "NEUTRAL", "coverageState": "Page with redirect"}
        if st == "noindex":
            return {"verdict": "NEUTRAL", "coverageState": "Excluded by 'noindex' tag",
                    "indexingState": "BLOCKED_BY_META_TAG", "pageFetchState": "SUCCESSFUL"}
        if st == "gone":
            return {"verdict": "FAIL", "coverageState": "Not found (404)", "pageFetchState": "NOT_FOUND"}
        return {"verdict": "NEUTRAL", "coverageState": "Alternate page with proper canonical tag",
                "googleCanonical": DEMO_BASE + ex["to"], "userCanonical": DEMO_BASE + ex["to"],
                "pageFetchState": "SUCCESSFUL"}

    def analytics(self, body: dict) -> list[dict]:
        dims = body.get("dimensions") or []
        if not dims:
            cur = body["endDate"] == str(today() - dt.timedelta(days=3))
            return [{"clicks": 39, "impressions": 1033, "ctr": 0.0378, "position": 9.2} if cur else
                    {"clicks": 31, "impressions": 595, "ctr": 0.0521, "position": 16.9}]
        if dims == ["page"]:
            return [{"keys": [u], "clicks": round(imp * 0.03), "impressions": imp, "ctr": 0.03,
                     "position": 6 + len(p) % 9} for u, (p, _, _, imp, _) in self.pages.items() if imp]
        if dims == ["query", "page"]:
            return [{"keys": [q, DEMO_BASE + p], "position": pos, "impressions": imp, "clicks": c,
                     "ctr": c / imp} for q, p, pos, imp, c in DEMO_QUERIES]
        rows = []  # ["page", "date"]: requested pages start earning once indexed
        end = dt.date.fromisoformat(body["endDate"])
        for u, (p, st, _, _, ex) in self.pages.items():
            if st != "landed":
                continue
            day = today() - dt.timedelta(days=ex["requested"] - ex["lag"])
            while day <= end:
                i = (day.toordinal() + len(p)) % 9
                rows.append({"keys": [u, str(day)], "impressions": 3 + i, "clicks": int(i == 4)})
                day += dt.timedelta(days=1)
        return rows

    def call(self, method: str, url: str, body=None, retries: int = 0):
        if url.endswith("/sites"):
            return {"siteEntry": [{"siteUrl": DEMO_SITE, "permissionLevel": "siteFullUser"}]}
        if url.endswith("/sitemaps"):
            return {"sitemap": [{"path": DEMO_BASE + "/sitemap.xml", "errors": 0, "warnings": 0,
                                 "lastDownloaded": self._day(1) + "T06:00:00Z",
                                 "contents": [{"submitted": str(len(self.pages))}]}]}
        if "/sitemaps/" in url:
            return {}
        if url == INSPECT:
            return {"inspectionResult": {"indexStatusResult": self.inspect(body["inspectionUrl"])}}
        if url.endswith("/searchAnalytics/query"):
            return {"rows": self.analytics(body)}
        raise ApiError(404, f"demo has no {url}")

    def get_url(self, url: str, timeout: int = 30) -> tuple[int, str, bytes]:
        if url == DEMO_BASE + "/sitemap.xml":
            return 200, url, ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/'
                              'schemas/sitemap/0.9"><sitemap><loc>' + DEMO_BASE + '/sitemap-pages.xml'
                              '</loc></sitemap><sitemap><loc>' + DEMO_BASE + '/sitemap-blog.xml'
                              '</loc></sitemap></sitemapindex>').encode()
        if url == DEMO_BASE + "/sitemap-pages.xml":
            return 200, url, sitemap_xml([(u, self._day(lm)) for u, (_, _, lm, _, _) in self.pages.items()]).encode()
        if url == DEMO_BASE + "/sitemap-blog.xml":
            blog = [(u, self._day(lm + 30)) for u, (p, _, lm, _, _) in self.pages.items()
                    if p.startswith("/blog/")]
            return 200, url, sitemap_xml(blog + [(u, None) for u in DEMO_VARIANTS]).encode()
        if url in DEMO_VARIANTS:
            return 200, DEMO_VARIANTS[url], b"<html></html>"
        if url.endswith(".txt"):  # IndexNow key file: pretend it's deployed
            return 200, url, url.rsplit("/", 1)[1][:-4].encode()
        p, st, _, _, ex = self.pages.get(url, ("", "gone", 0, 0, {}))
        if st == "redirect":
            return 200, DEMO_BASE + ex["to"], b"<html></html>"
        if st == "gone":
            return 404, url, b""
        links = ["/", "/pricing/", "/docs/", "/blog/", "/terms/", "/privacy/"]
        section = "/blog/" if p == "/blog/" else "/docs/" if p == "/docs/" else None
        if section:
            links += [q for q, s2, _, _, e in DEMO_PAGES if q.startswith(section) and q != section
                      and s2 not in ("redirect", "gone", "noindex", "canonical") and not e.get("orphan")]
        if p.startswith("/blog/"):
            links.append("/tag/react/")
        return 200, url, ("<html><body>" + "".join(f'<a href="{h}">x</a>' for h in links)
                          + "</body></html>").encode()

    def post_url(self, url: str, body: dict) -> tuple[int, str]:
        return 202, "accepted (demo: nothing was sent)"


def demo_seed(sd: pathlib.Path) -> None:
    """First demo run: a week-old snapshot and a request history, so verify,
    stats, quota and card have something to show."""
    if any((sd / "snapshots").glob("*.json")):
        return
    past = today() - dt.timedelta(days=7)
    old = DemoClient(before=True)
    snap = {u: inspect_url(old, DEMO_SITE, u) for u in old.pages}
    for u, rec in snap.items():
        rec["lastmod"] = str(today() - dt.timedelta(days=old.pages[u][2]))
    save_json(sd / "snapshots" / f"{past}.json",
              {"site": DEMO_SITE, "date": str(past), "sitemaps": [DEMO_BASE + "/sitemap.xml"], "urls": snap})
    now, log = now_dt(), {}
    for u, (_, st, _, _, ex) in old.pages.items():
        if "requested" in ex:
            at = (now - dt.timedelta(days=ex["requested"])).replace(hour=10, minute=15, second=0)
            log[u] = {"requested": str(at.date()), "requested_at": at.isoformat(),
                      "verified": None if st == "landed" else "Crawled - currently not indexed"}
        elif ex.get("today"):
            at = now - dt.timedelta(hours=3)
            log[u] = {"requested": str(at.date()), "requested_at": at.isoformat(), "verified": None}
    save_json(sd / "requests.json", log)


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--site", help="property, e.g. sc-domain:example.com or https://example.com/")
    p.add_argument("--key", help="service-account JSON key (default: $GSC_KEY, then ADC)")
    p.add_argument("--state", help="state directory (default: ~/.local/state/gsc-triage)")
    p.add_argument("--demo", action="store_true",
                   help="try every command on a made-up site; no key, nothing sent")
    p.add_argument("--daily-limit", type=int,
                   default=int(os.environ.get("GSC_DAILY_LIMIT", REQUEST_DAILY_LIMIT)),
                   help=f"manual requests Search Console allows per 24h (default {REQUEST_DAILY_LIMIT})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sites")
    sub.add_parser("doctor")

    i = sub.add_parser("inspect")
    i.add_argument("--sitemap", action="append", help="sitemap URL (repeatable); default: "
                   "the sitemaps submitted to the property")
    i.add_argument("--urls-file", help="extra URLs, one per line")
    i.add_argument("--skip-indexed", action="store_true",
                   help="skip URLs indexed in the previous snapshot (saves quota)")
    i.add_argument("--max", type=int, help="inspect at most N URLs")
    i.add_argument("--workers", type=int, default=8)

    t = sub.add_parser("triage")
    t.add_argument("--quota", type=int,
                   help="manual requests to plan for (default: what's left in the 24h window)")
    t.add_argument("--cooldown", type=int, default=14,
                   help="days before a requested URL is suggested again (default 14)")
    t.add_argument("--low-value", help="regex for pages to deprioritize")
    t.add_argument("--no-perf", action="store_true", help="skip the search analytics lookup")
    t.add_argument("--show-later", type=int, default=15)
    t.add_argument("--json", action="store_true")

    m = sub.add_parser("mark")
    m.add_argument("urls", nargs="+")
    m.add_argument("--date", help="YYYY-MM-DD, to backfill older requests (not counted in the quota)")
    m.add_argument("--at", help="when you clicked, HH:MM today or an ISO datetime (default now)")

    sub.add_parser("quota")

    v = sub.add_parser("verify")
    v.add_argument("--min-age", type=int, default=2,
                   help="only check requests at least N days old (default 2)")
    v.add_argument("--workers", type=int, default=8)

    st = sub.add_parser("stats")
    st.add_argument("--stuck-after", type=int, default=14,
                    help="flag requests not indexed after N days (default 14)")
    st.add_argument("--no-perf", action="store_true", help="skip the traffic lookup")
    st.add_argument("--limit", type=int, default=10)

    d = sub.add_parser("daily")
    d.add_argument("--no-perf", action="store_true")

    ln = sub.add_parser("links")
    ln.add_argument("--max", type=int, default=500, help="crawl at most N pages (default 500)")
    ln.add_argument("--workers", type=int, default=8)
    ln.add_argument("--limit", type=int, default=20)

    f = sub.add_parser("fixes")
    f.add_argument("--write", metavar="PATH", help="write a cleaned sitemap.xml")
    f.add_argument("--no-follow", action="store_true", help="don't fetch redirects to find targets")
    f.add_argument("--json", action="store_true")

    n = sub.add_parser("indexnow")
    n.add_argument("--setup", action="store_true", help="show the key and how to host it")
    n.add_argument("--use-key", metavar="KEY", help="use an existing IndexNow key instead of a new one")
    n.add_argument("--key-path", metavar="PATH",
                   help="where the key file is served, e.g. /indexnow-key.txt (default /<key>.txt)")
    n.add_argument("--changed", action="store_true",
                   help="only URLs never submitted or with a newer lastmod")
    n.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("card")
    c.add_argument("--out", help="PNG path (default ./gsc-card-<host>-<date>.png)")

    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=28)
    r.add_argument("--min-impressions", type=int, default=10)
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--exclude-query", metavar="REGEX",
                   help="drop matching queries from the lists, e.g. your brand name")

    s = sub.add_parser("sitemaps")
    s.add_argument("--submit", nargs="+", metavar="URL", help="(re)submit these sitemaps")
    return p


COMMANDS = {"inspect": cmd_inspect, "triage": cmd_triage, "mark": cmd_mark, "quota": cmd_quota,
            "verify": cmd_verify, "stats": cmd_stats, "daily": cmd_daily, "links": cmd_links,
            "fixes": cmd_fixes, "indexnow": cmd_indexnow, "card": cmd_card, "report": cmd_report,
            "sitemaps": cmd_sitemaps}


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    if a.demo:
        if a.cmd == "doctor":
            print("demo mode needs no setup. For your own site, drop --demo and run `doctor` again.")
            return
        print(f"demo mode: made-up site demo.example, nothing is sent anywhere", file=sys.stderr)
        client, site = DemoClient(), DEMO_SITE
        if a.cmd == "sites":
            return cmd_sites(a, client)
        sd = state_dir(site, a.state)
        demo_seed(sd)
        return COMMANDS[a.cmd](a, client, site, sd)
    if a.cmd == "doctor":
        return cmd_doctor(a)
    write = a.cmd == "sitemaps" and bool(getattr(a, "submit", None))
    client = Client(a.key, write=write)
    if a.cmd == "sites":
        return cmd_sites(a, client)
    site = resolve_site(client, a.site)
    sd = state_dir(site, a.state)
    COMMANDS[a.cmd](a, client, site, sd)


if __name__ == "__main__":
    main()
