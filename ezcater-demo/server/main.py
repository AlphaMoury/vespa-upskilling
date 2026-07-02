"""
FastAPI proxy for the Vespa demo. Mirrors the ezCater ML/LLM Search role:
  - Frontend QUERY UNDERSTANDING: NL query -> structured concepts -> precise Vespa query
  - hybrid (BM25 + e5 vectors), typeahead (gram), and the food-ontology fields

  GET /api/health
  GET /api/typeahead?q=&schema=dish
  GET /api/understand?q=...                 -> structured concepts (LLM if ANTHROPIC_API_KEY, else heuristic)
  GET /api/search?q=&mode=&schema=&...       -> ranked results
        mode: keyword | semantic | hybrid | understood   (understood = query understanding for dish)

Run:  ../../capstone/.venv/bin/python -m uvicorn main:app --port 8009
"""

import os
import re
import sys
import time
import json
import tempfile
import shutil
from pathlib import Path
import requests
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# make the ingestion package importable (shares the LLM provider + cost policy)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from ingest import config as ingest_config
    from ingest import llm as ingest_llm
    from ingest import semcache as ingest_semcache
    from ingest.graph import get_graph as _get_graph
except Exception:  # noqa: BLE001 — server still runs without the ingest package
    ingest_config = None
    ingest_llm = None
    ingest_semcache = None
    _get_graph = None

VESPA = "http://localhost:8080/search/"
VESPA_DOC = "http://localhost:8080/document/v1"
NAMESPACE = "ezcater"
SCHEMAS = {"dish": {"title": "name"}, "covid": {"title": "title"}, "question": {"title": "text"}}

app = FastAPI(title="Vespa x LLM catering search")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# warm the local e5 cache embedder in the background so the first query isn't slowed by load
if ingest_semcache is not None:
    import threading
    threading.Thread(target=ingest_semcache.warmup, daemon=True).start()


def _vespa(params):
    r = requests.get(VESPA, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _emb(q):
    return f'embed(e5, "{q.replace(chr(92), " ").replace(chr(34), " ")}")'


# ---------------- QUERY UNDERSTANDING ----------------
DIET = {"vegan": "vegan", "vegetarian": "vegetarian", "gluten free": "gluten-free", "gluten-free": "gluten-free",
        "dairy free": "dairy-free", "dairy-free": "dairy-free", "halal": "halal", "kosher": "kosher", "plant based": "vegan", "plant-based": "vegan"}
ALLERGENS = ["nuts", "dairy", "gluten", "shellfish", "soy"]
CUISINE_VOCAB = ["italian", "mexican", "japanese", "indian", "thai", "mediterranean", "american", "chinese", "breakfast"]
OCCASION = {"client": "client", "impressive": "impressive", "healthy": "healthy", "light": "light",
            "comfort": "comfort", "celebration": "celebration", "party": "celebration", "morning": "morning"}
INCLUDE_VOCAB = {"meat", "chicken", "beef", "pork", "lamb", "turkey", "fish", "seafood",
                 "shrimp", "salmon", "tuna", "cheese", "bacon", "sausage"}


def understand_heuristic(q: str) -> dict:
    t = q.lower()
    diet = []
    for k, v in DIET.items():
        if k in t and v not in diet:
            diet.append(v)
    excl = []
    for a in ALLERGENS:
        if f"no {a}" in t or f"{a} free" in t or f"{a}-free" in t or f"without {a}" in t:
            excl.append(a)
    if ("nut free" in t or "nut-free" in t) and "nuts" not in excl:
        excl.append("nuts")
    spice = 2 if ("spicy" in t or "hot " in t) else None
    cuisine = next((c.capitalize() for c in CUISINE_VOCAB if c in t), None)
    occ = [v for k, v in OCCASION.items() if k in t]
    occ = list(dict.fromkeys(occ))
    mp = None
    m = re.search(r"(?:under|below|less than|<|max)\s*\$?\s*(\d+)", t) or re.search(r"\$\s*(\d+)\s*(?:/|per|a)\s*(?:head|person|pp)", t)
    if m:
        mp = float(m.group(1))
    hc = None
    m2 = re.search(r"(?:for|party of|team of|group of)\s+(\d+)", t) or re.search(r"(\d+)\s*(?:people|persons|guests|pax)", t)
    if m2:
        hc = int(m2.group(1))
    inc = []
    for m3 in re.finditer(r"\bwith\s+([a-z]+)", t):  # "with meat" -> include; "without/no" don't match
        w = m3.group(1)
        if w in INCLUDE_VOCAB and w not in inc:
            inc.append(w)
    return {"free_text": q, "dietary": diet, "exclude_allergens": excl, "spice_min": spice,
            "cuisine": cuisine, "occasion": occ, "include": inc, "max_price_pp": mp, "headcount": hc, "method": "heuristic"}


_UNDERSTAND_SYS = (
    "Extract structured catering-search concepts from the query. Return ONLY JSON with keys: "
    "free_text (string, the semantic intent), dietary (array of: vegan,vegetarian,gluten-free,dairy-free,halal,kosher), "
    "exclude_allergens (array of: nuts,peanuts,dairy,gluten,shellfish,fish,soy,eggs,sesame), spice_min (0-3 or null), "
    "cuisine (one of Italian,Mexican,Japanese,Indian,Thai,Mediterranean,American,Chinese,Breakfast or null — "
    "ONLY set this when the user explicitly asks for a cuisine or food category; leave null when they name a "
    "specific dish like 'smoked brisket sliders'), "
    "occasion (array of: client,impressive,healthy,light,comfort,celebration,morning), "
    "include (array of ingredients/protein categories the user explicitly WANTS present, e.g. "
    "['meat'] for 'with meat', ['chicken'] for 'with chicken' — empty unless clearly requested), "
    "max_price_pp (number or null, per-person budget), headcount (int or null)."
)


def understand_llm(q: str) -> dict:
    """LLM concept extraction via the shared OpenAI provider. Falls back to heuristic.

    NOTE: this is only reached when config.query_llm_enabled() (LLM_QUERY=on). By policy
    the consumer hot path defaults to the deterministic heuristic — an LLM per query is a
    cost/latency/prompt-injection risk at ezCater's scale."""
    if ingest_llm is None:
        return understand_heuristic(q)
    data = ingest_llm.chat_json(_UNDERSTAND_SYS, q, max_tokens=400)
    if not data:
        return understand_heuristic(q)
    data["free_text"] = data.get("free_text") or q
    data["method"] = "llm"
    # backfill anything the LLM omitted with the heuristic (belt and suspenders)
    h = understand_heuristic(q)
    for k in ("dietary", "exclude_allergens", "cuisine", "occasion", "include", "max_price_pp", "headcount", "spice_min"):
        if not data.get(k):
            data[k] = h.get(k)
    return data


def _llm_query_on() -> bool:
    return bool(ingest_config and ingest_config.query_llm_enabled())


def understand(q: str) -> dict:
    """LLM-first (bounded by a SEMANTIC CACHE) when enabled; deterministic regex otherwise.
    The cache keys on the query's intent embedding, so paraphrases reuse one LLM answer."""
    if not _llm_query_on():
        return understand_heuristic(q)
    if ingest_semcache is not None:
        cached = ingest_semcache.get(q)
        if cached is not None:
            return {**cached, "free_text": cached.get("free_text") or q, "cache": "hit"}
    result = understand_llm(q)
    if ingest_semcache is not None:
        ingest_semcache.put(q, result)
    return {**result, "cache": "miss"}


@app.get("/api/understand")
def api_understand(q: str = ""):
    return understand(q) if q.strip() else {}


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/api/understand_stream")
def understand_stream(q: str = "", hits: int = 8, source: str = "",
                      cuisine: str = "", dietary: str = "", maxprice: str = ""):
    """Server-Sent Events for the 'understood' column, in one call so understanding runs ONCE:
      token   events  — the LLM generating concepts live (only on a cache miss)
      cached  event   — concepts served from cache instantly (no generation)
      results event   — the graph-expanded hybrid Vespa search: hits + graph + timing
    """
    def gen():
        t0 = time.perf_counter()
        if not q.strip():
            yield _sse({"type": "results", "concepts": {}, "hits": [], "graph": None})
            return
        # --- 1) get concepts (heuristic / cache hit / streamed LLM) ---
        if not _llm_query_on() or ingest_llm is None:
            concepts = understand_heuristic(q)
            yield _sse({"type": "cached", "concepts": concepts})
        else:
            cached = ingest_semcache.get(q) if ingest_semcache is not None else None
            if cached is not None:
                concepts = {**cached, "free_text": cached.get("free_text") or q, "cache": "hit"}
                yield _sse({"type": "cached", "concepts": concepts})
            else:
                acc = ""
                try:
                    client = ingest_llm._get_client()
                    model = ingest_config.OPENAI_MODEL if ingest_config else "gpt-4o-mini"
                    stream = client.chat.completions.create(
                        model=model, temperature=0, max_tokens=400,
                        response_format={"type": "json_object"}, stream=True,
                        messages=[{"role": "system", "content": _UNDERSTAND_SYS}, {"role": "user", "content": q}])
                    for chunk in stream:
                        delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                        if delta:
                            acc += delta
                            yield _sse({"type": "token", "text": delta})
                    concepts = ingest_llm._parse_json(acc) or understand_heuristic(q)
                except Exception:  # noqa: BLE001
                    concepts = understand_heuristic(q)
                concepts["free_text"] = concepts.get("free_text") or q
                concepts["method"] = "llm"
                concepts["cache"] = "miss"
                h = understand_heuristic(q)
                for k in ("dietary", "exclude_allergens", "cuisine", "occasion", "include", "max_price_pp", "headcount", "spice_min"):
                    if not concepts.get(k):
                        concepts[k] = h.get(k)
                if ingest_semcache is not None:
                    ingest_semcache.put(q, {k: v for k, v in concepts.items() if not str(k).startswith("_")})
                yield _sse({"type": "done", "concepts": concepts})
        understand_ms = round((time.perf_counter() - t0) * 1000, 1)
        # --- 2) run the hybrid Vespa search and stream the results back ---
        try:
            r = _understood_run(concepts, hits, source, extra=_facet_filter("dish", cuisine, dietary, maxprice))
            yield _sse({"type": "results", "concepts": concepts, "hits": r["hits"], "graph": r["graph"],
                        "applied_filters": r["applied_filters"], "debug": r["debug"], "total": r["total"],
                        "timing": {"total_ms": round((time.perf_counter() - t0) * 1000, 1),
                                   "vespa_ms": r["vespa_ms"], "understand_ms": understand_ms}})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "results", "concepts": concepts, "hits": [], "graph": None, "error": str(e)})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- result mapping ----------------
def _map(schema, f):
    if schema == "dish":
        return dict(name=f.get("name"), sub=f.get("caterer_name"), tag=f.get("cuisine"),
                    price=f.get("price"), price_pp=f.get("price_pp"), serves=f.get("serves"),
                    badges=f.get("dietary", []), spice=f.get("spice_level"), allergens=f.get("allergens", []),
                    desc=f.get("description"), source=f.get("source"), ingredients=f.get("ingredients", []))
    if schema == "covid":
        return dict(name=f.get("title") or "(untitled)", sub="COVID-19 research", tag=None, price=None,
                    badges=[], desc=(f.get("body") or "")[:240])
    return dict(name=f.get("text"), sub="Quora question", tag=None, price=None, badges=[], desc=None)


def _hits(schema, resp):
    out = []
    for h in resp.get("root", {}).get("children", []) or []:
        f = h.get("fields", {})
        mf = f.get("matchfeatures", {}) or {}
        item = _map(schema, f)
        item["relevance"] = round(h.get("relevance", 0), 4)
        item["bm25"] = round(mf.get("bm25sum", 0), 2) if mf else None
        # Vespa normalizes the feature name to no spaces: "closeness(field,embedding)"
        clo = mf.get("closeness(field,embedding)", mf.get("closeness(field, embedding)"))
        item["semantic"] = round(clo, 3) if (mf and clo is not None) else None
        out.append(item)
    return out


def _dedupe(hits, limit):
    seen, out = set(), []
    for h in hits:
        k = (h.get("name") or "").lower()
        if k not in seen:
            seen.add(k)
            out.append(h)
        if len(out) >= limit:
            break
    return out


@app.get("/api/health")
def health():
    counts = {}
    for s in SCHEMAS:
        try:
            counts[s] = _vespa({"yql": f"select * from {s} where true", "hits": 0})["root"]["fields"]["totalCount"]
        except Exception:  # noqa: BLE001
            counts[s] = None
    llm = ingest_config.status() if ingest_config else {"has_key": bool(os.environ.get("OPENAI_API_KEY"))}
    graph = None
    try:
        graph = _get_graph().stats() if _get_graph else None
    except Exception:  # noqa: BLE001
        pass
    sem = None
    if ingest_semcache is not None:
        sem = {"size": ingest_semcache.size(), "model": ingest_semcache.MODEL, **ingest_semcache.stats}
    return {"ok": any(counts.values()), "counts": counts,
            "llm": llm.get("query_llm", False),        # back-compat: is query-LLM live?
            "llm_status": llm, "graph": graph, "semcache": sem}


@app.get("/api/sources")
def sources(schema: str = "dish"):
    """Provenance facet: doc counts grouped by ingestion source (for the UI)."""
    if schema not in SCHEMAS:
        return {"sources": {}}
    try:
        yql = (f"select * from {schema} where true | "
               "all(group(source) each(output(count())))")
        resp = _vespa({"yql": yql, "hits": 0})
        out = {}
        for grp in resp.get("root", {}).get("children", []):
            for g in grp.get("children", []):
                for b in g.get("children", []):
                    val = b.get("value") or "(none)"
                    out[val] = b.get("fields", {}).get("count()", 0)
        return {"sources": out}
    except Exception as e:  # noqa: BLE001
        return {"sources": {}, "error": str(e)}


# ---------------- menu PDF upload (streamed: rasterize -> vision -> enrich -> feed) ----------------
@app.post("/api/upload_pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """Parse an uploaded menu PDF and stream progress (SSE): status events, then one 'item'
    event per transcribed dish (with the enriched structure + how many new ingredients it
    added to the ontology graph), then 'done'. Items are enriched and fed to Vespa live."""
    fname = os.path.basename(file.filename or "menu.pdf")
    data = await file.read()

    def gen():
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, fname)
        try:
            if not fname.lower().endswith(".pdf"):
                yield _sse({"type": "error", "error": "please upload a .pdf file"})
                return
            with open(path, "wb") as fh:
                fh.write(data)
            from ingest.adapters.pdf import MenuPDFAdapter
            from ingest.enrich import enrich as _enrich
            adapter = MenuPDFAdapter(path=path)
            yield _sse({"type": "status", "msg": "rasterizing pages…"})
            recs = list(adapter.fetch())
            npages = sum(len(r.get("images_b64", [])) for r in recs)
            use_vision = ingest_llm is not None and ingest_config is not None and ingest_config.index_llm_enabled()
            method = "vision-LLM" if use_vision else "text-parse"
            yield _sse({"type": "status", "msg": f"{method}: transcribing {npages} page(s)…", "method": method})
            items = []
            for r in recs:
                imgs = (r.get("images_b64") or [])[: adapter.max_pages]
                if use_vision and imgs:
                    from ingest.adapters.pdf import _VISION_SYS
                    content = [{"type": "text", "text": "Transcribe every menu item across these page image(s) into the JSON schema."}]
                    for b in imgs:
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}", "detail": "high"}})
                    acc = ""
                    stream = ingest_llm._get_client().chat.completions.create(
                        model=ingest_config.OPENAI_VISION_MODEL, stream=True, temperature=0, max_tokens=1500,
                        response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": _VISION_SYS}, {"role": "user", "content": content}])
                    for chunk in stream:
                        delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                        if delta:
                            acc += delta
                            yield _sse({"type": "token", "text": delta})   # live vision transcript
                    items += adapter.items_from_vision(ingest_llm._parse_json(acc) or {}, r)
                else:
                    items += adapter.to_menu_items(r)
            yield _sse({"type": "status", "msg": f"transcribed {len(items)} items — enriching + indexing…"})
            g = _get_graph() if _get_graph is not None else None
            fed, caterer = 0, None
            for it in items:
                before = g.stats().get("ingredient", 0) if g else 0
                _enrich(it)
                added = (g.stats().get("ingredient", 0) - before) if g else 0
                doc = it.to_vespa_doc()
                ok = False
                try:
                    rr = requests.post(f"{VESPA_DOC}/{NAMESPACE}/dish/docid/{doc['id']}",
                                       json={"fields": doc["fields"]}, timeout=30)
                    ok = rr.ok
                except Exception:  # noqa: BLE001
                    ok = False
                fed += 1 if ok else 0
                caterer = caterer or it.caterer_name
                yield _sse({"type": "item", "name": it.name, "price": it.price, "price_pp": it.price_pp,
                            "serves": it.serves, "course": it.course, "dietary": it.dietary,
                            "allergens": it.allergens, "ingredients": it.ingredients[:10],
                            "graph_added": added, "confidence": it.confidence, "fed": ok})
            if g:
                try:
                    g.save()
                except Exception:  # noqa: BLE001
                    pass
            yield _sse({"type": "done", "method": method, "caterer": caterer, "count": len(items), "fed": fed})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "error": str(e)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- ontology graph explorer (lazy expand/collapse) ----------------
def _gnode(nid: str) -> dict:
    kind, _, name = nid.partition(":")
    return {"id": nid, "kind": kind, "label": name}


@app.get("/api/graph/roots")
def graph_roots():
    """Top-level anchors to start the explorer: cuisines, allergens, diets (not the 500+ ingredients)."""
    if _get_graph is None:
        return {"nodes": [], "stats": {}}
    g = _get_graph().g
    roots = [_gnode(n) for n, d in g.nodes(data=True) if d.get("kind") in ("cuisine", "allergen", "diet", "category")]
    return {"nodes": roots, "stats": _get_graph().stats()}


@app.get("/api/graph/search")
def graph_search(q: str = "", limit: int = 12):
    """Typeahead over graph node labels (any kind) for the explorer's search box."""
    if _get_graph is None or len(q.strip()) < 1:
        return {"nodes": []}
    t = q.strip().lower()
    g = _get_graph().g
    hits = []
    for n, d in g.nodes(data=True):
        name = d.get("name", "")
        if t in name:
            hits.append((0 if name.startswith(t) else 1, len(name), _gnode(n)))
    hits.sort(key=lambda x: (x[0], x[1]))
    return {"nodes": [h[2] for h in hits[:limit]]}


@app.get("/api/graph/neighbors")
def graph_neighbors(node: str = "", limit: int = 40):
    """Neighbors of a node (both directions), for click-to-expand navigation."""
    if _get_graph is None or not node:
        return {"nodes": [], "edges": []}
    g = _get_graph().g
    if node not in g:
        return {"nodes": [], "edges": []}
    nodes, edges = {node: _gnode(node)}, []
    for _, t, d in list(g.out_edges(node, data=True))[:limit]:
        nodes[t] = _gnode(t)
        edges.append({"from": node, "to": t, "rel": d.get("rel")})
    for s, _, d in list(g.in_edges(node, data=True))[:limit]:
        nodes[s] = _gnode(s)
        edges.append({"from": s, "to": node, "rel": d.get("rel")})
    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/api/typeahead")
def typeahead(q: str = "", schema: str = "dish", limit: int = 6):
    if schema not in SCHEMAS:
        return {"suggestions": []}
    term = re.sub(r"[^a-z0-9 ]", " ", q.strip().lower()).strip()
    if len(term) < 2:
        return {"suggestions": []}
    title = SCHEMAS[schema]["title"]
    try:
        resp = _vespa({"yql": f'select {title} from {schema} where grams contains "{term}" limit 40', "ranking": "unranked"})
    except Exception:  # noqa: BLE001
        return {"suggestions": []}
    seen, sugg = set(), []
    for h in resp.get("root", {}).get("children", []) or []:
        name = (h.get("fields", {}) or {}).get(title)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            sugg.append({"name": name[:90]})
        if len(sugg) >= limit:
            break
    return {"suggestions": sugg}


def _graph_expand(concepts: dict) -> dict:
    """Query-time ONTOLOGY-GRAPH expansion: cuisine -> featured terms (broaden the vector
    query), exclude_allergens -> their ingredient sets (for the UI explanation)."""
    if _get_graph is None or not concepts:
        return {"added_terms": [], "allergen_ingredients": {}}
    try:
        return _get_graph().expand_query(concepts)
    except Exception:  # noqa: BLE001
        return {"added_terms": [], "allergen_ingredients": {}}


def _src_filter(source: str, schema: str) -> str:
    return f' and source contains "{source}"' if (source and schema == "dish") else ""


def _facet_filter(schema: str, cuisine: str = "", dietary: str = "", maxprice: str = "") -> str:
    """Manual UI facets (dish only) -> YQL. Applies to keyword/semantic/hybrid modes."""
    if schema != "dish":
        return ""
    parts = []
    if cuisine:
        parts.append(f'cuisine contains "{cuisine}"')
    for d in [x for x in (dietary or "").split(",") if x.strip()]:
        parts.append(f'dietary contains "{d.strip()}"')
    if maxprice:
        try:
            parts.append(f'price_pp < {float(maxprice)}')  # per-head, matches NL understanding
        except ValueError:
            pass
    return "".join(" and " + p for p in parts)


def _understood_yql(c, hits, source="", include_terms=None, extra=""):
    # HARD filters = genuine, EXPLICIT constraints (dietary, allergen exclusions, spice, budget,
    # and 'with X' inclusions). Cuisine is deliberately NOT a hard filter: it's often INFERRED
    # from a dish name, and filtering on it would drop an exact BM25 match (e.g. "Smoked Brisket
    # Sliders" with no cuisine tag). Cuisine only broadens the vector leg via graph expansion.
    filt = []
    for d in c.get("dietary") or []:
        filt.append(f'dietary contains "{d}"')
    for a in c.get("exclude_allergens") or []:
        filt.append(f'!(allergens contains "{a}")')
    if c.get("spice_min") is not None:
        filt.append(f'spice_level >= {int(c["spice_min"])}')
    if c.get("max_price_pp"):
        filt.append(f'price_pp < {float(c["max_price_pp"])}')
    if include_terms:  # "with meat" -> the dish must contain one of the expanded meat ingredients
        ors = " or ".join(f'ingredients contains "{t}"' for t in sorted(set(include_terms)))
        filt.append(f"({ors})")
    where = "(userQuery() or ({targetHits:200}nearestNeighbor(embedding,q)))" + "".join(" and " + f for f in filt)
    where += _src_filter(source, "dish") + (extra or "")   # extra = manual UI facet filters
    for clause in (extra or "").split(" and "):            # surface manual facets in applied_filters too
        c2 = clause.strip()
        if c2 and c2 not in filt:
            filt.append(c2)
    return f"select * from dish where {where} limit {hits}", filt


def _understood_run(concepts: dict, hits: int, source: str = "", extra: str = "") -> dict:
    """Given already-understood concepts, expand via the graph + run the hybrid Vespa query.
    `extra` carries the manual UI facet filters so they COMBINE with the understood constraints.
    Shared by /api/search (mode=understood) and the streaming endpoint so understanding runs once."""
    fetch = hits * 8
    graph = _graph_expand(concepts)
    free = concepts.get("free_text") or ""
    vec_text = (free + " " + " ".join(graph.get("added_terms", []))).strip()
    yql, applied = _understood_yql(concepts, fetch, source, include_terms=graph.get("include_terms"), extra=extra)
    params = {"yql": yql, "query": free, "ranking": "hybrid", "input.query(q)": _emb(vec_text or free)}
    _tv = time.perf_counter()
    resp = _vespa(params)
    vespa_ms = round((time.perf_counter() - _tv) * 1000, 1)
    return {"hits": _dedupe(_hits("dish", resp), hits), "graph": graph, "applied_filters": applied,
            "debug": {"yql": yql, "ranking": "hybrid", "keyword_query": free, "vector_query": vec_text},
            "vespa_ms": vespa_ms, "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}


@app.get("/api/search")
def search(q: str = "", mode: str = "hybrid", schema: str = "dish", hits: int = 8, source: str = "",
           cuisine: str = "", dietary: str = "", maxprice: str = ""):
    if schema not in SCHEMAS or not q.strip():
        return {"mode": mode, "hits": []}
    fetch = hits * 8
    t_start = time.perf_counter()
    # understood: understand ONCE, then delegate to the shared helper (also used by the stream)
    if mode == "understood" and schema == "dish":
        _tu = time.perf_counter()
        concepts = understand(q)
        understand_ms = round((time.perf_counter() - _tu) * 1000, 1)
        try:
            r = _understood_run(concepts, hits, source, extra=_facet_filter(schema, cuisine, dietary, maxprice))
            return {"mode": mode, "hits": r["hits"], "concepts": concepts, "applied_filters": r["applied_filters"],
                    "graph": r["graph"], "debug": r["debug"],
                    "timing": {"total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                               "vespa_ms": r["vespa_ms"], "understand_ms": understand_ms},
                    "total": r["total"]}
        except Exception as e:  # noqa: BLE001
            return {"mode": mode, "hits": [], "error": str(e), "concepts": concepts}

    extra = _src_filter(source, schema) + _facet_filter(schema, cuisine, dietary, maxprice)
    if mode == "keyword":
        params = {"yql": f"select * from {schema} where userQuery(){extra} limit {fetch}", "query": q, "ranking": "bm25"}
    elif mode == "semantic":
        params = {"yql": f"select * from {schema} where ({{targetHits:200}}nearestNeighbor(embedding,q)){extra} limit {fetch}",
                  "ranking": "semantic", "input.query(q)": _emb(q)}
    else:
        params = {"yql": f"select * from {schema} where (userQuery() or ({{targetHits:200}}nearestNeighbor(embedding,q))){extra} limit {fetch}",
                  "query": q, "ranking": "hybrid", "input.query(q)": _emb(q)}
    debug = {"yql": params.get("yql"), "ranking": params.get("ranking"),
             "keyword_query": params.get("query"),
             "vector_query": (q if mode in ("semantic", "hybrid") else None)}
    try:
        _tv = time.perf_counter()
        resp = _vespa(params)
        vespa_ms = round((time.perf_counter() - _tv) * 1000, 1)
        timing = {"total_ms": round((time.perf_counter() - t_start) * 1000, 1), "vespa_ms": vespa_ms, "understand_ms": None}
        return {"mode": mode, "hits": _dedupe(_hits(schema, resp), hits),
                "concepts": None, "applied_filters": [], "graph": None, "debug": debug,
                "timing": timing, "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}
    except Exception as e:  # noqa: BLE001
        return {"mode": mode, "hits": [], "error": str(e), "debug": debug}
