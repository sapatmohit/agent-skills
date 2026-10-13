# gsc-triage

Search Console lets you click **Request indexing** only about 10 times a day per property, and the API can't do it at all. `gsc-triage` makes those clicks count.

It URL-inspects every page in your sitemaps, separates pages a request will help from pages that need a fix first (redirects, noindex, 404, canonical), ranks the rest by search demand and freshness, and tracks whether your requests landed.

![coverage card](https://raw.githubusercontent.com/sapatmohit/agent-skills/main/assets/gsc-card-demo.png)

## Try it in 30 seconds, no setup

```bash
uvx gsc-triage --demo inspect
uvx gsc-triage --demo triage
uvx gsc-triage --demo card
```

`--demo` runs on a made-up site with a week of history. No key, nothing sent anywhere.

## Use it on your site

```bash
pip install gsc-triage        # or: uvx gsc-triage ...
gsc-triage doctor             # checks the setup, prints the exact fix
gsc-triage inspect            # inspect every sitemap URL
gsc-triage triage             # today's requests, sized to your rolling 24h quota
gsc-triage mark URL ...       # record what you requested
gsc-triage verify             # a few days later: did they get indexed?
```

Also: `stats` (time to index, traffic earned), `links` (orphan pages), `fixes` (sitemap edits), `indexnow` (submit to Bing, Yandex & co.), `card` (shareable PNG), `daily` (for cron), `report` (striking-distance queries).

Setup takes about 5 minutes: a free, read-only Google Cloud service account added to your Search Console property. Step-by-step guide: [github.com/sapatmohit/agent-skills](https://github.com/sapatmohit/agent-skills#setup-about-5-minutes-free).

## Use it from a coding agent

It's also an agent skill for Claude Code, Codex, Cursor and others: `npx skills add sapatmohit/agent-skills`. Then ask *"what should I request indexing for today?"*

MIT © Mohit Sapat
