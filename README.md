# tavily-headless

Raw Tavily API access in Python with no local AI post-processing. Responses are
passed through exactly as Tavily returns them.

## Install

```
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your key:

```
TAVILY_API_KEY=tvly-YOUR_API_KEY
```

## Usage

```
python cli.py search "who is Leo Messi?" --depth basic --topic news --time-range week
python cli.py extract "https://en.wikipedia.org/wiki/Lionel_Messi" --depth advanced --query "career stats"
python cli.py research "latest developments in AI" --model pro --length long --citations apa
python cli.py research "compare these vendors" --schema schema.json --file notes.md
python cli.py crawl "https://docs.tavily.com" --instructions "Find all pages about the Python SDK" --limit 30
python cli.py map "https://docs.tavily.com" --limit 100
```

Research streams live progress to stderr (`--quiet` to silence). Reports,
sources and credit usage pass through untouched.

### Standalone page

Serves a self-contained web UI (no build step, no CDN) with Search, Extract,
Research, Crawl and Map tabs:

```
python cli.py web --port 8000
```

Then open http://127.0.0.1:8000. Your API key stays server-side in `.env`.

The Research tab is a live console: the report is rendered as the agent writes
it, with the actual search queries, discovered sources and subtopic deep-dives
appearing in real time. Reports render as Markdown with clickable citations, a
section outline and a numbered reference list; structured-output tasks render
as a sectioned JSON document. Research tasks survive a page refresh and can be
reopened from the history drawer.

Other tabs: Search exposes topic (general/news/finance), freshness windows,
domain filters (restrict or prefer), country boost and language bias/strict
filter, cache freshness (`max_age_hours`, 0 = live), `auto_parameters`,
`safe_search`, chunks per source, images, published dates, exact-phrase
matching and raw-content format (markdown/text); Extract exposes depth,
format and a focus query that reranks chunks; Crawl walks a site with optional
plain-English instructions, regex path/domain filters and chunks per source;
Map discovers a site's URL graph (with the same regex filters and an
external-domain toggle) and hands selected URLs to Extract or Crawl.

Keyboard: `1`–`5` switch tabs, `/` focuses the query, `Ctrl+Enter` runs,
`R` toggles Raw JSON, `?` shows all shortcuts, `Esc` closes overlays. Inputs
and the last result of each tab persist in `localStorage`; a running research
task survives refresh and offers a resume banner (stop-watching keeps the task
running server-side).

`public/index.html` is the front end and `app.py` exposes `/api/search`,
`/api/extract`, `/api/research`, `/api/crawl`, `/api/map` (plus
`GET /api/research?id=...` for task status and live progress).

Markdown rendering uses a vendored copy of [marked](https://github.com/markedjs/marked)
(`public/marked.umd.js`, MIT; see `public/marked.LICENSE.txt`). The Raw JSON
toggle always shows the exact API response.

## Deploy to Firebase (always-on link)

The same page runs on Firebase Hosting with a Python Cloud Functions backend.
Your Tavily key is stored as a Cloud Secret and never reaches the browser.

Prerequisites: a Firebase project on the Blaze (pay-as-you-go) plan with
Firestore enabled, plus the Firebase CLI (`npm i -g firebase-tools`).

```
firebase login
firebase use <your-project-id>
firebase functions:secrets:set TAVILY_API_KEY
firebase deploy
```

Your site is then live at `https://<your-project-id>.web.app`.

- `functions/main.py` exposes `api_search`, `api_extract`, `api_research`,
  `api_crawl` and `api_map`.
- Research tasks live in a Firestore `research_tasks` collection. The
  `research_worker` trigger consumes Tavily's SSE stream (`stream: true`),
  writes throttled progress snapshots (phase, queries, sources, subtopics,
  live report draft) back to the task document, and finishes with the
  canonical result from `GET /research/{id}` including credit usage.
- If the stream drops mid-task, the worker falls back to polling
  `get_research` using the request id from the streaming response's
  `x-request-id` header (per-chunk payload ids are not retrievable). If the
  result endpoint is unavailable but the stream already delivered a report,
  that streamed report is passed through instead of failing the task (credit
  usage is then unknown).
- Firestore-triggered functions are capped at 540s by the platform, so the
  worker exits gracefully at 500s and marks over-long tasks failed with
  guidance (very long pro/long reports can exceed this). The local server
  (`python cli.py web`) has no such cap and allows up to 900s.
- Attached files are stored briefly in a separate `research_files` document
  (base64 payloads can approach Firestore's 1 MiB document limit) and deleted
  after the task is created.

Request validation is duplicated in `client.py` (local) and `functions/main.py`
(Firebase, deployed standalone). Keep the two in sync when changing limits.

Or as a library:

```python
from client import search_headless, extract_headless, research_headless, crawl_headless, map_headless

search_headless("who is Leo Messi?", topic="news")
extract_headless(["https://example.com"], extract_depth="advanced")
research_headless("latest developments in AI", model="pro", output_length="long")
crawl_headless("https://docs.tavily.com", limit=30)
map_headless("https://docs.tavily.com")
```

## Headless guarantees

- `search`: `include_answer` is forced off, so no LLM answer is generated.
  `include_raw_content` is on by default for full scraped text.
- `extract`, `crawl`, `map`: pure scraping/discovery, no AI involved.
- `research`: creates a task and streams it to completion, then returns the raw
  `content`, `sources` and `usage` untouched. Tavily's own model writes the
  report server-side; nothing is reprocessed locally. The live draft shown
  while writing is the same content, verbatim.

## Limits and costs worth knowing

- Research: models `mini`/`pro`/`auto`; `output_length` short/standard/long;
  citations numbered/MLA/APA/Chicago; prefer or block up to 20 domains each;
  up to 5 attached `.txt`/`.md`/`.json` files (80,000 words and 600 KB combined
  enforced here); optional JSON Schema for structured output.
- Search: `advanced` depth costs 2 credits (others 1); up to 20 results;
  include/exclude up to 300/150 domains.
- Extract: 1 credit per 5 successful pages, 2 at advanced depth; up to 20 URLs.
- Crawl/Map: 1 credit per 10 pages, 2 with natural-language instructions.
- Credit usage is requested on every call (`include_usage`) and shown in the UI.
