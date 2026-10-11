import os
import time

from tavily import TavilyClient


def _load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()


def _get_client():
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY is not set. Put it in a .env file or export it."
        )
    return TavilyClient(api_key=api_key)


def search_headless(
    query,
    search_depth="basic",
    max_results=10,
    include_raw_content=True,
    **extra,
):
    client = _get_client()
    return client.search(
        query=query,
        search_depth=search_depth,
        max_results=max_results,
        include_raw_content=include_raw_content,
        include_answer=False,
        **extra,
    )


def extract_headless(urls, format="markdown", **extra):
    client = _get_client()
    return client.extract(urls=urls, format=format, **extra)


def research_headless(
    query,
    model="mini",
    poll_interval=5,
    timeout=600,
    **extra,
):
    client = _get_client()
    task = client.research(query, model=model, **extra)
    request_id = task.get("request_id")
    if not request_id:
        raise RuntimeError(f"Research task creation failed: {task}")

    deadline = time.time() + timeout
    while True:
        status = client.get_research(request_id)
        state = status.get("status")
        if state == "completed":
            return {
                "request_id": request_id,
                "content": status.get("content"),
                "sources": status.get("sources"),
                "usage": status.get("usage"),
            }
        if state == "failed":
            raise RuntimeError(f"Research task failed: {status}")
        if time.time() > deadline:
            raise TimeoutError(
                f"Research task {request_id} timed out after {timeout}s "
                f"(last status: {state})"
            )
        time.sleep(poll_interval)
