"""
ONE Vespa app, THREE schemas (three indexes) = three Vespa use cases in one engine:
  - dish     : EzCater catering menu items  (commerce search + typeahead + filters)
  - covid    : trec-covid research papers   (medical / RAG-style retrieval)
  - question : Quora questions              (Q&A / duplicate-question, high volume)

All share one e5 embedder. Each has bm25 / semantic / hybrid rank profiles, a gram
field for substring typeahead, and (for dish) facets for filtering.
"""

from vespa.package import (
    ApplicationPackage, Schema, Document, Field, FieldSet,
    RankProfile, Function, GlobalPhaseRanking, HNSW, Component, Parameter,
)

EMBED_DIM = 384
NAMESPACE = "ezcater"


def _e5():
    return Component(id="e5", type="hugging-face-embedder", parameters=[
        Parameter("transformer-model", {"url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/e5-small-v2-int8.onnx"}),
        Parameter("tokenizer-model", {"url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/tokenizer.json"}),
    ])


def _rank_profiles(text_fields):
    bm25sum = " + ".join(f"bm25({f})" for f in text_fields)
    return [
        RankProfile(name="bm25", inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    functions=[Function(name="bm25sum", expression=bm25sum)], first_phase="bm25sum"),
        RankProfile(name="semantic", inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)"),
        RankProfile(name="hybrid", inherits="bm25", inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)",
                    global_phase=GlobalPhaseRanking(
                        expression="reciprocal_rank_fusion(bm25sum, closeness(field, embedding))", rerank_count=200),
                    match_features=["bm25sum", "closeness(field, embedding)"]),
    ]


def _gram(src):
    return Field(name="grams", type="string", indexing=[f"input {src}", "index"],
                 match=["gram", "gram-size: 2"], is_document_field=False)


def _emb(expr):
    return Field(name="embedding", type=f"tensor<float>(x[{EMBED_DIM}])",
                 indexing=[expr, "embed", "index", "attribute"],
                 ann=HNSW(distance_metric="angular"), is_document_field=False)


# ---------- 1) EzCater catering dishes (commerce: typeahead + filters) ----------
dish = Schema(
    name="dish",
    document=Document(fields=[
        Field(name="id", type="string", indexing=["summary"]),
        Field(name="name", type="string", indexing=["index", "summary"], index="enable-bm25"),
        Field(name="description", type="string", indexing=["index", "summary"], index="enable-bm25"),
        Field(name="cuisine", type="string", indexing=["index", "attribute", "summary"], index="enable-bm25"),
        Field(name="course", type="string", indexing=["attribute", "summary"]),
        Field(name="dietary", type="array<string>", indexing=["attribute", "summary"]),
        Field(name="serves", type="int", indexing=["attribute", "summary"]),
        Field(name="price", type="float", indexing=["attribute", "summary"]),
        Field(name="price_pp", type="float", indexing=["attribute", "summary"]),
        Field(name="caterer_name", type="string", indexing=["attribute", "summary"]),
        Field(name="popularity", type="int", indexing=["attribute", "summary"]),
        # --- food ontology (LLM-enriched in prod; see build_ontology.py) ---
        Field(name="spice_level", type="int", indexing=["attribute", "summary"]),
        Field(name="flavor", type="string", indexing=["attribute", "summary"]),
        Field(name="occasion", type="array<string>", indexing=["index", "attribute", "summary"]),
        Field(name="ingredients", type="array<string>", indexing=["index", "attribute", "summary"]),
        Field(name="allergens", type="array<string>", indexing=["attribute", "summary"]),
        _gram("name"),
        _emb('input name . " " . input cuisine . " " . input description'),
    ]),
    fieldsets=[FieldSet(name="default", fields=["name", "description", "cuisine", "ingredients", "occasion"])],
    rank_profiles=_rank_profiles(["name", "description"]),
)

# ---------- 2) trec-covid research papers (medical retrieval) ----------
covid = Schema(
    name="covid",
    document=Document(fields=[
        Field(name="id", type="string", indexing=["summary"]),
        Field(name="title", type="string", indexing=["index", "summary"], index="enable-bm25"),
        Field(name="body", type="string", indexing=["index", "summary"], index="enable-bm25", bolding=True),
        _gram("title"),
        _emb('input title . " " . input body'),
    ]),
    fieldsets=[FieldSet(name="default", fields=["title", "body"])],
    rank_profiles=_rank_profiles(["title", "body"]),
)

# ---------- 3) Quora questions (Q&A, high volume) ----------
question = Schema(
    name="question",
    document=Document(fields=[
        Field(name="id", type="string", indexing=["summary"]),
        Field(name="text", type="string", indexing=["index", "summary"], index="enable-bm25"),
        _gram("text"),
        _emb('input text'),
    ]),
    fieldsets=[FieldSet(name="default", fields=["text"])],
    rank_profiles=_rank_profiles(["text"]),
)

package = ApplicationPackage(name="ezcater", schema=[dish, covid, question], components=[_e5()])
