"""
HTTP surface for the Knowledge Graph tab — every /api/kg/* endpoint of the implementation
contract (§D), and nothing else.

The whole feature is pure Python + networkx: `ontology.py` owns the model (TBox) and the
materializer that compiles it into an instance graph (ABox), and the query layer — slot index,
DSL, analytics — is resolved by CAPABILITY, from `kg_query.py` if that module exists and
otherwise from `ontology.py` itself, so the split between those two files can move without
touching a route. Vespa is not reached by ANY code path here: the tab has to keep answering with
:8080 completely down, which is half the reason it exists.

This module is a thin, opinionated seam:
  * it decides HTTP status from an error CODE, never from an exception type leaking upward, so
    every failure arrives as an envelope the UI can render instead of a stack trace,
  * it holds the process-wide materialized-graph cache, keyed by the same three hashes §B.8 says
    invalidate a build — re-reading an 815-node graph per keystroke of the typeahead is ~30x the
    cost of serving it, and
  * it is the ONE place the compact binding string the builder canvas writes ("field:cuisine")
    meets the object binding form the model file stores.

Mounted by server/main.py:
    from kg_api import router as kg_router
    app.include_router(kg_router)
CORS is installed globally on the app there; this module deliberately adds no middleware.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

# uvicorn runs main.py with cwd=server/, so siblings import by bare name; the package form is
# kept working so `from server import kg_api` (tests, notebooks) resolves the same modules.
try:  # pragma: no cover - import shape depends on how the app was launched
    import ontology  # type: ignore
except ImportError:  # noqa: BLE001
    try:
        from server import ontology  # type: ignore
    except ImportError:  # noqa: BLE001
        ontology = None  # type: ignore

try:  # pragma: no cover
    import kg_query  # type: ignore
except ImportError:  # noqa: BLE001
    try:
        from server import kg_query  # type: ignore
    except ImportError:  # noqa: BLE001
        kg_query = None  # type: ignore

router = APIRouter(prefix="/api/kg")


# ---------------------------------------------------------------- errors

class KgError(Exception):
    """A failure the UI is expected to RENDER, not a crash. Carries the wire envelope."""

    def __init__(self, code: str, message: str, *, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail


# ontology.OntologyError codes -> HTTP. Anything unmapped is a 400: a request we could not honour
# is the caller's problem to fix, and a 500 would make the UI show "the sidecar is down" wrongly.
_STATUS = {
    "NOT_FOUND": 404, "BAD_ID": 400, "INVALID_MODEL": 400, "INVALID_JSON": 400,
    "INVALID_QUERY": 400,
    "DATASET_MISSING": 409, "READ_ONLY": 400, "TOO_LARGE": 400,
    "NOT_MATERIALIZED": 409, "STALE": 409, "BAD_REQUEST": 400,
    "KG_UNAVAILABLE": 503, "INTERNAL": 500,
}


def _envelope(code: str, message: str, status: int, detail: Any = None) -> JSONResponse:
    err: dict = {"code": code, "message": message}
    if detail is not None:
        err["detail"] = detail
    return JSONResponse(status_code=status, content={"ok": False, "error": err})


def _guarded(fn: Callable) -> Callable:
    """Wrap a handler so no failure ever reaches the client as a stack trace. FastAPI reads the
    signature through functools.wraps' __wrapped__, so the query/path params still bind."""

    def _convert(exc: Exception) -> JSONResponse:
        if isinstance(exc, KgError):
            return _envelope(exc.code, exc.message, exc.status, exc.detail)
        if ontology is not None and isinstance(exc, getattr(ontology, "OntologyError", ())):
            code = getattr(exc, "code", "INTERNAL")
            return _envelope(code, getattr(exc, "message", str(exc)),
                             _STATUS.get(code, 400), getattr(exc, "detail", None) or None)
        # Genuinely unexpected. Still an envelope — .kgr-err renders it; the traceback goes to the
        # server log where it belongs.
        import traceback
        traceback.print_exc()
        return _envelope("INTERNAL", f"{type(exc).__name__}: {exc}", 500)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def ainner(*a, **kw):
            try:
                return await fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001 — deliberate: every failure becomes an envelope
                return _convert(exc)
        return ainner

    @functools.wraps(fn)
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            return _convert(exc)
    return inner


def _need_ontology():
    if ontology is None:
        raise KgError("KG_UNAVAILABLE",
                      "server/ontology.py could not be imported — the Knowledge Graph runs "
                      "entirely in Python on :8009; Vespa is not involved.", status=503)
    return ontology


# The query layer is normally server/kg_query.py, but it is also valid for it to live inside
# ontology.py — the router only cares that SOMETHING exposes build_index/parse/execute, so it is
# resolved by capability rather than by filename and cached on first use.
_QUERY_MOD: Any = None
_QUERY_API = ("build_index", "parse", "execute", "typeahead", "list_queries")


def _need_query():
    global _QUERY_MOD
    if _QUERY_MOD is not None:
        return _QUERY_MOD
    for candidate in (kg_query, ontology):
        if candidate is not None and all(hasattr(candidate, fn) for fn in _QUERY_API):
            _QUERY_MOD = candidate
            return _QUERY_MOD
    raise KgError("KG_UNAVAILABLE",
                  "the Knowledge Graph query layer could not be imported — it runs entirely in "
                  "Python on :8009; Vespa is not involved.", status=503)


async def _body(request: Request) -> dict:
    """POST bodies are parsed by hand rather than via a pydantic model: a malformed draft coming
    off the canvas must come back as our own INVALID_JSON envelope, not FastAPI's 422 shape,
    which the UI has no renderer for."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise KgError("INVALID_JSON", f"request body is not valid JSON: {exc}")
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise KgError("INVALID_JSON", "request body must be a JSON object")
    return obj


# ---------------------------------------------------------------- caches

# model_id -> (fingerprint, graph, model). The fingerprint is model_hash + dataset_hash +
# BUILDER_VERSION: exactly the three inputs §B.8 says invalidate a build, so a cache hit is
# provably the graph the current model would produce. Dragging a node does not evict it.
_GRAPHS: dict[str, tuple[str, Any, dict]] = {}
_INDEXES: dict[str, tuple[str, Any]] = {}
_PALETTE: dict[str, Any] = {}


def _invalidate(model_id: str | None = None) -> None:
    """Explicit eviction on every write. Staleness is also checked per request, but a write we
    performed ourselves should never make the next reader pay for a re-hash to discover it."""
    if model_id is None:
        _GRAPHS.clear(); _INDEXES.clear(); _PALETTE.clear()
        return
    _GRAPHS.pop(model_id, None)
    _INDEXES.pop(model_id, None)


def _fingerprint(model: dict) -> str:
    o = _need_ontology()
    return f"{o.model_hash(model)}:{o.dataset_hash()}:{getattr(o, 'BUILDER_VERSION', 0)}"


def _model_id(model: str = "") -> str:
    o = _need_ontology()
    return (model or "").strip() or o.active_model_id()


def _load_model(model_id: str) -> dict:
    o = _need_ontology()
    if model_id == "default":
        return json.loads(json.dumps(o.DEFAULT_MODEL))
    return o.load_model(model_id)


def _graph_for(model_id: str) -> tuple[Any, dict]:
    """The materialized graph, cached. `load_materialized` builds it on a cache miss on disk, so a
    freshly cloned repo answers the first query correctly instead of 409-ing at the user."""
    o = _need_ontology()
    model = _load_model(model_id)
    fp = _fingerprint(model)
    hit = _GRAPHS.get(model_id)
    if hit is not None and hit[0] == fp:
        return hit[1], hit[2]
    g, _meta = o.load_materialized(model_id, rebuild_if_stale=True, now=o.utcnow_iso())
    if g is None:
        raise KgError("NOT_MATERIALIZED",
                      f'Model "{model_id}" has no instance graph yet. Save & materialize it first.',
                      status=409, detail={"model_id": model_id})
    _GRAPHS[model_id] = (fp, g, model)
    return g, model


def _index_for(model_id: str) -> Any:
    """Prefer kg_query's own cache (it revalidates per request); fall back to building the index
    off our cached graph so a partial query layer still serves the explorer."""
    q = _need_query()
    try:
        return q.get_index(model_id)
    except Exception:  # noqa: BLE001 — fall through to the local build
        pass
    g, model = _graph_for(model_id)
    fp = _fingerprint(model)
    hit = _INDEXES.get(model_id)
    if hit is not None and hit[0] == fp:
        return hit[1]
    idx = q.build_index(g, model)
    _INDEXES[model_id] = (fp, idx)
    return idx


def _index_version(model_id: str) -> str | None:
    try:
        return getattr(_index_for(model_id), "version", None)
    except Exception:  # noqa: BLE001 — a missing index must not break the explorer
        return None


# ---------------------------------------------------------------- small shared helpers

def _csv(value: Any) -> list[str]:
    """Comma-separated params (tags=, keep=, via=) arrive as one string from the browser and as a
    real list from a saved query's params blob. Accept both."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain(obj: Any) -> Any:
    """Slots are frozen dataclasses; the rest of the payloads are already plain. Drops None-valued
    keys so a measure slot does not ship `relation: null` and an entity slot does not ship
    `unit: null` — §D shows each kind carrying only its own fields."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    return obj


_QUOTE_CHARS = ' \t"\'|()'


def _insert_form(value: str) -> str:
    """Quoting lives on the SERVER (§D.3): the client concatenates `insert` blindly, so a value
    holding a space, a bar or a paren must come back already quoted or it would produce a string
    the tokenizer cannot parse (`cuisine:Salads & Bowls` -> three terms)."""
    s = str(value)
    if any(c in s for c in _QUOTE_CHARS):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def _match_tier(value: str, prefix: str) -> str:
    if not prefix:
        return "prefix"
    v, p = value.casefold(), prefix.casefold()
    if v == p:
        return "exact"
    if v.startswith(p):
        return "prefix"
    if any(tok.startswith(p) for tok in v.replace("-", " ").split()):
        return "token"
    return "substring"


def _doc_tag(model: dict) -> str:
    for et in model.get("entity_types") or []:
        if ((et.get("binding") or {}) if isinstance(et.get("binding"), dict) else {}).get("source") == "doc":
            return et.get("tag", "dish")
    return "dish"


def _rel_labels(model: dict) -> dict[str, str]:
    return {r.get("rel"): r.get("label", "") for r in (model.get("relation_types") or []) if r.get("rel")}


def _node_view(g: Any, nid: str) -> dict:
    d = g.nodes[nid] if nid in g else {}
    kind = d.get("kind") or nid.partition(":")[0]
    return {"id": nid, "kind": kind, "label": d.get("label") or d.get("name") or nid.partition(":")[2],
            "n": int(d.get("n", 1) or 1)}


# ---------------------------------------------------------------- binding compaction (§D.1)

_BINDING_SOURCES = {"row": "doc", "field": "field", "list": "list_field", "derive": "derived"}


def _collapse_binding(binding: Any, tag: str = "") -> str:
    """Object form -> the compact string the builder writes. `derive:<tag>` is preferred over
    `derive:<spec kind>` when a registry deriver of that name exists, so DEFAULT_MODEL's
    spec-driven price_band round-trips to the palette entry the user would drop on the canvas."""
    if not isinstance(binding, dict) or not binding:
        return "none"
    src = binding.get("source")
    if src == "doc":
        return "row"
    if src == "field":
        return f"field:{binding.get('field', '')}"
    if src == "list_field":
        return f"list:{binding.get('field', '')}"
    if src == "derived":
        if binding.get("deriver"):
            return f"derive:{binding['deriver']}"
        derivers = getattr(ontology, "DERIVERS", {}) or {}
        if tag and tag in derivers:
            return f"derive:{tag}"
        return f"derive:{(binding.get('spec') or {}).get('kind', 'spec')}"
    return "none"


def _expand_binding(compact: str, tag: str = "") -> dict:
    """Compact string -> object form, applied on save. An unrecognised string yields {} on
    purpose: the validator's MISSING_BINDING message is far more useful than a guess."""
    s = (compact or "").strip()
    head, _, rest = s.partition(":")
    src = _BINDING_SOURCES.get(head)
    if src == "doc":
        doc = ((getattr(ontology, "DEFAULT_MODEL", {}) or {}).get("entity_types") or [{}])[0].get("binding") or {}
        return {"source": "doc", "id_field": doc.get("id_field", "id"),
                "label_field": doc.get("label_field", "name"),
                "payload_fields": list(doc.get("payload_fields") or ["id", "name", "description"])}
    if src in ("field", "list_field"):
        return {"source": src, "field": rest}
    if src == "derived":
        # an unregistered name still round-trips: UNKNOWN_DERIVER from the validator names the
        # problem far better than a binding we silently dropped on the floor
        return {"source": "derived", "deriver": rest or tag}
    return {}


def _model_in(body: dict) -> dict:
    """Normalize a model document coming off the wire: expand compact bindings and drop the two
    server-owned fields, so a client that echoes back what we sent it cannot forge a build."""
    if not isinstance(body, dict) or not body:
        raise KgError("INVALID_MODEL", "expected a model document in the request body")
    m = json.loads(json.dumps(body))  # deep copy; it came off the wire so it is JSON-safe
    for et in m.get("entity_types") or []:
        if isinstance(et, dict) and isinstance(et.get("binding"), str):
            et["binding"] = _expand_binding(et["binding"], et.get("tag", ""))
    m.pop("materialized", None)
    m.pop("stats", None)
    return m


# staleness reasons that mean "there is no graph on disk at all"; every other reason means a
# graph exists but no longer matches the model, the data or the builder — still materialized.
_NO_GRAPH = ("never built", "the model id is not valid", "the saved graph is unreadable",
             "the ontology model is missing")


def _decorate(model: dict, model_id: str | None = None) -> dict:
    """`materialized` is a read-time server annotation (§B.1), never a value the client persists,
    so every model that leaves this module gets it stamped from what is actually on disk."""
    o = _need_ontology()
    mid = model_id or model.get("id") or ""
    out = dict(model)
    out.setdefault("stats", None)
    try:
        stale, reason = o.is_stale(mid)
        out["materialized"] = not (stale and reason in _NO_GRAPH)
    except Exception:  # noqa: BLE001 — an unreadable cache just means "not built"
        out["materialized"] = False
    return out


# ---------------------------------------------------------------- §D.1 builder / model


def _field_kinds() -> tuple[list[str], list[str]]:
    """scalar vs list field inventory for the palette. `dataset_fields` is the documented helper;
    its per-field shape is not pinned by the contract, so an unusable answer falls back to a
    direct scan of the corpus rather than guessing a key name."""
    o = _need_ontology()
    scalar, listy = [], []
    try:
        info = o.dataset_fields()
        for name, meta in (info or {}).items():
            if not isinstance(meta, dict):
                raise TypeError(name)
            flag = meta.get("list", meta.get("is_list", meta.get("multi")))
            if flag is None:
                kind = str(meta.get("kind") or meta.get("type") or "")
                if not kind:
                    raise TypeError(name)
                flag = "list" in kind or "array" in kind
            (listy if flag else scalar).append(name)
        if scalar or listy:
            return sorted(scalar), sorted(listy)
    except Exception:  # noqa: BLE001
        scalar, listy = [], []
    seen_scalar, seen_list = set(), set()
    for i, fields in enumerate(_iter_fields()):
        if i >= 200:
            break
        for k, v in fields.items():
            (seen_list if isinstance(v, list) else seen_scalar).add(k)
    return sorted(seen_scalar - seen_list), sorted(seen_list)


def _iter_fields() -> Iterable[dict]:
    """dishes.jsonl rows are {"id":…, "fields":{…}}; iter_docs is expected to hand back the inner
    projection, but unwrap defensively so both shapes work."""
    o = _need_ontology()
    for doc in o.iter_docs():
        if isinstance(doc, dict) and isinstance(doc.get("fields"), dict):
            yield doc["fields"]
        elif isinstance(doc, dict):
            yield doc


def _entity_values(et: dict) -> list[str]:
    """Every distinct value an entity type would produce over the whole corpus — 600 docs is a
    handful of milliseconds, and the palette's value counts are the thing that makes a 540/600
    diet tag read as visibly weak instead of silently useless."""
    o = _need_ontology()
    binding = et.get("binding") or {}
    src = binding.get("source")
    norm = et.get("normalize")
    out: list[str] = []
    seen: set[str] = set()
    if src == "doc":
        idf = binding.get("id_field", "id")
        return [str(f[idf]) for f in _iter_fields() if idf in f]
    deriver = None
    if src == "derived":
        deriver = o.resolve_deriver(binding)
    field = binding.get("field", "")
    for fields in _iter_fields():
        if src == "derived":
            vals = o.derive(deriver, fields) or []
        else:
            raw = fields.get(field)
            vals = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
        for v in vals:
            key = o._norm(v, norm) if hasattr(o, "_norm") else str(v).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(str(v))
    return out


@router.get("/palette")
@_guarded
def kg_palette():
    """The builder's drag palette: every entity type the built-in ontology knows how to bind,
    with a live value count so the user sees the size of what they are dropping."""
    o = _need_ontology()
    key = o.dataset_hash()
    if _PALETTE.get("key") == key:
        return _PALETTE["value"]
    types = []
    for et in (o.DEFAULT_MODEL.get("entity_types") or []):
        tag = et.get("tag", "")
        try:
            vals = _entity_values(et)
        except Exception:  # noqa: BLE001 — one broken binding must not empty the palette
            vals = []
        types.append({
            "tag": tag, "label": et.get("label", tag),
            "plural": et.get("plural") or (et.get("label", tag) + "s"),
            "color": et.get("color", "#7c8698"), "icon": et.get("icon", ""),
            "binding": _collapse_binding(et.get("binding"), tag),
            "values": len(vals), "samples": [str(v) for v in vals[:3]],
        })
    scalar, listy = _field_kinds()
    payload = {
        "ok": True,
        "types": types,
        "fields": {"scalar": scalar, "list": listy,
                   "derive": sorted((getattr(o, "DERIVERS", {}) or {}).keys())},
        "swatches": [et.get("color") for et in (o.DEFAULT_MODEL.get("entity_types") or [])
                     if et.get("color")],
    }
    _PALETTE.clear()
    _PALETTE.update({"key": key, "value": payload})
    return payload


def _summary(entry: dict) -> dict:
    """One row of GET /models (§D.1). `list_models` already computes most of this; we only fill
    what it left out, because overriding a correct upstream answer is how the picker starts lying.
    Reads the meta sidecar at most — never a graph, since this runs on every visit to the tab."""
    o = _need_ontology()
    mid = entry.get("id", "")
    out = {"id": mid, "name": entry.get("name", mid), "description": entry.get("description", ""),
           "version": int(entry.get("version", 1) or 1),
           "created_at": entry.get("created_at", ""), "updated_at": entry.get("updated_at", ""),
           "builtin": bool(entry.get("builtin", False))}
    for key in ("entity_types", "relation_types"):
        v = entry.get(key)
        out[key] = v if isinstance(v, int) else len(v or [])
    if all(k in entry for k in ("materialized", "stale", "stale_reason", "nodes", "edges")):
        out.update({k: entry[k] for k in ("materialized", "stale", "stale_reason", "nodes", "edges")})
        return out
    meta = {}
    try:
        p = o.GRAPHS_DIR / f"{mid}.meta.json"
        if p.exists():
            meta = json.loads(p.read_text()) or {}
    except Exception:  # noqa: BLE001 — an unreadable sidecar just means "never built"
        meta = {}
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else meta
    try:
        stale, reason = o.is_stale(mid)
    except Exception:  # noqa: BLE001
        stale, reason = True, "never built"
    out["materialized"] = bool(entry.get("materialized", not (stale and reason in _NO_GRAPH)))
    out["stale"] = bool(stale) if out["materialized"] else False
    out["stale_reason"] = reason if out["stale"] else None
    out["nodes"] = (stats or {}).get("nodes")
    out["edges"] = (stats or {}).get("edges")
    return out


@router.get("/models")
@_guarded
def kg_models():
    o = _need_ontology()
    active = o.active_model_id()
    rows = []
    for entry in o.list_models():
        row = _summary(entry if isinstance(entry, dict) else {"id": str(entry)})
        row["active"] = row["id"] == active
        rows.append(row)
    return {"ok": True, "active": active, "models": rows}


@router.get("/model/default")
@_guarded
def kg_model_default():
    """The pristine constant, never the on-disk shadow — this is what the builder's Reset uses."""
    o = _need_ontology()
    return {"ok": True, "model": json.loads(json.dumps(o.DEFAULT_MODEL))}


@router.get("/model/{model_id}")
@_guarded
def kg_model_get(model_id: str):
    return {"ok": True, "model": _decorate(_load_model(model_id), model_id)}


@router.post("/model")
@_guarded
async def kg_model_save(request: Request):
    """Save ONLY. The UI's "Save & materialize" calls this then /materialize, so a build failure
    never costs the user their canvas (§C.3)."""
    o = _need_ontology()
    model = _model_in(await _body(request))
    validation = o.validate_model(model)
    if not validation.get("ok", False):
        raise KgError("INVALID_MODEL", "the ontology model has errors that block saving",
                      status=400, detail=validation)
    saved = o.save_model(model, now=o.utcnow_iso())
    _invalidate(saved.get("id") or model.get("id"))
    return {"ok": True, "model": _decorate(saved), "validation": validation}


@router.post("/model/{model_id}/validate")
@_guarded
async def kg_model_validate(model_id: str, request: Request):
    """Never 4xx on a bad model — the canvas draws the errors from the payload, so the payload has
    to arrive even when (especially when) the draft is broken."""
    o = _need_ontology()
    body = await _body(request)
    model = body if body else _load_model(model_id)
    if isinstance(model.get("entity_types"), list):
        for et in model["entity_types"]:
            if isinstance(et, dict) and isinstance(et.get("binding"), str):
                et["binding"] = _expand_binding(et["binding"], et.get("tag", ""))
    return {"ok": True, "validation": o.validate_model(model)}


@router.post("/model/{model_id}/materialize")
@_guarded
async def kg_model_materialize(model_id: str, request: Request):
    """Save + compile the schema into the instance graph, in one call. Save first so the persisted
    model_hash is the one the build is stamped with; otherwise the graph is born stale."""
    o = _need_ontology()
    body = await _body(request)
    model = _model_in(body) if body else _load_model(model_id)
    validation = o.validate_model(model)
    if not validation.get("ok", False):
        raise KgError("INVALID_MODEL", "the ontology model has errors that block materializing",
                      status=400, detail=validation)
    now = o.utcnow_iso()
    saved = o.save_model(model, now=now) if body else model
    _invalidate(saved.get("id") or model_id)
    g, stats = o.materialize(saved, now=now, persist=True)
    out_model = dict(saved)
    out_model["materialized"] = True
    out_model["stats"] = stats
    mid = saved.get("id") or model_id
    _GRAPHS[mid] = (_fingerprint(saved), g, saved)
    _INDEXES.pop(mid, None)
    return {"ok": True, "model": out_model, "stats": stats}


@router.post("/model/{model_id}/duplicate")
@_guarded
def kg_model_duplicate(model_id: str):
    o = _need_ontology()
    copy = o.duplicate_model(model_id, now=o.utcnow_iso())
    _invalidate(copy.get("id"))
    return {"ok": True, "model": _decorate(copy)}


@router.delete("/model/{model_id}")
@_guarded
def kg_model_delete(model_id: str):
    """Deleting a built-in removes the on-disk shadow and restores the constant — which is why
    `default` itself is refused: there is nothing behind it to fall back to."""
    o = _need_ontology()
    if model_id == "default":
        raise KgError("READ_ONLY", "the built-in default model cannot be deleted")
    o.load_model(model_id)          # 404 for an id that never existed, before we touch any state
    deleted = o.delete_model(model_id)
    if not deleted:
        raise KgError("NOT_FOUND", f'no model "{model_id}"', status=404)
    _invalidate(model_id)
    return {"ok": True, "deleted": True}


@router.post("/active")
@_guarded
async def kg_active(request: Request):
    o = _need_ontology()
    body = await _body(request)
    mid = str(body.get("model_id") or "").strip()
    if not mid:
        raise KgError("BAD_REQUEST", "model_id is required")
    active = o.set_active_model(mid, now=o.utcnow_iso())
    return {"ok": True, "active": active}


# ---------------------------------------------------------------- §D.2 explorer

@router.get("/graph/roots")
@_guarded
def kg_roots(model: str = "", limit: int = 40):
    """Entry points for the explorer: the biggest node of every non-doc type, then the next
    largest overall. Never the 600 dish nodes — a canvas that opens with the whole corpus on it
    is unreadable and pins a core stabilizing it."""
    o = _need_ontology()
    mid = _model_id(model)
    g, m = _graph_for(mid)
    doc = _doc_tag(m)
    picked: list[str] = []
    rest: list[tuple[int, str]] = []
    for et in (m.get("entity_types") or []):
        tag = et.get("tag", "")
        if not tag or tag == doc:
            continue
        ranked = sorted(o.nodes_by_tag(g, tag), key=lambda n: (-int(g.nodes[n].get("n", 1) or 1), n))
        if ranked:
            picked.append(ranked[0])
            rest.extend((-int(g.nodes[n].get("n", 1) or 1), n) for n in ranked[1:])
    rest.sort()
    picked += [n for _, n in rest[:8]]
    by_kind: dict[str, int] = {}
    for _n, d in g.nodes(data=True):
        k = d.get("kind", "?")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"ok": True, "model_id": mid, "index_version": _index_version(mid),
            "nodes": [_node_view(g, n) for n in picked[:max(1, limit)]],
            "stats": {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(), "by_kind": by_kind}}


@router.get("/graph/neighbors")
@_guarded
def kg_neighbors(model: str = "", node: str = "", limit: int = 25):
    """Click-to-expand. `node` ids legitimately contain spaces and `&` (cuisine:salads & bowls),
    so the client must encodeURIComponent them; an unknown id degrades to an empty expansion
    rather than a 404 that would abort the whole canvas."""
    o = _need_ontology()
    mid = _model_id(model)
    g, m = _graph_for(mid)
    if not node or node not in g:
        return {"ok": True, "model_id": mid, "index_version": _index_version(mid),
                "nodes": [], "edges": [], "truncated": False, "total": 0}
    labels = _rel_labels(m)
    raw = o.neighbors(g, node, limit=limit) or {}
    nodes = raw.get("nodes")
    edges = raw.get("edges")
    total = raw.get("total")
    if nodes is None or edges is None:
        nodes_map = {node: _node_view(g, node)}
        edges = []
        out_e = list(g.out_edges(node, data=True))
        in_e = list(g.in_edges(node, data=True))
        total = len(out_e) + len(in_e)
        for _s, t, d in out_e[:limit]:
            nodes_map[t] = _node_view(g, t)
            edges.append({"from": node, "to": t, "rel": d.get("rel"),
                          "label": d.get("label") or labels.get(d.get("rel"), "")})
        for s, _t, d in in_e[:limit]:
            nodes_map[s] = _node_view(g, s)
            edges.append({"from": s, "to": node, "rel": d.get("rel"),
                          "label": d.get("label") or labels.get(d.get("rel"), "")})
        nodes = list(nodes_map.values())
    for e in edges:
        e.setdefault("label", labels.get(e.get("rel"), ""))
    total = int(total if total is not None else len(edges))
    return {"ok": True, "model_id": mid, "index_version": _index_version(mid),
            "nodes": nodes, "edges": edges,
            "truncated": bool(raw.get("truncated", total > len(edges))), "total": total}


@router.get("/graph/search")
@_guarded
def kg_graph_search(model: str = "", q: str = "", limit: int = 12):
    """Typeahead over node labels for the explorer's search box. Prefix hits first, then shortest
    label — the same ordering the food-ontology explorer already uses, so the two feel identical."""
    mid = _model_id(model)
    g, _m = _graph_for(mid)
    t = (q or "").strip().casefold()
    if not t:
        return {"ok": True, "model_id": mid, "nodes": []}
    hits = []
    for n, d in g.nodes(data=True):
        name = str(d.get("label") or d.get("name") or "").casefold()
        if t in name:
            hits.append((0 if name.startswith(t) else 1, len(name), -int(d.get("n", 1) or 1), n))
    hits.sort()
    return {"ok": True, "model_id": mid, "nodes": [_node_view(g, h[3]) for h in hits[:max(1, limit)]]}


# ---------------------------------------------------------------- §D.3 slots / values / typeahead

_TEXT_SLOT = {"tag": "text", "label": "Free text", "kind": "text",
              "fields": ["label", "description"], "polarity": "both", "order": 99}


@router.get("/slots")
@_guarded
def kg_slots(model: str = ""):
    """What the query bar CAN ask. Safety-critical types are forced into the first five and the
    rest sort by coverage — stable across loads so the chip palette never reshuffles."""
    mid = _model_id(model)
    g, m = _graph_for(mid)
    idx = _index_for(mid)
    slots = getattr(idx, "slots", None) or _need_query().build_slots(m, g, idx)
    rows = [_plain(s) for s in (slots.values() if isinstance(slots, dict) else slots)]
    rows.sort(key=lambda s: (0 if s.get("safety_critical") else 1, s.get("order", 50),
                             -float(s.get("coverage", 0) or 0), s.get("tag", "")))
    forced = {s.get("tag") for s in rows if s.get("safety_critical")}
    rows = [s for s in rows if s.get("tag") in forced] + [s for s in rows if s.get("tag") not in forced]
    if not any(s.get("kind") == "text" for s in rows):
        rows.append(dict(_TEXT_SLOT))
    # a declared entity type no relation reaches is real, and silently dropping it would hide a
    # modelling mistake the builder can fix in one drag
    doc = _doc_tag(m)
    reach = {doc}
    rels = m.get("relation_types") or []
    for _ in range(len(rels) + 1):
        for r in rels:
            if r.get("from") in reach or r.get("to") in reach:
                reach.add(r.get("from")); reach.add(r.get("to"))
    unreachable = [{"tag": et.get("tag"), "reason": f"no relation connects it to {doc}"}
                   for et in (m.get("entity_types") or []) if et.get("tag") not in reach]
    return {"ok": True, "model_id": mid, "index_version": getattr(idx, "version", None),
            "subject": getattr(idx, "subject", doc), "slots": rows, "unreachable": unreachable}


@router.get("/values")
@_guarded
def kg_values(model: str = "", type: str = "", q: str = "", limit: int = 10):  # noqa: A002
    o = _need_ontology()
    mid = _model_id(model)
    g, _m = _graph_for(mid)
    tag = (type or "").strip()
    if not tag:
        raise KgError("BAD_REQUEST", "type is required")
    rows = o.vocab(g, tag, q or "", max(1, limit)) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            r = {"value": str(r)}
        value = r.get("value") or r.get("label") or ""
        label = r.get("label") or value
        out.append({"value": value, "label": label,
                    "node": r.get("node") or f"{tag}:{str(value).casefold()}",
                    "count": int(r.get("count", r.get("n", 0)) or 0),
                    "match": r.get("match") or _match_tier(str(label), q or ""),
                    "insert": r.get("insert") or _insert_form(value)})
    return {"ok": True, "tag": tag, "values": out}


@router.get("/typeahead")
@_guarded
def kg_typeahead(model: str = "", q: str = "", caret: int = -1, tag: str = "",
                 mode: str = "auto", limit: int = 8):
    """Caret-aware completion over the DSL. The caret defaults to end-of-string so a plain
    `?q=veg` still works from a shell or a saved link."""
    q_mod = _need_query()
    mid = _model_id(model)
    idx = _index_for(mid)
    src = q or ""
    pos = len(src) if caret is None or caret < 0 else min(int(caret), len(src))
    t0 = time.perf_counter()
    raw = q_mod.typeahead(src, pos, idx, tag=tag or "", mode=mode or "auto",
                          limit=max(1, limit)) or {}
    out = dict(raw)
    out.setdefault("ok", True)
    out["ok"] = True
    out.setdefault("mode", mode or "auto")
    out.setdefault("tag", tag or "")
    out.setdefault("prefix", "")
    out.setdefault("replace", [pos, pos])
    out.setdefault("suggestions", [])
    for s in out["suggestions"]:
        if isinstance(s, dict) and "insert" not in s:
            s["insert"] = _insert_form(s.get("value") or s.get("label") or "")
    out["took_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return out


# ---------------------------------------------------------------- §D.4 query

def _run_query(mid: str, *, q: str = "", slots: list | None = None, limit: int = 24,
               offset: int = 0, rollup: bool = True, facets: bool = True,
               subgraph: bool = True) -> dict:
    q_mod = _need_query()
    idx = _index_for(mid)
    parsed = (q_mod.slots_to_parsed(slots, idx) if slots else q_mod.parse(q or "", idx))
    resp = q_mod.execute(parsed, idx, limit=max(0, limit), offset=max(0, offset),
                         rollup=rollup, facets=facets, subgraph=subgraph) or {}
    out = dict(resp)
    out["ok"] = True                     # a blocked query is a 200 with blocked:true, never a 4xx
    out["model_id"] = mid
    out["index_version"] = getattr(idx, "version", None)
    out.setdefault("blocked", bool(parsed.get("blocked", False)))
    out.setdefault("parse", _plain(parsed))
    out.setdefault("dsl", parsed.get("source", q or ""))
    out.setdefault("normalized_dsl", parsed.get("normalized") or q_mod.render(parsed))
    out.setdefault("warnings", parsed.get("warnings") or [])
    out.setdefault("relaxation", [])
    return out


@router.get("/query")
@_guarded
def kg_query_get(model: str = "", q: str = "", limit: int = 24, offset: int = 0,
                 rollup: bool = True, facets: bool = True, subgraph: bool = True):
    return _run_query(_model_id(model), q=q, limit=limit, offset=offset,
                      rollup=rollup, facets=facets, subgraph=subgraph)


@router.post("/query")
@_guarded
async def kg_query_post(request: Request):
    """`slots` wins over `q` when both are present — the chip row is the user's actual intent and
    the response still carries normalized_dsl so the text bar stays in sync."""
    body = await _body(request)
    slots = body.get("slots")
    if slots is not None and not isinstance(slots, list):
        raise KgError("BAD_REQUEST", "slots must be a list of FilledSlot objects")
    return _run_query(_model_id(str(body.get("model") or "")),
                      q=str(body.get("q") or ""), slots=slots or None,
                      limit=_as_int(body.get("limit"), 24), offset=_as_int(body.get("offset"), 0),
                      rollup=_as_bool(body.get("rollup"), True),
                      facets=_as_bool(body.get("facets"), True),
                      subgraph=_as_bool(body.get("subgraph"), True))


# ---------------------------------------------------------------- §D.7 analytics

_ANALYTICS = ("coverage", "blockers", "substitute", "bridge", "one_stop", "versatility")


def _analytics(name: str, mid: str, p: dict) -> dict:
    """One dispatcher for the six, shared by the GET endpoints and by /queries/{id}/run — a saved
    analytics query must execute through exactly the same path as a hand-typed one."""
    q_mod = _need_query()
    idx = _index_for(mid)
    if name == "coverage":
        rows, cols = str(p.get("rows") or ""), str(p.get("cols") or "")
        if not rows or not cols:
            raise KgError("BAD_REQUEST", "rows and cols are required")
        return q_mod.analytics_coverage(idx, rows, cols, str(p.get("base") or ""),
                                        _as_int(p.get("min_count"), 1))
    if name == "blockers":
        return q_mod.analytics_blockers(idx, base=str(p.get("base") or ""),
                                        tags=_csv(p.get("tags")) or None,
                                        limit=_as_int(p.get("limit"), 10))
    if name == "substitute":
        dish = str(p.get("dish") or "")
        if not dish:
            raise KgError("BAD_REQUEST", "dish is required")
        return q_mod.analytics_substitute(idx, dish, _csv(p.get("keep")),
                                          require=str(p.get("require") or ""),
                                          limit=_as_int(p.get("limit"), 8))
    if name == "bridge":
        a, b = str(p.get("a") or ""), str(p.get("b") or "")
        if not a or not b:
            raise KgError("BAD_REQUEST", "a and b are required")
        g, _m = _graph_for(mid)
        return q_mod.analytics_bridge(idx, g, a, b, _csv(p.get("via")), k=_as_int(p.get("k"), 6))
    if name == "one_stop":
        req = p.get("require")
        req = req if isinstance(req, (list, tuple)) else ([req] if req else [])
        req = [str(r) for r in req if str(r).strip()]
        if not req:
            raise KgError("BAD_REQUEST", "at least one require= is needed")
        return q_mod.analytics_one_stop(idx, req, group=str(p.get("group") or "caterer"),
                                        limit=_as_int(p.get("limit"), 12))
    if name == "versatility":
        over = str(p.get("over") or "")
        if not over:
            raise KgError("BAD_REQUEST", "over is required")
        return q_mod.analytics_versatility(idx, over, base=str(p.get("base") or ""),
                                           rollup=_as_bool(p.get("rollup"), True),
                                           limit=_as_int(p.get("limit"), 10))
    raise KgError("NOT_FOUND", f'no analytics named "{name}"', status=404)


# Which params of each analytic name a TAG, and which carry a DSL string. An analytic whose
# tag is absent from the live ontology still computes -- over an empty posting list -- and would
# otherwise return a confident, empty, WRONG answer (a "most versatile dish" with degree 0, a
# blockers report whose base silently widened from 230 to the whole catalogue). The query
# executor already reports `unknown_tag`; analytics threw that signal away. This restores it.
_ANALYTICS_TAG_PARAMS = {
    "coverage":    (("rows", "cols"), ()),
    "blockers":    ((), ("tags",)),
    "substitute":  ((), ("keep",)),
    "bridge":      ((), ("via",)),
    "one_stop":    (("group",), ()),
    "versatility": (("over",), ()),
}
_ANALYTICS_DSL_PARAMS = {
    "coverage": ("base",), "blockers": ("base",), "substitute": ("require",),
    "bridge": (), "one_stop": ("require",), "versatility": ("base",),
}


def _known_tags(idx: Any) -> set:
    return set(getattr(idx, "postings", {}) or {}) | set(getattr(idx, "slots", {}) or {})


def _analytics_warnings(name: str, idx: Any, p: dict) -> list:
    """Every way an analytic can be quietly meaningless on the CURRENT ontology."""
    q_mod = _need_query()
    aliases = getattr(q_mod, "ALIASES", {}) or {}
    known = _known_tags(idx)
    warns: list = []
    seen: set = set()

    def bad_tag(tag: str, where: str) -> None:
        t = aliases.get(str(tag).lower(), str(tag).lower())
        if not t or t in known:
            return
        key = ("unknown_tag", t, where)
        if key in seen:
            return
        seen.add(key)
        warns.append({"code": "unknown_tag", "tag": t, "param": where,
                      "message": f'"{t}" is not an entity type in this ontology; '
                                 f"{where} was ignored."})

    scalars, csvs = _ANALYTICS_TAG_PARAMS.get(name, ((), ()))
    for key in scalars:
        if str(p.get(key) or "").strip():
            bad_tag(str(p[key]), key)
    for key in csvs:
        for t in _csv(p.get(key)):
            bad_tag(t, key)
    # a/b of bridge are node refs: "cuisine:Thai"
    for key in ("a", "b"):
        raw = str(p.get(key) or "").strip()
        if raw and ":" in raw:
            bad_tag(raw.split(":", 1)[0], key)

    parse = getattr(q_mod, "parse", None)
    if callable(parse):
        for key in _ANALYTICS_DSL_PARAMS.get(name, ()):
            raw = p.get(key)
            for src in (raw if isinstance(raw, (list, tuple)) else [raw]):
                src = str(src or "").strip()
                if not src:
                    continue
                try:
                    parsed = parse(src, idx)
                except Exception:  # noqa: BLE001
                    continue
                for w in (parsed.get("warnings") or []):
                    if w.get("code") != "unknown_tag":
                        continue
                    t = w.get("tag")
                    key2 = ("unknown_tag", t, key)
                    if key2 in seen:
                        continue
                    seen.add(key2)
                    warns.append({"code": "constraint_dropped", "tag": t, "param": key,
                                  "dsl": src,
                                  "message": f'"{t}" is not in this ontology, so the constraint '
                                             f'was dropped from {key}="{src}" — the numbers below '
                                             f"are NOT scoped the way the label says."})
                for sl in (parsed.get("slots") or []):
                    for v in (sl.get("values") or []):
                        if v.get("status") == "unresolved":
                            key3 = ("unresolved", sl.get("tag"), v.get("value"), key)
                            if key3 in seen:
                                continue
                            seen.add(key3)
                            warns.append({"code": "value_unresolved", "tag": sl.get("tag"),
                                          "value": v.get("value"), "param": key, "dsl": src,
                                          "message": f'{sl.get("tag")}:"{v.get("value")}" matched '
                                                     f"nothing in this ontology."})
    return warns


def _analytics_out(name: str, mid: str, p: dict) -> dict:
    out = dict(_analytics(name, mid, p) or {})
    out["ok"] = True
    out["model_id"] = mid
    out.setdefault("index_version", _index_version(mid))
    try:
        warns = _analytics_warnings(name, _index_for(mid), p)
    except Exception:  # noqa: BLE001 - a warning pass must never break the answer
        warns = []
    if warns:
        out["warnings"] = list(out.get("warnings") or []) + warns
        out["degraded"] = True
    else:
        out.setdefault("warnings", [])
        out.setdefault("degraded", False)
    return out


@router.get("/analytics/coverage")
@_guarded
def an_coverage(model: str = "", rows: str = "", cols: str = "", base: str = "",
                min_count: int = 1):
    mid = _model_id(model)
    return _analytics_out("coverage", mid, {"rows": rows, "cols": cols, "base": base,
                                            "min_count": min_count})


@router.get("/analytics/blockers")
@_guarded
def an_blockers(model: str = "", base: str = "", tags: str = "", limit: int = 10):
    mid = _model_id(model)
    return _analytics_out("blockers", mid, {"base": base, "tags": tags, "limit": limit})


@router.get("/analytics/substitute")
@_guarded
def an_substitute(model: str = "", dish: str = "", keep: str = "", require: str = "",
                  limit: int = 8):
    mid = _model_id(model)
    return _analytics_out("substitute", mid, {"dish": dish, "keep": keep, "require": require,
                                              "limit": limit})


@router.get("/analytics/bridge")
@_guarded
def an_bridge(model: str = "", a: str = "", b: str = "", via: str = "", k: int = 6):
    mid = _model_id(model)
    return _analytics_out("bridge", mid, {"a": a, "b": b, "via": via, "k": k})


@router.get("/analytics/one_stop")
@_guarded
def an_one_stop(model: str = "", require: list[str] = Query(default=[]),  # noqa: B008
                group: str = "caterer", limit: int = 12):
    """`require` is repeatable: each occurrence is one constraint the group must satisfy, which is
    what makes this a set-cover question rather than a filter."""
    mid = _model_id(model)
    return _analytics_out("one_stop", mid, {"require": list(require), "group": group,
                                            "limit": limit})


@router.get("/analytics/versatility")
@_guarded
def an_versatility(model: str = "", over: str = "", base: str = "", rollup: bool = True,
                   limit: int = 10):
    mid = _model_id(model)
    return _analytics_out("versatility", mid, {"over": over, "base": base, "rollup": rollup,
                                               "limit": limit})


# ---------------------------------------------------------------- §D.6 saved queries

def _queries_payload(mid: str, include_builtin: bool, tag: str) -> dict:
    q_mod = _need_query()
    raw = q_mod.list_queries(mid, include_builtin) or {}
    if isinstance(raw, list):
        raw = {"queries": raw}
    rows = list(raw.get("queries") or [])
    if tag:
        rows = [q for q in rows if tag in (q.get("tags") or [])]
    tags = raw.get("tags")
    if not tags:
        seen: list[str] = []
        for q in (raw.get("queries") or []):
            for t in (q.get("tags") or []):
                if t not in seen:
                    seen.append(t)
        tags = seen
    return {"ok": True, "model_id": mid, "tags": tags, "queries": rows}


@router.get("/queries")
@_guarded
def kg_queries(model: str = "", include_builtin: bool = True, tag: str = ""):
    return _queries_payload(_model_id(model), include_builtin, tag)


@router.post("/queries")
@_guarded
async def kg_query_save(request: Request):
    """A rule that is already broken cannot be saved: the DSL is parsed against the LIVE index
    first, so `-allergen:"pine nut"` is rejected here rather than discovered at click time."""
    q_mod = _need_query()
    body = await _body(request)
    mid = _model_id(str(body.get("model") or ""))
    if not str(body.get("title") or "").strip():
        raise KgError("BAD_REQUEST", "title is required")
    if not body.get("dsl") and not body.get("endpoint"):
        raise KgError("BAD_REQUEST", "a saved query needs either dsl or endpoint+params")
    idx = _index_for(mid)
    payload = {k: v for k, v in body.items() if k != "model"}
    payload["model_id"] = mid
    try:
        saved = q_mod.save_query(payload, idx, now=_need_ontology().utcnow_iso())
    except ValueError as exc:
        raise KgError("BAD_REQUEST", str(exc))
    out = {"ok": True, "model_id": mid, "query": saved}
    if isinstance(saved, dict) and saved.get("last_count") == 0:
        out["warning"] = "empty_at_save"
    return out


@router.patch("/queries/{qid}")
@_guarded
async def kg_query_patch(qid: str, request: Request):
    q_mod = _need_query()
    patch = await _body(request)
    patch.pop("id", None)
    updated = q_mod.patch_query(qid, patch, now=_need_ontology().utcnow_iso())
    if not updated:
        raise KgError("NOT_FOUND", f'no saved query "{qid}"', status=404)
    return {"ok": True, "query": updated}


@router.delete("/queries/{qid}")
@_guarded
def kg_query_delete(qid: str):
    """Deleting a built-in writes a `hidden` tombstone rather than removing anything, so the
    twelve canned queries are always one PATCH away from coming back."""
    if not _need_query().delete_query(qid):
        raise KgError("NOT_FOUND", f'no saved query "{qid}"', status=404)
    return {"ok": True, "deleted": True}


@router.post("/queries/{qid}/run")
@_guarded
def kg_query_run(qid: str, model: str = "", limit: int = 24, rollup: bool = True):
    """Run a canned query. Slot queries go through the DSL executor, analytics queries through the
    same dispatcher the GET endpoints use — one code path, so a card can never drift from the
    endpoint it claims to call."""
    mid = _model_id(model)
    rows = _queries_payload(mid, True, "")["queries"]
    q = next((r for r in rows if r.get("id") == qid), None)
    if q is None:
        raise KgError("NOT_FOUND", f'no saved query "{qid}"', status=404)
    params = dict(q.get("params") or {})
    kind = q.get("kind") or ("analytics" if q.get("endpoint", "").startswith("/api/kg/analytics") else "slots")
    if kind == "analytics":
        name = str(q.get("endpoint") or "").rstrip("/").rsplit("/", 1)[-1]
        if name not in _ANALYTICS:
            raise KgError("BAD_REQUEST", f'saved query "{qid}" names an unknown endpoint')
        params.setdefault("limit", limit)
        out = _analytics_out(name, mid, params)
        out["query_id"] = qid
        return out
    out = _run_query(mid, q=str(q.get("dsl") or ""),
                     limit=_as_int(params.get("limit", limit), limit),
                     rollup=_as_bool(params.get("rollup", rollup), rollup))
    out["query_id"] = qid
    return out
