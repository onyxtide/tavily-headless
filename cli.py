import argparse
import base64
import json
import os
import sys

from app import run as run_web
from client import (
    crawl_headless,
    extract_headless,
    map_headless,
    research_headless,
    search_headless,
    validate_crawl_body,
    validate_extract_body,
    validate_map_body,
    validate_research_body,
    validate_search_body,
)


def _split_domains(value):
    return [d.strip() for d in (value or "").split(",") if d.strip()]


def _progress_printer(progress):
    phase = progress.get("phase", "")
    detail = ""
    if phase == "searching":
        detail = f" ({progress.get('searches', 0)} searches, {progress.get('sources_count', 0)} sources)"
    elif phase == "deep_dive":
        detail = f" ({len(progress.get('subtopics', []))} subtopics)"
    print(f"[research] {phase}{detail}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="tavily-headless",
        description="Raw Tavily API access with no local AI post-processing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="POST /search, include_answer kept off")
    p_search.add_argument("query")
    p_search.add_argument(
        "--depth",
        choices=["basic", "advanced", "fast", "ultra-fast"],
        default="basic",
    )
    p_search.add_argument("--max-results", type=int, default=10)
    p_search.add_argument("--topic", choices=["general", "news", "finance"], default="general")
    p_search.add_argument("--time-range", choices=["day", "week", "month", "year"])
    p_search.add_argument("--start-date", help="YYYY-MM-DD")
    p_search.add_argument("--end-date", help="YYYY-MM-DD")
    p_search.add_argument("--include-domains", help="comma-separated hosts")
    p_search.add_argument("--exclude-domains", help="comma-separated hosts")
    p_search.add_argument("--chunks", type=int, help="chunks per source (1-3)")
    p_search.add_argument("--exact-match", action="store_true")
    p_search.add_argument("--images", action="store_true")
    p_search.add_argument("--image-descriptions", action="store_true")
    p_search.add_argument("--published-date", action="store_true")
    p_search.add_argument("--filter-published-date", action="store_true")
    p_search.add_argument("--no-raw", action="store_true", help="skip include_raw_content")
    p_search.add_argument("--raw-format", choices=["markdown", "text"], default="markdown")
    p_search.add_argument("--domain-mode", choices=["restrict", "prefer"], default="restrict")
    p_search.add_argument("--country", help="boost results from a country (topic=general only)")
    p_search.add_argument("--language", help="ISO 639-1 code or language name")
    p_search.add_argument("--filter-language", action="store_true", help="strict language filter")
    p_search.add_argument("--max-age-hours", type=int, help="cache freshness: -1..720 (0=live)")
    p_search.add_argument("--auto-parameters", action="store_true")
    p_search.add_argument("--safe-search", action="store_true")

    p_extract = sub.add_parser("extract", help="POST /extract, pure scraping")
    p_extract.add_argument("urls", help="comma-separated URLs (max 20)")
    p_extract.add_argument("--format", choices=["markdown", "text"], default="markdown")
    p_extract.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    p_extract.add_argument("--query", help="focus query: rerank chunks by relevance")
    p_extract.add_argument("--chunks", type=int, help="chunks per source (1-5, with --query)")
    p_extract.add_argument("--images", action="store_true")

    p_research = sub.add_parser(
        "research",
        help="POST /research with live streaming progress; raw report pass-through",
    )
    p_research.add_argument("query")
    p_research.add_argument("--model", choices=["mini", "pro", "auto"], default="mini")
    p_research.add_argument("--length", choices=["short", "standard", "long"], default="standard")
    p_research.add_argument(
        "--citations", choices=["numbered", "mla", "apa", "chicago"], default="numbered"
    )
    p_research.add_argument("--prefer-domains", help="comma-separated hosts (soft preference)")
    p_research.add_argument("--block-domains", help="comma-separated hosts (hard block)")
    p_research.add_argument("--schema", help="path to a JSON Schema file for structured output")
    p_research.add_argument(
        "--file", action="append", default=[], help="attach .txt/.md/.json (repeatable, max 5)"
    )
    p_research.add_argument("--timeout", type=float, default=900)
    p_research.add_argument("--quiet", action="store_true", help="no progress lines on stderr")

    p_crawl = sub.add_parser("crawl", help="POST /crawl, site traversal with extraction")
    p_crawl.add_argument("url")
    p_crawl.add_argument("--instructions", help="natural-language guidance (2x credits)")
    p_crawl.add_argument("--max-depth", type=int, default=1)
    p_crawl.add_argument("--max-breadth", type=int, default=20)
    p_crawl.add_argument("--limit", type=int, default=50)
    p_crawl.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    p_crawl.add_argument("--format", choices=["markdown", "text"], default="markdown")
    p_crawl.add_argument("--chunks", type=int, help="chunks per source (1-5, with --instructions)")
    p_crawl.add_argument("--images", action="store_true")
    p_crawl.add_argument("--no-external", action="store_true", help="skip external-domain links")
    p_crawl.add_argument("--select-paths", action="append", default=[], help="regex (repeatable)")
    p_crawl.add_argument("--exclude-paths", action="append", default=[], help="regex (repeatable)")
    p_crawl.add_argument("--select-domains", action="append", default=[], help="regex (repeatable)")
    p_crawl.add_argument("--exclude-domains", action="append", default=[], help="regex (repeatable)")

    p_map = sub.add_parser("map", help="POST /map, discover a site's URL graph")
    p_map.add_argument("url")
    p_map.add_argument("--instructions", help="natural-language guidance (2x credits)")
    p_map.add_argument("--max-depth", type=int, default=1)
    p_map.add_argument("--max-breadth", type=int, default=20)
    p_map.add_argument("--limit", type=int, default=50)
    p_map.add_argument("--no-external", action="store_true", help="skip external-domain links")
    p_map.add_argument("--select-paths", action="append", default=[], help="regex (repeatable)")
    p_map.add_argument("--exclude-paths", action="append", default=[], help="regex (repeatable)")
    p_map.add_argument("--select-domains", action="append", default=[], help="regex (repeatable)")
    p_map.add_argument("--exclude-domains", action="append", default=[], help="regex (repeatable)")

    p_web = sub.add_parser("web", help="serve the standalone page")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args()

    if args.command == "web":
        run_web(host=args.host, port=args.port)
        return

    if args.command == "search":
        response = search_headless(
            **validate_search_body(
                {
                    "query": args.query,
                    "depth": args.depth,
                    "max_results": args.max_results,
                    "topic": args.topic,
                    "time_range": args.time_range,
                    "start_date": args.start_date,
                    "end_date": args.end_date,
                    "include_domains": _split_domains(args.include_domains),
                    "exclude_domains": _split_domains(args.exclude_domains),
                    "chunks_per_source": args.chunks,
                    "exact_match": args.exact_match,
                    "include_images": args.images,
                    "include_image_descriptions": args.image_descriptions,
                    "include_published_date": args.published_date,
                    "filter_by_published_date": args.filter_published_date,
                    "include_raw": not args.no_raw,
                    "raw_format": args.raw_format,
                    "include_domains_mode": args.domain_mode,
                    "country": args.country,
                    "language": args.language,
                    "filter_by_language": args.filter_language,
                    "max_age_hours": args.max_age_hours,
                    "auto_parameters": args.auto_parameters,
                    "safe_search": args.safe_search,
                }
            )
        )
    elif args.command == "extract":
        response = extract_headless(
            **validate_extract_body(
                {
                    "urls": [u.strip() for u in args.urls.split(",") if u.strip()],
                    "format": args.format,
                    "extract_depth": args.depth,
                    "query": args.query,
                    "chunks_per_source": args.chunks,
                    "include_images": args.images,
                }
            )
        )
    elif args.command == "crawl":
        response = crawl_headless(
            **validate_crawl_body(
                {
                    "url": args.url,
                    "instructions": args.instructions,
                    "max_depth": args.max_depth,
                    "max_breadth": args.max_breadth,
                    "limit": args.limit,
                    "extract_depth": args.depth,
                    "format": args.format,
                    "chunks_per_source": args.chunks,
                    "include_images": args.images,
                    "allow_external": not args.no_external,
                    "select_paths": args.select_paths,
                    "exclude_paths": args.exclude_paths,
                    "select_domains": args.select_domains,
                    "exclude_domains": args.exclude_domains,
                }
            )
        )
    elif args.command == "map":
        response = map_headless(
            **validate_map_body(
                {
                    "url": args.url,
                    "instructions": args.instructions,
                    "max_depth": args.max_depth,
                    "max_breadth": args.max_breadth,
                    "limit": args.limit,
                    "allow_external": not args.no_external,
                    "select_paths": args.select_paths,
                    "exclude_paths": args.exclude_paths,
                    "select_domains": args.select_domains,
                    "exclude_domains": args.exclude_domains,
                }
            )
        )
    elif args.command == "research":
        files = []
        for path in args.file:
            with open(path, "rb") as fh:
                raw = fh.read()
            files.append(
                {"name": os.path.basename(path), "data": base64.b64encode(raw).decode()}
            )
        schema = None
        if args.schema:
            with open(args.schema, encoding="utf-8") as fh:
                schema = json.load(fh)
        query, params, files_payload, _meta = validate_research_body(
            {
                "query": args.query,
                "model": args.model,
                "output_length": args.length,
                "citation_format": args.citations,
                "include_domains": _split_domains(args.prefer_domains),
                "exclude_domains": _split_domains(args.block_domains),
                "output_schema": schema,
                "files": files,
            }
        )
        kwargs = {
            k: v for k, v in params.items() if k != "model" and v not in (None, [], {})
        }
        if files_payload:
            kwargs["files"] = files_payload
        response = research_headless(
            query,
            model=params["model"],
            timeout=args.timeout,
            on_progress=None if args.quiet else _progress_printer,
            **kwargs,
        )

    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
