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
import json
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VESPA = "http://localhost:8080/search/"
SCHEMAS = {"dish": {"title": "name"}, "covid": {"title": "title"}, "question": {"title": "text"}}

app = FastAPI(title="Vespa x LLM catering search")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    return {"free_text": q, "dietary": diet, "exclude_allergens": excl, "spice_min": spice,
            "cuisine": cuisine, "occasion": occ, "max_price_pp": mp, "headcount": hc, "method": "heuristic"}


def understand_llm(q: str) -> dict:
    """Use an LLM (Anthropic) to extract concepts — the production approach. Falls back to heuristic."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return understand_heuristic(q)
    try:
        import anthropic  # type: ignore
        client = anthropic.Anthropic(api_key=key)
        sys = ("Extract structured catering-search concepts from the query. Return ONLY JSON with keys: "
               "free_text (string, the semantic intent), dietary (array of: vegan,vegetarian,gluten-free,dairy-free,halal,kosher), "
               "exclude_allergens (array of: nuts,dairy,gluten,shellfish,soy), spice_min (0-3 or null), "
               "cuisine (one of Italian,Mexican,Japanese,Indian,Thai,Mediterranean,American,Chinese,Breakfast or null), "
               "occasion (array of: client,impressive,healthy,light,comfort,celebration,morning), "
               "max_price_pp (number or null, per-person budget), headcount (int or null).")
        msg = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=400,
                                     system=sys, messages=[{"role": "user", "content": q}])
        txt = msg.content[0].text
        data = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        data["free_text"] = data.get("free_text") or q
        data["method"] = "llm"
        return data
    except Exception:  # noqa: BLE001
        return understand_heuristic(q)


def understand(q: str) -> dict:
    return understand_llm(q)


@app.get("/api/understand")
def api_understand(q: str = ""):
    return understand(q) if q.strip() else {}


# ---------------- result mapping ----------------
def _map(schema, f):
    if schema == "dish":
        return dict(name=f.get("name"), sub=f.get("caterer_name"), tag=f.get("cuisine"),
                    price=f.get("price"), price_pp=f.get("price_pp"), badges=f.get("dietary", []),
                    spice=f.get("spice_level"), allergens=f.get("allergens", []), desc=f.get("description"))
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
        item["semantic"] = round(mf.get("closeness(field, embedding)", 0), 3) if mf else None
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
    return {"ok": any(counts.values()), "counts": counts, "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))}


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


def _understood_yql(c, hits):
    filt = []
    for d in c.get("dietary") or []:
        filt.append(f'dietary contains "{d}"')
    for a in c.get("exclude_allergens") or []:
        filt.append(f'!(allergens contains "{a}")')
    if c.get("spice_min") is not None:
        filt.append(f'spice_level >= {int(c["spice_min"])}')
    if c.get("cuisine"):
        filt.append(f'cuisine contains "{c["cuisine"]}"')
    if c.get("max_price_pp"):
        filt.append(f'price_pp < {float(c["max_price_pp"])}')
    where = "(userQuery() or ({targetHits:200}nearestNeighbor(embedding,q)))" + "".join(" and " + f for f in filt)
    return f"select * from dish where {where} limit {hits}", filt


@app.get("/api/search")
def search(q: str = "", mode: str = "hybrid", schema: str = "dish", hits: int = 8):
    if schema not in SCHEMAS or not q.strip():
        return {"mode": mode, "hits": []}
    fetch = hits * 8
    concepts, applied = None, []
    if mode == "understood" and schema == "dish":
        concepts = understand(q)
        yql, applied = _understood_yql(concepts, fetch)
        params = {"yql": yql, "query": concepts.get("free_text") or q, "ranking": "hybrid",
                  "input.query(q)": _emb(concepts.get("free_text") or q)}
    elif mode == "keyword":
        params = {"yql": f"select * from {schema} where userQuery() limit {fetch}", "query": q, "ranking": "bm25"}
    elif mode == "semantic":
        params = {"yql": f"select * from {schema} where ({{targetHits:200}}nearestNeighbor(embedding,q)) limit {fetch}",
                  "ranking": "semantic", "input.query(q)": _emb(q)}
    else:
        params = {"yql": f"select * from {schema} where userQuery() or ({{targetHits:200}}nearestNeighbor(embedding,q)) limit {fetch}",
                  "query": q, "ranking": "hybrid", "input.query(q)": _emb(q)}
    try:
        resp = _vespa(params)
        return {"mode": mode, "hits": _dedupe(_hits(schema, resp), hits),
                "concepts": concepts, "applied_filters": applied,
                "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}
    except Exception as e:  # noqa: BLE001
        return {"mode": mode, "hits": [], "error": str(e), "concepts": concepts}
