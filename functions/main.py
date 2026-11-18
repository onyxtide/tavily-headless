import base64
import json
import re
import time

from firebase_admin import firestore, initialize_app
from firebase_functions import firestore_fn, https_fn, params
from tavily import TavilyClient

initialize_app_succeeded = False
try:
    initialize_app()
    initialize_app_succeeded = True
except Exception:
    pass

TAVILY_API_KEY = params.SecretParam("TAVILY_API_KEY")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

RESEARCH_MODELS = ("mini", "pro", "auto")
OUTPUT_LENGTHS = ("short", "standard", "long")
CITATION_FORMATS = ("numbered", "mla", "apa", "chicago")
SEARCH_DEPTHS = ("basic", "advanced", "fast", "ultra-fast")
TOPICS = ("general", "news", "finance")
TIME_RANGES = ("day", "week", "month", "year")
EXTRACT_DEPTHS = ("basic", "advanced")
CONTENT_FORMATS = ("markdown", "text")
DOMAIN_MODES = ("restrict", "prefer")
COUNTRIES = (
    "afghanistan", "albania", "algeria", "andorra", "angola", "argentina",
    "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain",
    "bangladesh", "barbados", "belarus", "belgium", "belize", "benin",
    "bhutan", "bolivia", "bosnia and herzegovina", "botswana", "brazil",
    "brunei", "bulgaria", "burkina faso", "burundi", "cambodia", "cameroon",
    "canada", "cape verde", "central african republic", "chad", "chile",
    "china", "colombia", "comoros", "congo", "costa rica", "croatia", "cuba",
    "cyprus", "czech republic", "denmark", "djibouti", "dominican republic",
    "ecuador", "egypt", "el salvador", "equatorial guinea", "eritrea",
    "estonia", "ethiopia", "fiji", "finland", "france", "gabon", "gambia",
    "georgia", "germany", "ghana", "greece", "guatemala", "guinea", "haiti",
    "honduras", "hungary", "iceland", "india", "indonesia", "iran", "iraq",
    "ireland", "israel", "italy", "jamaica", "japan", "jordan", "kazakhstan",
    "kenya", "kuwait", "kyrgyzstan", "latvia", "lebanon", "lesotho",
    "liberia", "libya", "liechtenstein", "lithuania", "luxembourg",
    "madagascar", "malawi", "malaysia", "maldives", "mali", "malta",
    "mauritania", "mauritius", "mexico", "moldova", "monaco", "mongolia",
    "montenegro", "morocco", "mozambique", "myanmar", "namibia", "nepal",
    "netherlands", "new zealand", "nicaragua", "niger", "nigeria",
    "north korea", "north macedonia", "norway", "oman", "pakistan",
    "panama", "paraguay", "peru", "philippines", "poland", "portugal",
    "qatar", "romania", "russia", "rwanda", "saudi arabia", "senegal",
    "serbia", "singapore", "slovakia", "slovenia", "somalia", "south africa",
    "south korea", "south sudan", "spain", "sri lanka", "sudan", "sweden",
    "switzerland", "syria", "tajikistan", "tanzania", "thailand", "togo",
    "trinidad and tobago", "tunisia", "turkey", "turkmenistan", "uganda",
    "ukraine", "united arab emirates", "united kingdom", "united states",
    "uruguay", "uzbekistan", "venezuela", "vietnam", "yemen", "zambia",
    "zimbabwe",
)
MAX_PATTERNS = 20
FILE_EXTENSIONS = (".txt", ".md", ".json")
MAX_FILES = 5
MAX_FILE_WORDS = 80000
MAX_FILE_BYTES = 600000
MAX_RESEARCH_DOMAINS = 20
# Event-driven (Firestore-triggered) functions are capped at 540s by the
# platform. Exit gracefully before that so the task records a failed status
# with guidance instead of being killed mid-write and left "running".
WORKER_DEADLINE_SECONDS = 500
PROGRESS_WRITE_INTERVAL = 2.0
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _client():
    return TavilyClient(api_key=TAVILY_API_KEY.value)


def _json(obj, status=200):
    body = json.dumps(obj, default=str)
    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = "application/json; charset=utf-8"
    return https_fn.Response(body, status=status, headers=headers)


def _options():
    return https_fn.Response("", status=204, headers=dict(CORS_HEADERS))


def _body(req):
    try:
        return req.get_json(silent=True) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Validation (mirrored in client.py for the local server; keep the two in sync)
# ---------------------------------------------------------------------------


def _require_str(value, field, max_len=20000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{field} is too long (max {max_len} characters)")
    return value


def _bool(value, default=False):
    return default if value is None else bool(value)


def _int(value, field, default, lo, hi):
    if value in (None, ""):
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if not lo <= v <= hi:
        raise ValueError(f"{field} must be between {lo} and {hi}")
    return v


def _enum(value, field, allowed, default):
    value = value or default
    if value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return value


def _domains(value, field, limit):
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of domains")
    out = []
    for d in value:
        if not isinstance(d, str):
            raise ValueError(f"{field} must be a list of domains")
        d = d.strip().lower()
        if not d:
            continue
        if "/" in d or " " in d or ":" in d:
            raise ValueError(f"{field}: use bare hosts like example.com (got '{d}')")
        if d not in out:
            out.append(d)
    if len(out) > limit:
        raise ValueError(f"{field}: maximum {limit} domains")
    return out


def _date(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _patterns(value, field, limit=MAX_PATTERNS):
    """Validate a list of regex patterns (crawl/map path & domain filters)."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        value = [p.strip() for p in value.splitlines() if p.strip()]
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of regex patterns")
    out = []
    for p in value:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"{field} must be a list of regex patterns")
        p = p.strip()
        if len(p) > 500:
            raise ValueError(f"{field}: pattern too long (max 500 characters)")
        try:
            re.compile(p)
        except re.error as exc:
            raise ValueError(f"{field}: invalid regex '{p}' ({exc})")
        if p not in out:
            out.append(p)
    if len(out) > limit:
        raise ValueError(f"{field}: maximum {limit} patterns")
    return out


def _search_kwargs(body):
    query = _require_str(body.get("query"), "query", 4000)
    depth = _enum(body.get("depth"), "depth", SEARCH_DEPTHS, "basic")
    topic = _enum(body.get("topic"), "topic", TOPICS, "general")
    raw_format = _enum(body.get("raw_format"), "raw_format", CONTENT_FORMATS, "markdown")
    include_raw = _bool(body.get("include_raw"), True)
    kwargs = {
        "query": query,
        "search_depth": depth,
        "max_results": _int(body.get("max_results"), "max_results", 10, 0, 20),
        "topic": topic,
        "include_raw_content": (
            ("text" if raw_format == "text" else True) if include_raw else False
        ),
        # Headless guarantee: never request an LLM-generated answer.
        "include_answer": False,
        "include_favicon": True,
        "include_usage": True,
        "include_published_date": _bool(
            body.get("include_published_date"), topic == "news"
        ),
    }
    time_range = body.get("time_range")
    if time_range:
        kwargs["time_range"] = _enum(time_range, "time_range", TIME_RANGES, None)
    start = _date(body.get("start_date"), "start_date")
    end = _date(body.get("end_date"), "end_date")
    if start:
        kwargs["start_date"] = start
    if end:
        kwargs["end_date"] = end
    if body.get("max_age_hours") not in (None, ""):
        kwargs["max_age_hours"] = _int(
            body.get("max_age_hours"), "max_age_hours", None, -1, 720
        )
    if _bool(body.get("filter_by_published_date")):
        kwargs["filter_by_published_date"] = True
    inc = _domains(body.get("include_domains"), "include_domains", 300)
    exc = _domains(body.get("exclude_domains"), "exclude_domains", 150)
    if inc:
        kwargs["include_domains"] = inc
        mode = _enum(
            body.get("include_domains_mode"), "include_domains_mode", DOMAIN_MODES, "restrict"
        )
        if mode != "restrict":
            kwargs["include_domains_mode"] = mode
    elif body.get("include_domains_mode"):
        raise ValueError("include_domains_mode requires include_domains")
    if exc:
        kwargs["exclude_domains"] = exc
    country = body.get("country")
    if country:
        if topic != "general":
            raise ValueError("country is only available when topic is general")
        kwargs["country"] = _enum(
            str(country).strip().lower(), "country", COUNTRIES, None
        )
    language = (body.get("language") or "").strip()
    if language:
        if len(language) > 40:
            raise ValueError("language is too long (max 40 characters)")
        kwargs["language"] = language
        if _bool(body.get("filter_by_language")):
            kwargs["filter_by_language"] = True
    elif _bool(body.get("filter_by_language")):
        raise ValueError("filter_by_language requires language")
    if depth != "ultra-fast":
        chunks = _int(body.get("chunks_per_source"), "chunks_per_source", None, 1, 3)
        if chunks:
            kwargs["chunks_per_source"] = chunks
    if _bool(body.get("exact_match")):
        kwargs["exact_match"] = True
    if _bool(body.get("auto_parameters")):
        kwargs["auto_parameters"] = True
    if _bool(body.get("safe_search")):
        if depth in ("fast", "ultra-fast"):
            raise ValueError("safe_search is not supported for fast or ultra-fast depth")
        kwargs["safe_search"] = True
    if _bool(body.get("include_images")):
        kwargs["include_images"] = True
        if _bool(body.get("include_image_descriptions")):
            kwargs["include_image_descriptions"] = True
    return kwargs


def _extract_kwargs(body):
    urls = body.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
        raise ValueError("Provide 1 to 20 URLs")
    urls = [_require_str(u, "url", 2000) for u in urls]
    kwargs = {
        "urls": urls,
        "extract_depth": _enum(
            body.get("extract_depth"), "extract_depth", EXTRACT_DEPTHS, "basic"
        ),
        "format": _enum(body.get("format"), "format", CONTENT_FORMATS, "markdown"),
        "include_favicon": True,
        "include_usage": True,
    }
    focus = (body.get("query") or "").strip()
    if focus:
        kwargs["query"] = focus
        kwargs["chunks_per_source"] = _int(
            body.get("chunks_per_source"), "chunks_per_source", 3, 1, 5
        )
    if _bool(body.get("include_images")):
        kwargs["include_images"] = True
    return kwargs


def _site_filters(body, kwargs):
    """Shared crawl/map regex filters and image inclusion."""
    for field in ("select_paths", "exclude_paths", "select_domains", "exclude_domains"):
        patterns = _patterns(body.get(field), field)
        if patterns:
            kwargs[field] = patterns
    if _bool(body.get("include_images")):
        kwargs["include_images"] = True


def _crawl_kwargs(body):
    kwargs = {
        "url": _require_str(body.get("url"), "url", 2000),
        "max_depth": _int(body.get("max_depth"), "max_depth", 1, 1, 5),
        "max_breadth": _int(body.get("max_breadth"), "max_breadth", 20, 1, 500),
        "limit": _int(body.get("limit"), "limit", 50, 1, 1000),
        "extract_depth": _enum(
            body.get("extract_depth"), "extract_depth", EXTRACT_DEPTHS, "basic"
        ),
        "format": _enum(body.get("format"), "format", CONTENT_FORMATS, "markdown"),
        "allow_external": _bool(body.get("allow_external"), True),
        "include_favicon": True,
        "include_usage": True,
        "timeout": 140,
    }
    instructions = (body.get("instructions") or "").strip()
    if instructions:
        kwargs["instructions"] = instructions
        chunks = _int(body.get("chunks_per_source"), "chunks_per_source", None, 1, 5)
        if chunks:
            kwargs["chunks_per_source"] = chunks
    elif body.get("chunks_per_source") not in (None, ""):
        raise ValueError("chunks_per_source requires instructions")
    _site_filters(body, kwargs)
    return kwargs


def _map_kwargs(body):
    kwargs = {
        "url": _require_str(body.get("url"), "url", 2000),
        "max_depth": _int(body.get("max_depth"), "max_depth", 1, 1, 5),
        "max_breadth": _int(body.get("max_breadth"), "max_breadth", 20, 1, 500),
        "limit": _int(body.get("limit"), "limit", 50, 1, 1000),
        "allow_external": _bool(body.get("allow_external"), True),
        "include_usage": True,
        "timeout": 140,
    }
    instructions = (body.get("instructions") or "").strip()
    if instructions:
        kwargs["instructions"] = instructions
    _site_filters(body, kwargs)
    return kwargs


def _files(files):
    if not files:
        return [], []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError(f"Attach at most {MAX_FILES} files")
    payload, meta = [], []
    total_bytes = total_words = 0
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("Each file needs a name and base64 data")
        name, data = f.get("name"), f.get("data")
        if not isinstance(name, str) or not name.lower().endswith(FILE_EXTENSIONS):
            raise ValueError("Files must be .txt, .md or .json")
        if not isinstance(data, str) or not data:
            raise ValueError(f"{name}: missing base64 data")
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            raise ValueError(f"{name}: invalid base64 data")
        total_bytes += len(raw)
        if total_bytes > MAX_FILE_BYTES:
            raise ValueError("Attached files exceed 600 KB combined")
        words = len(raw.decode("utf-8", errors="replace").split())
        total_words += words
        if total_words > MAX_FILE_WORDS:
            raise ValueError("Attached files exceed 80,000 words combined")
        payload.append({"name": name, "data": data, "type": "base64"})
        meta.append({"name": name, "words": words, "bytes": len(raw)})
    return payload, meta


def _output_schema(schema):
    if schema in (None, "", {}):
        return None
    if not isinstance(schema, dict):
        raise ValueError("output_schema must be a JSON object")
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise ValueError("output_schema needs a non-empty 'properties' object")
    for key, spec in props.items():
        if not isinstance(spec, dict) or spec.get("type") not in (
            "object",
            "string",
            "integer",
            "number",
            "array",
        ):
            raise ValueError(
                f"output_schema.properties.{key}: type must be object, string, "
                "integer, number or array"
            )
        if not isinstance(spec.get("description"), str) or not spec["description"].strip():
            raise ValueError(f"output_schema.properties.{key} needs a description")
    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or not required
            or any(r not in props for r in required)
        ):
            raise ValueError("output_schema.required must list defined property names")
    out = {"properties": props}
    if required:
        out["required"] = required
    return out


def _research_payload(body):
    query = _require_str(body.get("query"), "query")
    params = {
        "model": _enum(body.get("model"), "model", RESEARCH_MODELS, "auto"),
        "output_length": _enum(
            body.get("output_length"), "output_length", OUTPUT_LENGTHS, "standard"
        ),
        "citation_format": _enum(
            body.get("citation_format"), "citation_format", CITATION_FORMATS, "numbered"
        ),
        "include_domains": _domains(
            body.get("include_domains"), "include_domains", MAX_RESEARCH_DOMAINS
        ),
        "exclude_domains": _domains(
            body.get("exclude_domains"), "exclude_domains", MAX_RESEARCH_DOMAINS
        ),
        "output_schema": _output_schema(body.get("output_schema")),
    }
    files_payload, files_meta = _files(body.get("files"))
    return query, params, files_payload, files_meta


# ---------------------------------------------------------------------------
# SSE parsing for research streaming (mirrored in client.py)
# ---------------------------------------------------------------------------


def _sse_events(chunks):
    """Yield (event_name, parsed_json_or_None) from raw SSE byte chunks."""
    buf = b""

    def parse_block(block):
        name, data_lines = "message", []
        for line in block.split(b"\n"):
            line = line.decode("utf-8", errors="replace").rstrip("\r")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return (name, None) if name == "done" else None
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            return "done", None
        try:
            return name, json.loads(raw)
        except Exception:
            return name, None

    for chunk in chunks:
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            parsed = parse_block(block)
            if parsed:
                yield parsed
    if buf.strip():
        parsed = parse_block(buf)
        if parsed:
            yield parsed


def _new_progress():
    return {
        "phase": "planning",
        "searches": 0,
        "queries": [],
        "sources_count": 0,
        "sources": [],
        "subtopics": [],
        "events": [],
    }


def _add_source(progress, source):
    if not isinstance(source, dict):
        return
    url = source.get("url")
    if not isinstance(url, str) or not url:
        return
    if any(s.get("url") == url for s in progress["sources"]):
        return
    progress["sources_count"] += 1
    if len(progress["sources"]) < 60:
        progress["sources"].append(
            {
                "url": url,
                "title": source.get("title") or url,
                "favicon": source.get("favicon"),
            }
        )


def _event(progress, started, name, text):
    if not text:
        return
    progress["events"].append(
        {"t": round(time.time() - started, 1), "name": name, "text": text}
    )
    if len(progress["events"]) > 40:
        progress["events"] = progress["events"][-40:]


def _apply_delta(progress, started, delta, acc):
    tc = delta.get("tool_calls")
    if isinstance(tc, dict):
        kind = tc.get("type")
        if kind == "tool_call":
            for call in tc.get("tool_call") or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name") or ""
                args = call.get("arguments") or ""
                if name == "WebSearch":
                    progress["phase"] = "searching"
                    progress["searches"] += 1
                    for q in call.get("queries") or []:
                        if (
                            isinstance(q, str)
                            and q not in progress["queries"]
                            and len(progress["queries"]) < 40
                        ):
                            progress["queries"].append(q)
                elif name == "ResearchSubtopic":
                    progress["phase"] = "deep_dive"
                    if (
                        args
                        and args not in progress["subtopics"]
                        and len(progress["subtopics"]) < 20
                    ):
                        progress["subtopics"].append(args)
                elif name == "Generating":
                    progress["phase"] = "writing"
                _event(progress, started, name, args)
        elif kind == "tool_response":
            for resp in tc.get("tool_response") or []:
                if not isinstance(resp, dict):
                    continue
                for s in resp.get("sources") or []:
                    _add_source(progress, s)
                _event(progress, started, resp.get("name") or "", resp.get("arguments") or "")
    content = delta.get("content")
    if isinstance(content, str) and content:
        acc["parts"].append(content)
        progress["phase"] = "writing"
    elif isinstance(content, dict):
        acc["structured"] = content
        progress["phase"] = "writing"
    sources = delta.get("sources")
    if isinstance(sources, list):
        acc["sources"] = sources
        for s in sources:
            _add_source(progress, s)


def _poll_research(client, request_id, started, deadline, on_tick=None):
    """Canonical finish: poll until completed, failed or the deadline passes."""
    failures = 0
    while True:
        try:
            status = client.get_research(request_id, include_usage=True)
            failures = 0
        except Exception:
            # The result record can lag the stream's [DONE] by a few seconds
            # (transient 404). Retry briefly before giving up.
            failures += 1
            if failures >= 4 or time.time() - started > deadline:
                raise
            time.sleep(2)
            continue
        state = status.get("status")
        if state == "completed":
            return status
        if state == "failed":
            raise RuntimeError(f"Research task failed: {status.get('error') or state}")
        if time.time() - started > deadline:
            raise TimeoutError(
                f"Research task {request_id} timed out (last status: {state})"
            )
        if on_tick:
            on_tick()
        time.sleep(5)


def _research_stream(client, query, model, extra):
    """POST /research with stream=True via the SDK's authenticated session.

    The SDK's research(stream=True) hides the response object, but its
    x-request-id header is the task's canonical request id — the per-chunk
    payload ids are NOT retrievable via GET /research/{id}. Capturing the
    header is what makes canonical finish (with usage) and drop recovery
    possible. Returns (request_id_or_None, response).
    """
    data = {
        "input": query,
        "model": model,
        "stream": True,
        "output_schema": extra.get("output_schema"),
        "citation_format": extra.get("citation_format", "numbered"),
    }
    data.update(
        {k: v for k, v in extra.items() if k not in ("output_schema", "citation_format")}
    )
    data = {k: v for k, v in data.items() if v is not None}
    resp = client.session.post(
        client.base_url + "/research",
        data=json.dumps(data),
        stream=True,
        # Read timeout bounds silent stalls; a timeout mid-stream falls back
        # to polling with the captured request id.
        timeout=(10, 120),
    )
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        resp.close()
        raise RuntimeError(f"Research request failed ({resp.status_code}): {detail}")
    rid = resp.headers.get("x-request-id")
    request_id = rid if isinstance(rid, str) and UUID_RE.match(rid) else None
    return request_id, resp


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def _sync_endpoint(req, build_kwargs, call):
    if req.method == "OPTIONS":
        return _options()
    try:
        kwargs = build_kwargs(_body(req))
        return _json(call(**kwargs))
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        return _json({"error": str(exc)}, status=400)


@https_fn.on_request(region="us-central1", timeout_sec=120, secrets=[TAVILY_API_KEY])
def api_search(req: https_fn.Request) -> https_fn.Response:
    return _sync_endpoint(req, _search_kwargs, lambda **kw: _client().search(**kw))


@https_fn.on_request(region="us-central1", timeout_sec=120, secrets=[TAVILY_API_KEY])
def api_extract(req: https_fn.Request) -> https_fn.Response:
    return _sync_endpoint(req, _extract_kwargs, lambda **kw: _client().extract(**kw))


@https_fn.on_request(region="us-central1", timeout_sec=300, secrets=[TAVILY_API_KEY])
def api_crawl(req: https_fn.Request) -> https_fn.Response:
    return _sync_endpoint(req, _crawl_kwargs, lambda **kw: _client().crawl(**kw))


@https_fn.on_request(region="us-central1", timeout_sec=300, secrets=[TAVILY_API_KEY])
def api_map(req: https_fn.Request) -> https_fn.Response:
    return _sync_endpoint(req, _map_kwargs, lambda **kw: _client().map(**kw))


@https_fn.on_request(region="us-central1", timeout_sec=120, secrets=[TAVILY_API_KEY])
def api_research(req: https_fn.Request) -> https_fn.Response:
    if req.method == "OPTIONS":
        return _options()
    db = firestore.client()
    tasks = db.collection("research_tasks")
    if req.method == "GET":
        task_id = req.args.get("id")
        if not task_id:
            return _json({"error": "missing id"}, status=400)
        doc = tasks.document(task_id).get()
        if not doc.exists:
            return _json({"error": "unknown task"}, status=404)
        return _json(doc.to_dict())
    if req.method == "POST":
        try:
            query, params, files_payload, files_meta = _research_payload(_body(req))
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        ref = tasks.document()
        try:
            # Files live in their own document: base64 payloads can approach
            # Firestore's 1 MiB per-document limit, and the task doc must stay
            # small because progress updates rewrite it frequently.
            if files_payload:
                db.collection("research_files").document(ref.id).set(
                    {"files": files_payload}
                )
            ref.set(
                {
                    "status": "pending",
                    "query": query,
                    "model": params["model"],
                    "params": params,
                    "files_meta": files_meta,
                    "created_at": firestore.SERVER_TIMESTAMP,
                }
            )
        except Exception as exc:
            return _json({"error": str(exc)}, status=500)
        return _json({"request_id": ref.id}, status=201)
    return _json({"error": "method not allowed"}, status=405)


@firestore_fn.on_document_created(
    document="research_tasks/{taskId}",
    region="us-central1",
    timeout_sec=540,
    memory=512,
    secrets=[TAVILY_API_KEY],
)
def research_worker(event: firestore_fn.Event) -> None:
    task_id = event.params["taskId"]
    db = firestore.client()
    ref = db.collection("research_tasks").document(task_id)
    files_ref = db.collection("research_files").document(task_id)
    data = event.data.to_dict() if event.data else {}
    query = data.get("query", "")
    params = data.get("params") or {}
    model = params.get("model") or data.get("model") or "auto"
    has_files = bool(data.get("files_meta"))
    started = time.time()
    progress = _new_progress()
    acc = {"parts": [], "structured": None, "sources": None}
    last_write = 0.0

    def flush(force=False):
        nonlocal last_write
        now = time.time()
        if not force and now - last_write < PROGRESS_WRITE_INTERVAL:
            return
        last_write = now
        snapshot = dict(progress)
        # Live draft: the page renders the report while the agent writes it.
        if acc["structured"] is not None:
            snapshot["draft"] = json.dumps(acc["structured"], indent=2, default=str)[
                :200000
            ]
        elif acc["parts"]:
            snapshot["draft"] = "".join(acc["parts"])[:200000]
        try:
            ref.update(
                {"status": "running", "phase": progress["phase"], "progress": snapshot}
            )
        except Exception:
            pass

    try:
        kwargs = {
            k: v for k, v in params.items() if k != "model" and v not in (None, [], {})
        }
        if has_files:
            snap = files_ref.get()
            if snap.exists:
                kwargs["files"] = (snap.to_dict() or {}).get("files") or []
        client = _client()
        flush(True)
        request_id = None
        try:
            request_id, resp = _research_stream(client, query, model, kwargs)

            def chunks():
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    resp.close()

            for name, payload in _sse_events(chunks()):
                if time.time() - started > WORKER_DEADLINE_SECONDS:
                    raise TimeoutError(
                        f"Research exceeded the hosted worker limit "
                        f"({WORKER_DEADLINE_SECONDS}s). Try a shorter output_length "
                        f"or the mini model; the local server (python cli.py web) "
                        f"allows up to 900s."
                    )
                if name == "done":
                    break
                if not isinstance(payload, dict):
                    continue
                if payload.get("object") == "error":
                    raise RuntimeError(
                        str(payload.get("error") or "research stream error")
                    )
                delta = ((payload.get("choices") or [{}])[0]).get("delta") or {}
                _apply_delta(progress, started, delta, acc)
                flush()
        except Exception:
            # Stream dropped mid-task: fall back to polling if we captured the
            # request id; otherwise the task cannot be recovered.
            if request_id is None:
                raise
            _event(progress, started, "Stream", "Connection dropped; polling for result")
            flush(True)

        result = None
        if request_id:
            try:
                result = _poll_research(
                    client, request_id, started, WORKER_DEADLINE_SECONDS, on_tick=flush
                )
            except Exception as exc:
                # Last resort: if the result endpoint is unreachable but the
                # stream already delivered a report, pass that through rather
                # than failing a task that actually succeeded.
                if acc["structured"] is None and not acc["parts"]:
                    raise
                _event(
                    progress, started, "Stream",
                    f"Result endpoint unavailable ({str(exc)[:120]}); using streamed report",
                )
        if result is None:
            content = (
                acc["structured"]
                if acc["structured"] is not None
                else "".join(acc["parts"])
            )
            if not content:
                raise RuntimeError("Research stream ended without a report")
            result = {
                "content": content,
                "sources": acc["sources"] or progress["sources"],
                "usage": None,
            }
        progress["phase"] = "done"
        flush(True)
        final_progress = {k: v for k, v in progress.items() if k != "draft"}
        ref.update(
            {
                "status": "completed",
                "progress": final_progress,
                "result": {
                    "content": result.get("content"),
                    "sources": result.get("sources"),
                    "usage": result.get("usage"),
                    "request_id": result.get("request_id") or request_id,
                },
                "finished_at": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception as exc:
        try:
            ref.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "finished_at": firestore.SERVER_TIMESTAMP,
                }
            )
        except Exception:
            pass
    finally:
        if has_files:
            try:
                files_ref.delete()
            except Exception:
                pass
