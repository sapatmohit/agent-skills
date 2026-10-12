# agent-skills

Skills for coding agents (Claude Code, Codex, Cursor, Gemini CLI and anything else that reads `SKILL.md`).

```bash
npx skills add sapatmohit/agent-skills
```

## gsc-index-triage

Google Search Console lets you click "Request indexing" only about 10 times a day per property, and the API can't do it at all. This skill makes those clicks count.

- **Inspects every sitemap URL** through the URL Inspection API and saves dated snapshots, so you see what got indexed and what dropped out since last time.
- **Separates "request" from "fix first"**: redirects, noindex, 404s, robots blocks and canonical mismatches don't need a request, they need a fix. Each gets a one-line hint.
- **Ranks the rest**: never-seen pages, pages with past impressions, and pages updated since their last crawl go first. Legal, author, tag and pagination pages go last.
- **Tracks your requests** and verifies a few days later which ones landed.
- **Reports performance**: clicks, impressions, CTR and position against the previous period, plus striking-distance queries (position 4–20) and page-1 queries with zero clicks.

```text
$ uv run scripts/gsc_triage.py inspect
sc-domain:example.com: 107 URLs from 3 sitemap(s); inspecting 107
107 URLs: 84 indexed, 23 request, 0 fix, 0 error

$ uv run scripts/gsc_triage.py triage --quota 5
Request indexing for these today (Search Console > URL inspection > Request indexing):
 1. https://docs.example.com/cli/check
      score 42.0: Google has never seen it
 ...
Fix, don't request (2): requesting won't help until these are fixed
  - https://example.com/old-pricing
      Page with redirect: sitemap lists a redirecting URL; list the final URL instead
```

Setup takes about 5 minutes: a read-only service account added as a Restricted user on your property. The Search Console API is free. See [SKILL.md](skills/gsc-index-triage/SKILL.md#setup-once-per-user).

The script is a single file with two dependencies (`google-auth` and `requests`), declared inline so that `uv run` handles them. It also works on its own without an agent:

```bash
export GSC_KEY=~/.config/gsc/key.json
uv run skills/gsc-index-triage/scripts/gsc_triage.py --help
```

Tests: `python3 -m unittest discover skills/gsc-index-triage/tests`

## License

MIT
