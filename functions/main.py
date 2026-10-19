import json
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


def _client():
    return TavilyClient(api_key=TAVILY_API_KEY.value)


def _json(obj, status=200):
    body = json.dumps(obj, default=str)
    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = "application/json; charset=utf-8"
    return https_fn.Response(body, status=status, headers=headers)


def _options():
    return https_fn.Response("", status=204, headers=dict(CORS_HEADERS))


@https_fn.on_request(region="us-central1", timeout_sec=120, secrets=[TAVILY_API_KEY])
def api_search(req: https_fn.Request) -> https_fn.Response:
    if req.method == "OPTIONS":
        return _options()
    try:
        body = req.get_json(silent=True) or {}
    except Exception:
        body = {}
    try:
        response = _client().search(
            query=body.get("query", ""),
            search_depth=body.get("depth", "basic"),
            max_results=int(body.get("max_results") or 10),
            include_raw_content=bool(body.get("include_raw", True)),
            include_answer=False,
            include_favicon=True,
        )
        return _json(response)
    except Exception as exc:
        return _json({"error": str(exc)}, status=400)


@https_fn.on_request(region="us-central1", timeout_sec=120, secrets=[TAVILY_API_KEY])
def api_extract(req: https_fn.Request) -> https_fn.Response:
    if req.method == "OPTIONS":
        return _options()
    try:
        body = req.get_json(silent=True) or {}
    except Exception:
        body = {}
    try:
        response = _client().extract(
            urls=body.get("urls", []),
            include_favicon=True,
        )
        return _json(response)
    except Exception as exc:
        return _json({"error": str(exc)}, status=400)


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
            body = req.get_json(silent=True) or {}
        except Exception:
            body = {}
        ref = tasks.document()
        ref.set(
            {
                "status": "pending",
                "query": body.get("query", ""),
                "model": body.get("model", "mini"),
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
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
    data = event.data.to_dict() if event.data else {}
    query = data.get("query", "")
    model = data.get("model", "mini")
    try:
        client = _client()
        created = client.research(query, model=model)
        request_id = created.get("request_id")
        if not request_id:
            raise RuntimeError(f"Research task creation failed: {created}")
        deadline = time.time() + 520
        while True:
            status = client.get_research(request_id)
            state = status.get("status")
            if state == "completed":
                ref.update(
                    {
                        "status": "completed",
                        "result": {
                            "content": status.get("content"),
                            "sources": status.get("sources"),
                            "usage": status.get("usage"),
                        },
                        "finished_at": firestore.SERVER_TIMESTAMP,
                    }
                )
                return
            if state == "failed":
                raise RuntimeError(f"Research task failed: {status}")
            if time.time() > deadline:
                raise TimeoutError(
                    f"Research task {request_id} timed out (last status: {state})"
                )
            time.sleep(5)
    except Exception as exc:
        ref.update(
            {
                "status": "failed",
                "error": str(exc),
                "finished_at": firestore.SERVER_TIMESTAMP,
            }
        )
