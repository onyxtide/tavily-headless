import argparse
import json

from app import run as run_web
from client import extract_headless, research_headless, search_headless


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
    p_search.add_argument(
        "--no-raw",
        action="store_true",
        help="skip include_raw_content",
    )

    p_extract = sub.add_parser("extract", help="POST /extract, pure scraping")
    p_extract.add_argument("urls", help="comma-separated URLs (max 20)")
    p_extract.add_argument("--format", choices=["markdown", "text"], default="markdown")

    p_research = sub.add_parser(
        "research",
        help="POST /research then poll; raw report pass-through",
    )
    p_research.add_argument("query")
    p_research.add_argument("--model", choices=["mini", "pro", "auto"], default="mini")
    p_research.add_argument("--interval", type=float, default=5)
    p_research.add_argument("--timeout", type=float, default=600)

    p_web = sub.add_parser("web", help="serve the standalone page")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args()

    if args.command == "web":
        run_web(host=args.host, port=args.port)
        return

    if args.command == "search":
        response = search_headless(
            args.query,
            search_depth=args.depth,
            max_results=args.max_results,
            include_raw_content=not args.no_raw,
        )
    elif args.command == "extract":
        response = extract_headless(
            [u.strip() for u in args.urls.split(",") if u.strip()],
            format=args.format,
        )
    elif args.command == "research":
        response = research_headless(
            args.query,
            model=args.model,
            poll_interval=args.interval,
            timeout=args.timeout,
        )

    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
