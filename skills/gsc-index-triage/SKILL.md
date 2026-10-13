---
name: gsc-index-triage
description: Find which pages Google hasn't indexed and decide which deserve today's few manual "Request indexing" clicks in Google Search Console. URL-inspects every sitemap URL, separates "request" from "fix first" (redirects, noindex, 404, canonical) and turns those into sitemap edits, flags orphan pages with no internal links, ranks by search demand and freshness, tracks the rolling 24h request quota, verifies later and measures time to index. Also submits to Bing/Yandex via IndexNow, reports clicks/impressions vs the previous period and striking-distance queries, and renders a shareable coverage card. Use when the user asks why pages aren't indexed, what to request indexing for, wants a Search Console / GSC status check, wants to try it (demo mode, no setup), or is tracking SEO after publishing new content.
---

# GSC index triage

Search Console limits manual "Request indexing" to roughly 10 URLs per property per day (sometimes fewer), and the API cannot request indexing at all. This skill makes those few clicks count: it finds every not-indexed URL, explains which ones a request won't fix, and hands the user a short, ranked list.

Script: `scripts/gsc_triage.py` (Python 3.9+). Run it with `uv run scripts/gsc_triage.py <command>` so that uv installs its dependencies (`google-auth`, `requests`, and `pillow` for `card`). Without uv, run `pip install google-auth requests pillow` and use `python3`. It's also on PyPI: `uvx gsc-triage <command>` or `pip install gsc-triage` gives the same CLI as `gsc-triage`, which helps when the user wants to run it outside the agent (e.g. from cron).

## Setup (once per user)

Check first: `uv run scripts/gsc_triage.py doctor`. It checks the dependencies, key, token, API, property access and sitemaps, and prints the exact fix for whatever fails. If it ends with "all set", skip this section.

**Just want to see it work?** Add `--demo` to any command (`--demo inspect`, then `--demo triage`, `--demo card`...). It runs on a made-up site with a week of history, needs no key, and sends nothing anywhere. Offer this when the user is curious but hasn't set anything up.

Otherwise walk the user through creating a read-only service account. Tell them the steps; they click through them:

1. In the Google Cloud console, open (or create) a project, go to **APIs & Services > Library**, and enable **Google Search Console API**. It's free and needs no billing.
2. Go to **IAM & Admin > Service accounts > Create**, name it e.g. `gsc-reader`, and skip the roles step.
3. Open the account, go to **Keys > Add key > JSON**, and download the file.
4. Have the user move the file to `~/.config/gsc/key.json` and run `chmod 600` on it. **Never ask the user to paste the key into chat.**
5. In Search Console, go to **Settings > Users and permissions > Add user**, enter the service account email, and choose **Restricted** (read-only is enough).

Then `export GSC_KEY=~/.config/gsc/key.json` (or pass `--key`) and run `doctor` again. If the credential sees several properties, pass `--site sc-domain:example.com` or `--site https://example.com/` (or set `GSC_SITE`).

Alternative without a key file: `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/webmasters.readonly,https://www.googleapis.com/auth/cloud-platform`.

## Workflow

1. **`inspect`** URL-inspects every URL in the property's submitted sitemaps (or `--sitemap URL`, repeatable; `--urls-file`). It runs 8 at a time, about 1 minute per 60 URLs, and saves a dated snapshot. It also prints what was newly indexed and what dropped out since the last snapshot. Quota is 2,000 inspections per day per property: on big sites use `--skip-indexed` (re-checks only URLs that weren't indexed last time) or `--max N`.
2. **`triage --quota N`** prints three lists:
   - **Request today**: the top N requestable URLs, where N is what's left of the daily limit (10 by default; `--daily-limit` or `GSC_DAILY_LIMIT` to change, `--quota N` to override). The quota appears to reset on a rolling 24 hours from each request, not at midnight, so triage counts the requests recorded with `mark` in the last 24h and says when the next slot frees up. If Search Console says "Quota exceeded" anyway, run `quota` and tell the user that time; requests made outside `mark` aren't counted.
   - **Fix, don't request**: pages with redirects, noindex, 404/soft 404, robots blocks or canonicals pointing elsewhere. Requesting indexing does nothing for these. Each comes with a hint; fix the cause, usually in the sitemap or the page.
   - **Later**: the remaining URLs, ranked.

   Ranking: never-seen and discovered-not-crawled URLs first, pages with past impressions boosted (capped, on a log scale, so history can't drown out new pages), pages updated since their last crawl boosted, legal/author/tag/pagination pages sunk (`--low-value REGEX` overrides that list). If `links` has run, each URL shows how many pages link to it, and orphans are flagged. Use `--json` when you need to parse the output.
3. The user requests indexing in the Search Console UI (URL inspection > Request indexing). Give them the URLs as a plain list they can copy. You cannot do this step for them.
4. **`mark URL ...`** records what they actually requested, with the time (`--at 09:30` if they clicked earlier), and prints the quota left. Those URLs are held back from triage for 14 days (`--cooldown`).
5. **`verify`** (2+ days later) re-inspects requested URLs and reports which got indexed. `inspect` also records any requested URL it finds indexed, so a regular `inspect` covers this too.
6. **`stats`** shows the median time from request to indexing, requests still not indexed after 14 days (those need page work, not another request), and the impressions and clicks those pages earned since they were indexed. Quote these numbers; they show whether the work paid off.

**`daily`** runs `inspect --skip-indexed`, `verify` and `triage` in one go: the morning routine. To run it unattended, suggest a local cron or launchd job (the key stays on their machine), e.g. `0 8 * * * GSC_KEY=~/.config/gsc/key.json uv run /path/to/gsc_triage.py daily > ~/gsc-today.txt`. A cloud routine (`/schedule`) can only run it if the key is available there; don't paste the key into one.

**`links`** fetches every sitemap page and counts internal links to each URL. A not-indexed page that nothing links to (an orphan) is the usual reason a page stays "Discovered" or drops back out after a request: tell the user to link it from a related post, a hub page or the nav, then request. It reads the served HTML only, so links added by JavaScript are missed. Results are stored and shown in `triage`.

**`fixes`** turns the "fix, don't request" URLs into concrete sitemap edits: remove (404, noindex, robots, or a redirect/canonical target that's already listed) or replace old → new (it follows redirects to find the final URL). Sitemaps are usually generated, so find the generator in the user's codebase (e.g. `sitemap.ts`, `next-sitemap.config.js`, a CMS setting) and change it there; offer to make that change. `fixes --write sitemap.xml` writes a cleaned copy for static sites. `--json` for parsing. It also lists **sitemaps that disagree**: the same page under several URLs (trailing slash, `www`, http/https), marking which form to keep (the indexed one, or Google's canonical) and which sitemap lists each. `inspect` already merges a URL listed in several sitemaps into one inspection and keeps its newest `lastmod`.

**`indexnow`** submits URLs to Bing, Yandex, Seznam, Naver and Yep through IndexNow, which, unlike Google, accepts automated submissions. The first run creates a key and explains how to host it as `https://<host>/<key>.txt` (offer to add the file to the site's public/static folder). After it's deployed, `indexnow` submits all non-broken sitemap URLs; later, `indexnow --changed` sends only new or updated ones. `--dry-run` checks the key file without submitting. Google ignores IndexNow; say so if asked.

**`card`** writes a 2400×1350 PNG of the property's coverage (indexed of total, gained since about a week ago, waiting, need a fix, median time to index) for posting on X or LinkedIn. It needs Pillow, which `uv run` installs. Show the user the image and the path.

**`report --days 28 --exclude-query 'brandname'`** compares clicks, impressions, CTR and position with the previous period. It lists **striking-distance** queries (position 4–20, the cheapest wins: improve that page's content, title and internal links) and **page-1 queries with zero clicks** (rewrite the title and meta description). Exclude brand queries, or they crowd the lists. Data lags about 3 days.

**`sitemaps`** lists submitted sitemaps with last-download dates. A sitemap with no download date has never been fetched by Google; tell the user to resubmit it in the UI. `sitemaps --submit URL` resubmits, but it needs Owner or Full permission.

State (snapshots, request log, link counts, IndexNow key) lives in `~/.local/state/gsc-triage/<property>/`. Override with `--state` or `GSC_TRIAGE_STATE`.

## Interpreting results honestly

- **URL is unknown / Discovered – not indexed**: a manual request genuinely helps here. Also check that the page is linked from somewhere Google crawls (nav, related posts, a hub page).
- **Crawled – currently not indexed**: Google fetched it and declined. A request alone rarely changes that. Suggest improving the page (unique substance, not thin or duplicated), adding internal links, then requesting again. Triage boosts these only when the page changed after its last crawl.
- An indexed page still has to earn rankings. Indexing is the precondition, not the win.
- **Orphans**: a request gets a page crawled, but without internal links Google often won't keep it. Fix links first when `links` flags one.
- Don't suggest Google's **Indexing API** for ordinary pages. It's limited to JobPosting and BroadcastEvent pages, and using it for anything else risks losing access.
- New pages usually enter at low positions, so the average position can look worse right after publishing. Judge trends over 4–8 weeks.
