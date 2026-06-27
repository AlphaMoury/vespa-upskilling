"""
The Vespa application, defined in Python (pyvespa).

This single file is the "schema + ranking" of our app. The other scripts import
`package` from here to deploy it, and import the helpers to connect/query.

Read this top to bottom — it maps 1:1 to a native Vespa `.sd` schema. To SEE that
native form, run:  python -c "from app_package import package; package.to_files('generated_app')"
and open generated_app/schemas/doc.sd

Verified against the official pyvespa hybrid-search quickstart (2026-06).
"""

from vespa.package import (
    ApplicationPackage,
    Schema,
    Document,
    Field,
    FieldSet,
    RankProfile,
    Function,
    GlobalPhaseRanking,
    HNSW,
    Component,
    Parameter,
)

# ---- constants shared across scripts -------------------------------------------------
APP_NAME = "hybridsearch"
SCHEMA = "doc"
NAMESPACE = "tutorial"
EMBED_DIM = 384  # e5-small-v2 outputs 384 dimensions. MUST match the tensor below.

# ---- the application package ---------------------------------------------------------
package = ApplicationPackage(
    name=APP_NAME,
    schema=[
        Schema(
            name=SCHEMA,
            document=Document(
                fields=[
                    # plain returnable id
                    Field(name="id", type="string", indexing=["summary"]),
                    # full-text fields: tokenized inverted index + BM25 enabled + returned
                    Field(
                        name="title",
                        type="string",
                        indexing=["index", "summary"],
                        index="enable-bm25",
                    ),
                    Field(
                        name="body",
                        type="string",
                        indexing=["index", "summary"],
                        index="enable-bm25",
                        bolding=True,
                    ),
                    # the vector field. Vespa generates this itself from title+body at feed
                    # time via the `embed` step, so we do NOT feed it (is_document_field=False).
                    Field(
                        name="embedding",
                        type=f"tensor<float>(x[{EMBED_DIM}])",
                        indexing=[
                            'input title . " " . input body',  # concatenate the two text fields
                            "embed",      # run the embedder component (e5, defined below)
                            "index",      # build the HNSW graph for ANN
                            "attribute",  # keep in memory so ranking can read it (closeness)
                        ],
                        ann=HNSW(distance_metric="angular"),  # cosine-style, correct for e5
                        is_document_field=False,
                    ),
                ]
            ),
            # a user query searches title+body together
            fieldsets=[FieldSet(name="default", fields=["title", "body"])],
            rank_profiles=[
                # 1) pure keyword: classic BM25 over both text fields
                RankProfile(
                    name="bm25",
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    functions=[
                        Function(name="bm25sum", expression="bm25(title) + bm25(body)")
                    ],
                    first_phase="bm25sum",
                ),
                # 2) pure semantic: cosine closeness between query and doc embedding
                RankProfile(
                    name="semantic",
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)",
                ),
                # 3) HYBRID: first-phase recalls on vector similarity, then the global-phase
                #    fuses BM25 rank + vector rank with Reciprocal Rank Fusion (RRF).
                #    RRF uses only rank POSITIONS, so it sidesteps BM25-vs-cosine scale issues.
                RankProfile(
                    name="fusion",
                    inherits="bm25",  # reuse the bm25sum function
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)",
                    global_phase=GlobalPhaseRanking(
                        expression="reciprocal_rank_fusion(bm25sum, closeness(field, embedding))",
                        rerank_count=1000,
                    ),
                ),
            ],
        )
    ],
    components=[
        # Vespa's built-in HuggingFace embedder, running e5-small-v2 (int8 ONNX).
        # It embeds documents at feed time AND the query at search time — so you never
        # touch a vector yourself; you feed and search with plain text.
        Component(
            id="e5",
            type="hugging-face-embedder",
            parameters=[
                Parameter(
                    "transformer-model",
                    {
                        "url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/e5-small-v2-int8.onnx"
                    },
                ),
                Parameter(
                    "tokenizer-model",
                    {
                        "url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/tokenizer.json"
                    },
                ),
            ],
        )
    ],
)


# ---- helpers shared by 02 / 03 / 04 --------------------------------------------------
def connect_local():
    """Connect to an already-running local Vespa (started by 01_deploy_and_feed.py),
    WITHOUT redeploying. Returns a live Vespa app object."""
    from vespa.application import Vespa

    return Vespa(url="http://localhost", port=8080)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').strip()


def embed_body(text: str) -> dict:
    """Build the request body that turns the query text into the query(q) tensor,
    server-side, using the e5 embedder. Used for semantic + hybrid queries."""
    return {"input.query(q)": f'embed(e5, "{_esc(text)}")'}


# top-k document ids for a query under a given rank profile -----------------------------
def search_ids(session, text: str, mode: str, hits: int = 10):
    """Run one query and return a list of (id, title, relevance), highest first.
    mode is one of: 'bm25', 'semantic', 'fusion'.
    `targetHits` (ANN candidate pool) is kept generous so hybrid has real recall;
    `hits` only limits how many we display/score."""
    target = max(100, hits)
    if mode == "bm25":
        yql = "select * from sources * where userQuery()"
        resp = session.query(yql=yql, query=text, ranking="bm25", hits=hits)
    elif mode == "semantic":
        yql = (
            f"select * from sources * where "
            f"({{targetHits:{target}}}nearestNeighbor(embedding,q))"
        )
        resp = session.query(yql=yql, ranking="semantic", hits=hits, body=embed_body(text))
    elif mode == "fusion":
        yql = (
            f"select * from sources * where userQuery() or "
            f"({{targetHits:{target}}}nearestNeighbor(embedding,q))"
        )
        resp = session.query(
            yql=yql, query=text, ranking="fusion", hits=hits, body=embed_body(text)
        )
    else:
        raise ValueError(f"unknown mode: {mode}")

    out = []
    for hit in resp.hits:
        fields = hit.get("fields", {})
        out.append((fields.get("id"), fields.get("title", ""), hit.get("relevance")))
    return out
