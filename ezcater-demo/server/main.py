"""
FastAPI proxy for the 3-index Vespa demo (dish / covid / question).
  GET /api/health                         -> per-index doc counts
  GET /api/typeahead?q=&schema=dish       -> substring suggestions (gram match)
  GET /api/search?q=&mode=&schema=&...     -> ranked results (keyword|semantic|hybrid) + filters

Run:  ../../capstone/.venv/bin/python -m uvicorn main:app --reload --port 8009
"""

import re
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VESPA = "http://localhost:8080/search/"

# per-schema config: the "title" field (for typeahead + display), and how to map a hit
SCHEMAS = {
    "dish":     {"title": "name",  "label": "Catering"},
    "covid":    {"title": "title", "label": "COVID research"},
    "question": {"title": "text",  "label": "Quora questions"},
}

app = FastAPI(title="Vespa use-case demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _vespa(params):
    r = requests.get(VESPA, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _emb(q):
    return f'embed(e5, "{q.replace(chr(92), " ").replace(chr(34), " ")}")'


def _map(schema, f, mf):
    """Normalize a hit from any schema into a common card shape."""
    if schema == "dish":
        return dict(name=f.get("name"), sub=f.get("caterer_name"), tag=f.get("cuisine"),
                    price=f.get("price"), badges=f.get("dietary", []), desc=f.get("description"))
    if schema == "covid":
        return dict(name=f.get("title") or "(untitled paper)", sub="COVID-19 research", tag=None,
                    price=None, badges=[], desc=(f.get("body") or "")[:240])
    return dict(name=f.get("text"), sub="Quora question", tag=None, price=None, badges=[], desc=None)


def _hits(schema, resp):
    out = []
    for h in resp.get("root", {}).get("children", []) or []:
        f = h.get("fields", {})
        mf = f.get("matchfeatures", {}) or {}
        item = _map(schema, f, mf)
        item["relevance"] = round(h.get("relevance", 0), 4)
        item["bm25"] = round(mf.get("bm25sum", 0), 2) if mf else None
        item["semantic"] = round(mf.get("closeness(field, embedding)", 0), 3) if mf else None
        out.append(item)
    return out


def _dedupe(hits, limit):
    seen, out = set(), []
    for h in hits:
        k = (h.get("name") or "").lower()
        if k in seen:
            continue
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
    return {"ok": any(v for v in counts.values()), "counts": counts}


@app.get("/api/typeahead")
def typeahead(q: str = "", schema: str = "dish", limit: int = 6):
    if schema not in SCHEMAS:
        return {"suggestions": []}
    term = re.sub(r"[^a-z0-9 ]", " ", q.strip().lower()).strip()
    if len(term) < 2:
        return {"suggestions": []}
    title = SCHEMAS[schema]["title"]
    yql = f'select {title} from {schema} where grams contains "{term}" limit 40'
    try:
        resp = _vespa({"yql": yql, "ranking": "unranked"})
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


def _filters(schema, cuisine, dietary, maxprice):
    """Build extra YQL filter clauses (dish facets only)."""
    if schema != "dish":
        return ""
    cl = []
    if cuisine:
        cl.append(f'cuisine contains "{cuisine}"')
    if dietary:
        for d in dietary.split(","):
            d = d.strip()
            if d:
                cl.append(f'dietary contains "{d}"')
    if maxprice:
        try:
            cl.append(f"price < {float(maxprice)}")
        except ValueError:
            pass
    return (" and " + " and ".join(cl)) if cl else ""


@app.get("/api/search")
def search(q: str = "", mode: str = "hybrid", schema: str = "dish", hits: int = 8,
           cuisine: str = "", dietary: str = "", maxprice: str = ""):
    if schema not in SCHEMAS or not q.strip():
        return {"mode": mode, "hits": []}
    fetch = hits * 8
    filt = _filters(schema, cuisine, dietary, maxprice)
    if mode == "keyword":
        recall = "userQuery()"
        extra = {"query": q, "ranking": "bm25"}
    elif mode == "semantic":
        recall = "({targetHits:200}nearestNeighbor(embedding,q))"
        extra = {"ranking": "semantic", "input.query(q)": _emb(q)}
    else:
        recall = "(userQuery() or ({targetHits:200}nearestNeighbor(embedding,q)))"
        extra = {"query": q, "ranking": "hybrid", "input.query(q)": _emb(q)}
    yql = f"select * from {schema} where {recall}{filt} limit {fetch}"
    try:
        resp = _vespa({"yql": yql, **extra})
        return {"mode": mode, "hits": _dedupe(_hits(schema, resp), hits),
                "total": resp.get("root", {}).get("fields", {}).get("totalCount", 0)}
    except Exception as e:  # noqa: BLE001
        return {"mode": mode, "hits": [], "error": str(e)}
