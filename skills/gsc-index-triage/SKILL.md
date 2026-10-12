---
name: gsc-index-triage
description: Find which pages Google hasn't indexed and decide which deserve today's few manual "Request indexing" clicks in Google Search Console. URL-inspects every sitemap URL, separates "request" from "fix first" (redirects, noindex, 404, canonical), ranks by search demand and freshness, tracks what was requested, and verifies later. Also reports clicks/impressions vs the previous period and striking-distance queries. Use when the user asks why pages aren't indexed, what to request indexing for, wants a Search Console / GSC status check, or is tracking SEO after publishing new content.
---

# GSC index triage

Search Console limits manual "Request indexing" to roughly 10 URLs per property per day (sometimes fewer), and the API cannot request indexing at all. This skill makes those few clicks count: it finds every not-indexed URL, explains which ones a request won't fix, and hands the user a short, ranked list.

Script: `scripts/gsc_triage.py` (Python 3.9+). Run it with `uv run scripts/gsc_triage.py <command>` so that uv installs its two dependencies (`google-auth` and `requests`). Without uv, run `pip install google-auth requests` and use `python3`.

## Setup (once per user)

Check first: `uv run scripts/gsc_triage.py sites`. If it lists the property, skip this.

Otherwise walk the user through creating a read-only service account. Tell them the steps; they click through them:

1. In the Google Cloud console, open (or create) a project, go to **APIs & Services > Library**, and enable **Google Search Console API**. It's free and needs no billing.
2. Go to **IAM & Admin > Service accounts > Create**, name it e.g. `gsc-reader`, and skip the roles step.
3. Open the account, go to **Keys > Add key > JSON**, and download the file.
4. Have the user move the file to `~/.config/gsc/key.json` and run `chmod 600` on it. **Never ask the user to paste the key into chat.**
5. In Search Console, go to **Settings > Users and permissions > Add user**, enter the service account email, and choose **Restricted** (read-only is enough).

Then `export GSC_KEY=~/.config/gsc/key.json` (or pass `--key`). If the credential sees several properties, pass `--site sc-domain:example.com` or `--site https://example.com/` (or set `GSC_SITE`).

Alternative without a key file: `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/webmasters.readonly,https://www.googleapis.com/auth/cloud-platform`.

## Workflow

1. **`inspect`** URL-inspects every URL in the property's submitted sitemaps (or `--sitemap URL`, repeatable; `--urls-file`). It runs 8 at a time, about 1 minute per 60 URLs, and saves a dated snapshot. It also prints what was newly indexed and what dropped out since the last snapshot. Quota is 2,000 inspections per day per property: on big sites use `--skip-indexed` (re-checks only URLs that weren't indexed last time) or `--max N`.
2. **`triage --quota N`** prints three lists:
   - **Request today**: the top N requestable URLs. Ask the user how many requests they have left today; the default is 10.
   - **Fix, don't request**: pages with redirects, noindex, 404/soft 404, robots blocks or canonicals pointing elsewhere. Requesting indexing does nothing for these. Each comes with a hint; fix the cause, usually in the sitemap or the page.
   - **Later**: the remaining URLs, ranked.

   Ranking: never-seen and discovered-not-crawled URLs first, pages with past impressions boosted, pages updated since their last crawl boosted, legal/author/tag/pagination pages sunk (`--low-value REGEX` overrides that list). Use `--json` when you need to parse the output.
3. The user requests indexing in the Search Console UI (URL inspection > Request indexing). Give them the URLs as a plain list they can copy. You cannot do this step for them.
4. **`mark URL ...`** records what they actually requested. Those URLs are held back from triage for 14 days (`--cooldown`).
5. **`verify`** (2+ days later) re-inspects requested URLs and reports which got indexed.

**`report --days 28 --exclude-query 'brandname'`** compares clicks, impressions, CTR and position with the previous period. It lists **striking-distance** queries (position 4–20, the cheapest wins: improve that page's content, title and internal links) and **page-1 queries with zero clicks** (rewrite the title and meta description). Exclude brand queries, or they crowd the lists. Data lags about 3 days.

**`sitemaps`** lists submitted sitemaps with last-download dates. A sitemap with no download date has never been fetched by Google; tell the user to resubmit it in the UI. `sitemaps --submit URL` resubmits, but it needs Owner or Full permission.

State (snapshots, request log) lives in `~/.local/state/gsc-triage/<property>/`. Override with `--state` or `GSC_TRIAGE_STATE`.

## Interpreting results honestly

- **URL is unknown / Discovered – not indexed**: a manual request genuinely helps here. Also check that the page is linked from somewhere Google crawls (nav, related posts, a hub page).
- **Crawled – currently not indexed**: Google fetched it and declined. A request alone rarely changes that. Suggest improving the page (unique substance, not thin or duplicated), adding internal links, then requesting again. Triage boosts these only when the page changed after its last crawl.
- An indexed page still has to earn rankings. Indexing is the precondition, not the win.
- Don't suggest Google's **Indexing API** for ordinary pages. It's limited to JobPosting and BroadcastEvent pages, and using it for anything else risks losing access.
- New pages usually enter at low positions, so the average position can look worse right after publishing. Judge trends over 4–8 weeks.
