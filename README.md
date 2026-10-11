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
python cli.py search "who is Leo Messi?" --depth basic --max-results 10
python cli.py extract "https://en.wikipedia.org/wiki/Lionel_Messi,https://example.com"
python cli.py research "latest developments in AI" --model mini
```

Or as a library:

```python
from client import search_headless, extract_headless, research_headless

search_headless("who is Leo Messi?")
extract_headless(["https://example.com"])
research_headless("latest developments in AI")
```

## Headless guarantees

- `search`: `include_answer` is forced off, so no LLM answer is generated.
  `include_raw_content` is on by default for full scraped text.
- `extract`: pure scraping, no AI involved.
- `research`: creates a task and polls until completion, then returns the raw
  `content` and `sources` untouched. Note Tavily's own model writes the report
  server-side; nothing is reprocessed locally.
