"""
Thin FastAPI proxy between the React app and Vespa.
  GET /api/typeahead?q=chick            -> dish-name suggestions (Vespa prefix search)
  GET /api/search?q=...&mode=hybrid     -> ranked dishes (mode: keyword | semantic | hybrid)
  GET /api/health

Run (from ezcater-demo/server, using the capstone venv that has fastapi):
  ../../capstone/.venv/bin/python -m uvicorn main:app --reload --port 8000
"""

import re
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VESPA = "http://localhost:8080/search/"
app = FastAPI(title="EzCater x Vespa demo")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _vespa(params):
    r = requests.get(VESPA, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _hits(resp):
    out = []
    for h in resp.get("root", {}).get("children", []) or []:
        f = h.get("fields", {})
        mf = f.get("matchfeatures", {}) or {}
        out.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "description": f.get("description"),
            "cuisine": f.get("cuisine"),
            "dietary": f.get("dietary", []),
            "price": f.get("price"),
            "serves": f.get("serves"),
            "caterer": f.get("caterer_name"),
            "relevance": round(h.get("relevance", 0), 4),
            "bm25": round(mf.get("bm25sum", 0), 3) if mf else None,
            "semantic": round(mf.get("closeness(field, embedding)", 0), 3) if mf else None,
        })
    return out


def _emb(q):
    safe = q.replace("\\", " ").replace('"', " ")
    return f'embed(e5, "{safe}")'


@app.get("/api/health")
def health():
    try:
        n = _vespa({"yql": "select * from dish where true", "hits": 0})["root"]["fields"]["totalCount"]
        return {"ok": True, "dishes": n}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.get("/api/typeahead")
def typeahead(q: str = "", schema: str = "dish", limit: int = 6):
    term = re.sub(r"[^a-z0-9 ]", " ", q.strip().lower()).strip()
    if len(term) < 2:
        return {"suggestions": []}
    # n-gram substring match on the `grams` field (matches mid-word, case-insensitive)
    yql = f'select name, cuisine from {schema} where grams contains "{term}" limit 40'
    try:
        resp = _vespa({"yql": yql, "ranking": "unranked"})
    except Exception:  # noqa: BLE001
        return {"suggestions": []}
    seen, sugg = set(), []
    for h in resp.get("root", {}).get("children", []) or []:
        f = h.get("fields", {})
        name = f.get("name")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            sugg.append({"name": name, "cuisine": f.get("cuisine")})
        if len(sugg) >= limit:
            break
    return {"suggestions": sugg}


def _dedupe(hits, limit):
    seen, out = set(), []
    for h in hits:
        key = (h.get("name") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


@app.get("/api/search")
def search(q: str = "", mode: str = "hybrid", schema: str = "dish", hits: int = 8):
    if not q.strip():
        return {"mode": mode, "hits": []}
    fetch = hits * 8  # over-fetch, then dedupe identical dish names from different caterers
    if mode == "keyword":
        params = {"yql": f"select * from {schema} where userQuery() limit {fetch}",
                  "query": q, "ranking": "bm25"}
    elif mode == "semantic":
        params = {"yql": f"select * from {schema} where ({{targetHits:200}}nearestNeighbor(embedding,q)) limit {fetch}",
                  "ranking": "semantic", "input.query(q)": _emb(q)}
    else:  # hybrid
        params = {"yql": f"select * from {schema} where userQuery() or ({{targetHits:200}}nearestNeighbor(embedding,q)) limit {fetch}",
                  "query": q, "ranking": "hybrid", "input.query(q)": _emb(q)}
    try:
        resp = _vespa(params)
        return {"mode": mode, "hits": _dedupe(_hits(resp), hits),
                "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}
    except Exception as e:  # noqa: BLE001
        return {"mode": mode, "hits": [], "error": str(e)}
