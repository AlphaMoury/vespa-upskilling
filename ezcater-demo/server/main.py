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
import hashlib
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

# The Knowledge Graph studio (ontology model store, materializer, graph query engine).
# Guarded like the ingest imports above: the search API must still boot if it fails.
try:
    from kg_api import router as kg_router
    app.include_router(kg_router)
except Exception as e:  # noqa: BLE001
    print(f"!! knowledge-graph API unavailable: {e}")


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
# words that follow a negation cue but are NOT an ingredient to exclude
_XING_STOP = {"the", "a", "an", "any", "some", "all", "of", "extra", "more", "please",
              "thanks", "meat", "animal", "spicy", "gluten", "dairy", "problem", "worries"}


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
    # spice only when actually requested: guard against "not spicy"/"mild", and don't fire
    # on "hot dogs" (substring) — require the word "spicy"/"fiery" or "hot sauce"
    neg_spice = ("mild" in t) or bool(re.search(r"\b(?:not|non|no|less|without|zero)\b\W+(?:\w+\W+){0,2}spic", t))
    spice = 2 if (not neg_spice and (re.search(r"\bspic(?:y|ier)\b", t) or "hot sauce" in t or "fiery" in t)) else None
    cuisine = next((c.capitalize() for c in CUISINE_VOCAB if c in t), None)
    occ = [v for k, v in OCCASION.items() if k in t]
    occ = list(dict.fromkeys(occ))
    mp = None
    # a budget is MONEY — don't let "under 15 people" read as a $15 cap (negative lookahead on group nouns)
    _people = r"people|persons?|guests?|pax|heads?|attendees?|folks|of us"
    m = re.search(rf"(?:under|below|less than|<|max)\s*\$?\s*\b(\d+)\b(?!\s*(?:{_people}))", t) or re.search(r"\$\s*(\d+)\s*(?:/|per|a)\s*(?:head|person|pp)", t)
    if m:
        mp = float(m.group(1))
    hc = None
    # headcount is a number of PEOPLE — prefer an explicit group noun, and never read a
    # duration/time/price as a headcount ("for 2 hours", "for 30 minutes", "for 20 bucks")
    _units = r"hours?|hrs?|days?|minutes?|mins?|weeks?|months?|am|pm|dollars?|bucks?|percent|%|/"
    m2 = re.search(rf"\b(\d+)\s*(?:{_people})", t) or re.search(rf"(?:for|party of|team of|group of)\s+\b(\d+)\b(?!\s*(?:{_units}))", t)
    if m2:
        hc = int(m2.group(1))
    inc = []
    for m3 in re.finditer(r"\bwith\s+([a-z]+)", t):  # "with meat" -> include; "without/no" don't match
        w = m3.group(1)
        if w in INCLUDE_VOCAB and w not in inc:
            inc.append(w)
    # "no pickles" / "without onions" / "hold the cilantro" -> exclude a specific ingredient
    # (allergens are handled above; skip stopwords + protein categories that are dietary intents)
    xing = []
    for m4 in re.finditer(r"\b(?:no|without|hold(?: the)?|skip(?: the)?|minus|sans|nix(?: the)?|excluding)\s+([a-z][a-z]+)", t):
        w = m4.group(1)
        base = w[:-1] if (w.endswith("s") and len(w) > 3) else w
        if w in ALLERGENS or w in _XING_STOP or w in INCLUDE_VOCAB or base in xing:
            continue
        xing.append(base)
    return {"free_text": q, "dietary": diet, "exclude_allergens": excl, "exclude_ingredients": xing,
            "spice_min": spice, "cuisine": cuisine, "occasion": occ, "include": inc,
            "max_price_pp": mp, "headcount": hc, "method": "heuristic"}


_UNDERSTAND_SYS = (
    "Extract structured catering-search concepts from the query. Return ONLY JSON with keys: "
    "free_text (string, the semantic intent), dietary (array of: vegan,vegetarian,gluten-free,dairy-free,halal,kosher), "
    "exclude_allergens (array of: nuts,peanuts,dairy,gluten,shellfish,fish,soy,eggs,sesame — ONLY for a NEGATED "
    "allergen mention like 'no nuts'/'nut-free'/'without dairy'; a POSITIVE 'with nuts'/'with cheese' is NOT an "
    "exclusion), "
    "exclude_ingredients (array of specific NON-allergen ingredients the user does NOT want, lowercase, e.g. "
    "['pickles'] for 'no pickles', ['onion'] for 'without onions', ['cilantro'] for 'hold the cilantro'), "
    "spice_min (0-3 or null), "
    "cuisine (one of Italian,Mexican,Japanese,Indian,Thai,Mediterranean,American,Chinese,Breakfast or null). "
    "Set it ONLY when the query's WORDS actually name the cuisine — and then set it even if a dish is also "
    "named: 'italian pizza' -> Italian, 'mexican tacos' -> Mexican, 'thai green curry' -> Thai, "
    "'italian food' -> Italian. If the query names ONLY a dish, leave it null even though that dish belongs "
    "to a cuisine: 'pizza' -> null, 'tacos' -> null, 'smoked brisket sliders' -> null), "
    "occasion (array of: client,impressive,healthy,light,comfort,celebration,morning), "
    "include (array of ingredients/categories the user explicitly WANTS present, e.g. "
    "['meat'] for 'with meat', ['chicken'] for 'with chicken' — empty unless clearly requested. "
    "A POSITIVE 'with X' goes HERE even if X is an allergen: 'with nuts' -> ['nuts'], 'with cheese' -> ['cheese']. "
    "NEVER put a NEGATED item here: 'no meat' / 'without chicken' / 'meat-free' / 'hold the cheese' "
    "are EXCLUSIONS, not includes. Map 'no meat' / 'meatless' / 'no animal' to dietary ['vegetarian']), "
    "max_price_pp (number or null, per-person budget — a MONEY amount, not a group size), "
    "headcount (int or null — the number of PEOPLE to feed; NEVER a duration/time like "
    "'2 hours', a date, or a price)."
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
    # backfill only keys the LLM OMITTED entirely — never override a value it returned,
    # including a deliberate null/[]. (Overriding turned "not spicy" -> spice_min 2 and
    # "americano" -> cuisine American via the substring heuristic.)
    h = understand_heuristic(q)
    for k in ("dietary", "exclude_allergens", "exclude_ingredients", "cuisine", "occasion", "include", "max_price_pp", "headcount", "spice_min"):
        if k not in data:
            data[k] = h.get(k)
    return data


def _llm_query_on() -> bool:
    return bool(ingest_config and ingest_config.query_llm_enabled())


def _normalize_cuisine(c: dict, q: str) -> dict:
    """Cuisine is a CONTROLLED VOCABULARY governed by the user's literal words, not by LLM
    inference. Two failure modes this closes: the LLM inventing a cuisine the user never typed
    ("pizza" -> Italian, which then floods the vector query), and the LLM missing one they did
    ("italian pizza" -> null). Word-boundary matched, so "americano" never yields American.
    It also stops a cached cuisine leaking onto a different query."""
    t = (q or "").lower()
    c["cuisine"] = next((v.capitalize() for v in CUISINE_VOCAB if re.search(rf"\b{re.escape(v)}\b", t)), None)
    return c


def _cached_free_text(cached: dict, q: str) -> str:
    """Which text should we actually SEARCH for on a cache hit?

    An EXACT hit is the same query, so the LLM's cleaned free_text belongs to it. A SEMANTIC
    (paraphrase) hit belongs to a DIFFERENT query — reusing its free_text would search for the
    wrong thing ("tacos" reusing "pizza"). The structured CONSTRAINTS (diet/allergens/price)
    do transfer across paraphrases; the search text never does.
    """
    if cached.get("_cache") == "exact":
        return cached.get("free_text") or q
    return q


def understand(q: str) -> dict:
    """LLM-first (bounded by a SEMANTIC CACHE) when enabled; deterministic regex otherwise.
    The cache keys on the query's intent embedding, so paraphrases reuse one LLM answer."""
    if not _llm_query_on():
        return _normalize_cuisine(understand_heuristic(q), q)
    if ingest_semcache is not None:
        cached = ingest_semcache.get(q)
        if cached is not None:
            return _normalize_cuisine({**cached, "free_text": _cached_free_text(cached, q), "cache": "hit"}, q)
    result = understand_llm(q)
    if ingest_semcache is not None:
        ingest_semcache.put(q, result)
    return _normalize_cuisine({**result, "cache": "miss"}, q)


@app.get("/api/understand")
def api_understand(q: str = ""):
    return understand(q) if q.strip() else {}


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/api/understand_stream")
def understand_stream(q: str = "", hits: int = 8, source: str = "",
                      cuisine: str = "", dietary: str = "", maxprice: str = "", headcount: str = ""):
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
            concepts = _normalize_cuisine(understand_heuristic(q), q)
            yield _sse({"type": "cached", "concepts": concepts})
        else:
            cached = ingest_semcache.get(q) if ingest_semcache is not None else None
            if cached is not None:
                concepts = _normalize_cuisine({**cached, "free_text": _cached_free_text(cached, q), "cache": "hit"}, q)
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
                for k in ("dietary", "exclude_allergens", "exclude_ingredients", "cuisine", "occasion", "include", "max_price_pp", "headcount", "spice_min"):
                    if k not in concepts:  # backfill omitted keys only; respect deliberate LLM null/[]
                        concepts[k] = h.get(k)
                _normalize_cuisine(concepts, q)   # cuisine comes from the user's words, not inference
                if ingest_semcache is not None:
                    ingest_semcache.put(q, {k: v for k, v in concepts.items() if not str(k).startswith("_")})
                yield _sse({"type": "done", "concepts": concepts})
        understand_ms = round((time.perf_counter() - t0) * 1000, 1)
        # --- 2) run the hybrid Vespa search and stream the results back ---
        try:
            r = _understood_run(concepts, hits, source, extra=_facet_filter("dish", cuisine, dietary, maxprice, headcount))
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
            # Idempotent re-upload: derive each doc id from the normalized dish NAME (not the
            # vision-transcribed caterer, which varies run-to-run), and delete any prior pdf docs
            # sharing a name so re-uploading the same menu REPLACES it instead of duplicating.
            new_names = {re.sub(r"\s+", " ", (it.name or "").strip().lower()) for it in items if it.name}
            try:
                prev = _vespa({"yql": 'select * from dish where source matches "pdf.*"', "hits": 400, "timeout": "5s"})
                for h in prev.get("root", {}).get("children", []) or []:
                    did = (h.get("id") or "").split("::")[-1]
                    nm = re.sub(r"\s+", " ", (h.get("fields", {}).get("name") or "").strip().lower())
                    if did and nm in new_names:
                        requests.delete(f"{VESPA_DOC}/{NAMESPACE}/dish/docid/{did}", timeout=10)
            except Exception:  # noqa: BLE001
                pass
            for it in items:
                before = g.stats().get("ingredient", 0) if g else 0
                _enrich(it)
                added = (g.stats().get("ingredient", 0) - before) if g else 0
                doc = it.to_vespa_doc()
                doc["id"] = "pdfmenu-" + hashlib.sha1(re.sub(r"\s+", " ", (it.name or "").strip().lower()).encode()).hexdigest()[:12]
                doc["fields"]["id"] = doc["id"]
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


# ---------------- voice search: server-side speech-to-text (OpenAI Whisper) ----------------
@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Transcribe a short audio clip with Whisper — the reliable voice path that works in any
    browser (the browser Web Speech API depends on Google's/Apple's service, which is often
    blocked). Uses the same OpenAI key as the rest of the demo."""
    if ingest_llm is None:
        return {"text": "", "error": "no LLM provider configured"}
    data = await file.read()
    if not data:
        return {"text": "", "error": "empty audio"}
    try:
        import io
        buf = io.BytesIO(data)
        buf.name = file.filename or "voice.webm"
        client = ingest_llm._get_client()
        model = (ingest_config.OPENAI_TRANSCRIBE_MODEL if ingest_config and hasattr(ingest_config, "OPENAI_TRANSCRIBE_MODEL") else "whisper-1")
        r = client.audio.transcriptions.create(model=model, file=buf, language="en")
        return {"text": (getattr(r, "text", "") or "").strip()}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "error": str(e)}


# ---------------- ontology graph explorer (lazy expand/collapse) ----------------
def _gnode(nid: str) -> dict:
    kind, _, name = nid.partition(":")
    return {"id": nid, "kind": kind, "label": name}


_SEED_CATS = {"meat", "poultry", "seafood", "fish", "cheese"}


@app.get("/api/graph/roots")
def graph_roots(max_categories: int = 14):
    """Top-level anchors to start the explorer: cuisines, allergens, diets, and CATEGORIES.
    LLM growth now yields ~90 categories, so we show the seed categories plus the largest
    others (by member count) and cap the rest — the long tail is reachable via the search box."""
    if _get_graph is None:
        return {"nodes": [], "stats": {}}
    g = _get_graph().g
    roots = [_gnode(n) for n, d in g.nodes(data=True) if d.get("kind") in ("cuisine", "allergen", "diet")]
    cats = [(n, d.get("name"), g.out_degree(n)) for n, d in g.nodes(data=True) if d.get("kind") == "category"]
    seed = [n for n, nm, _ in cats if nm in _SEED_CATS]
    rest = [n for n, _ in sorted([(n, deg) for n, nm, deg in cats if nm not in _SEED_CATS],
                                 key=lambda x: -x[1])[:max_categories]]
    roots += [_gnode(n) for n in seed + rest]
    return {"nodes": roots, "stats": _get_graph().stats()}


_MODIFIERS = {"fresh", "chopped", "ground", "dried", "minced", "whole", "large", "small", "medium",
              "boneless", "skinless", "extra", "virgin", "organic", "cooked", "raw", "sliced",
              "grated", "shredded", "frozen", "canned", "stem", "stems", "leaf", "leaves", "part",
              "parts", "hot", "mild", "sweet", "unsalted", "salted", "low", "sodium", "lean", "ripe"}


def _canon(name: str) -> str:
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _MODIFIERS]
    words = [w[:-1] if (w.endswith("s") and len(w) > 3) else w for w in words]  # depluralize
    return " ".join(sorted(words))


@app.get("/api/graph/dupes")
def graph_dupes(limit: int = 15):
    """Entity-resolution candidates: ingredient nodes that normalize to the same canonical
    form (e.g. 'cilantro' / 'fresh cilantro' / 'fresh cilantro stems') — likely duplicates."""
    if _get_graph is None:
        return {"groups": [], "total_dupes": 0}
    from collections import defaultdict
    buckets = defaultdict(set)
    for _, d in _get_graph().g.nodes(data=True):
        if d.get("kind") == "ingredient":
            buckets[_canon(d["name"])].add(d["name"])
    groups = []
    for canon, names in buckets.items():
        if canon and len(names) > 1:
            ordered = sorted(names, key=lambda x: (len(x), x))
            groups.append({"keep": ordered[0], "members": ordered, "canon": canon})
    groups.sort(key=lambda x: -len(x["members"]))
    return {"groups": groups[:limit], "total_dupes": sum(len(x["members"]) - 1 for x in groups)}


@app.post("/api/graph/merge")
def graph_merge(keep: str = "", drop: str = ""):
    """Merge the `drop` ingredient node into `keep`: redirect its edges, remove the node, persist."""
    if _get_graph is None or not keep or not drop:
        return {"ok": False, "error": "missing args"}
    gobj = _get_graph()
    g = gobj.g
    kid, did = f"ingredient:{keep.lower().strip()}", f"ingredient:{drop.lower().strip()}"
    if kid not in g or did not in g or kid == did:
        return {"ok": False, "error": "node not found"}
    for _, t, dd in list(g.out_edges(did, data=True)):
        if t != kid:
            g.add_edge(kid, t, **dd)
    for s, _, dd in list(g.in_edges(did, data=True)):
        if s != kid:
            g.add_edge(s, kid, **dd)
    g.remove_node(did)
    gobj.save()
    return {"ok": True, "merged": drop, "into": keep, "stats": gobj.stats()}


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


def _facet_filter(schema: str, cuisine: str = "", dietary: str = "", maxprice: str = "", headcount: str = "") -> str:
    """Manual UI facets (dish only) -> YQL. Applies to keyword/semantic/hybrid + understood."""
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
    if headcount:  # only items whose platter serves at least the whole group
        try:
            parts.append(f'serves >= {int(float(headcount))}')
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
    for ing in c.get("exclude_ingredients") or []:   # "no pickles" -> drop dishes containing pickles
        v = re.sub(r'[^a-z0-9 -]', '', str(ing).lower()).strip()
        if not v:
            continue
        variants = {v, v[:-1]} if (v.endswith("s") and len(v) > 3) else {v, v + "s"}  # array contains is EXACT
        ors = " or ".join(f'ingredients contains "{x}"' for x in sorted(variants))
        filt.append(f"!({ors})")
    if c.get("spice_min"):  # 0 ("not spicy") is a no-op minimum — don't emit spice_level >= 0
        filt.append(f'spice_level >= {int(c["spice_min"])}')
    if c.get("max_price_pp"):
        filt.append(f'price_pp < {float(c["max_price_pp"])}')
    if c.get("headcount"):  # "for 15 people" -> a platter that serves the whole group
        filt.append(f'serves >= {int(c["headcount"])}')
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


def _browse(schema: str, hits: int, extra: str) -> dict:
    """No query, just hard filters — browse the filtered catalog. There is no BM25 or vector
    signal to rank on, so this is a match-all + filters scan returned unranked."""
    yql = f"select * from {schema} where true{extra} limit {hits}"
    t0 = time.perf_counter()
    try:
        resp = _vespa({"yql": yql, "ranking": "unranked"})
    except Exception as e:  # noqa: BLE001
        return {"mode": "browse", "hits": [], "error": str(e)}
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"mode": "browse", "hits": _dedupe(_hits(schema, resp), hits), "concepts": None,
            "applied_filters": [c.strip() for c in extra.split(" and ") if c.strip()], "graph": None,
            "debug": {"yql": yql, "ranking": "unranked", "keyword_query": None, "vector_query": None},
            "timing": {"total_ms": ms, "vespa_ms": ms, "understand_ms": None},
            "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}


@app.get("/api/search")
def search(q: str = "", mode: str = "hybrid", schema: str = "dish", hits: int = 8, source: str = "",
           cuisine: str = "", dietary: str = "", maxprice: str = "", headcount: str = ""):
    if schema not in SCHEMAS:
        return {"mode": mode, "hits": []}
    if not q.strip():   # filters with no query -> browse the filtered catalog
        only_filters = _src_filter(source, schema) + _facet_filter(schema, cuisine, dietary, maxprice, headcount)
        return _browse(schema, hits, only_filters) if only_filters else {"mode": mode, "hits": []}
    fetch = hits * 8
    t_start = time.perf_counter()
    # understood: understand ONCE, then delegate to the shared helper (also used by the stream)
    if mode == "understood" and schema == "dish":
        _tu = time.perf_counter()
        concepts = understand(q)
        understand_ms = round((time.perf_counter() - _tu) * 1000, 1)
        try:
            r = _understood_run(concepts, hits, source, extra=_facet_filter(schema, cuisine, dietary, maxprice, headcount))
            return {"mode": mode, "hits": r["hits"], "concepts": concepts, "applied_filters": r["applied_filters"],
                    "graph": r["graph"], "debug": r["debug"],
                    "timing": {"total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                               "vespa_ms": r["vespa_ms"], "understand_ms": understand_ms},
                    "total": r["total"]}
        except Exception as e:  # noqa: BLE001
            return {"mode": mode, "hits": [], "error": str(e), "concepts": concepts}

    extra = _src_filter(source, schema) + _facet_filter(schema, cuisine, dietary, maxprice, headcount)
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
