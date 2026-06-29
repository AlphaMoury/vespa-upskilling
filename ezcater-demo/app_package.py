"""
EzCater-style Vespa app: TWO schemas (two indexes) sharing one e5 embedder.
  - caterer : the supplier (name, cuisine, city, rating, min order)
  - dish    : a menu item (name, description, cuisine, dietary, price, serves)

Each schema has three rank profiles:
  - bm25     : pure keyword
  - semantic : pure vector
  - hybrid   : keyword + vector fused with reciprocal rank fusion (the one we demo)

Typeahead is done at query time with YQL prefix matching on `name` (no special schema).
"""

from vespa.package import (
    ApplicationPackage, Schema, Document, Field, FieldSet,
    RankProfile, Function, GlobalPhaseRanking, HNSW, Component, Parameter,
)

EMBED_DIM = 384
NAMESPACE = "ezcater"


def _e5():
    return Component(
        id="e5",
        type="hugging-face-embedder",
        parameters=[
            Parameter("transformer-model", {"url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/e5-small-v2-int8.onnx"}),
            Parameter("tokenizer-model", {"url": "https://data.vespa-cloud.com/sample-apps-data/e5-small-v2-int8/tokenizer.json"}),
        ],
    )


def _rank_profiles(text_fields):
    """bm25 / semantic / hybrid where bm25sum sums bm25 over the given text fields."""
    bm25sum = " + ".join(f"bm25({f})" for f in text_fields)
    return [
        RankProfile(name="bm25",
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    functions=[Function(name="bm25sum", expression=bm25sum)],
                    first_phase="bm25sum"),
        RankProfile(name="semantic",
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)"),
        RankProfile(name="hybrid", inherits="bm25",
                    inputs=[("query(q)", f"tensor<float>(x[{EMBED_DIM}])")],
                    first_phase="closeness(field, embedding)",
                    global_phase=GlobalPhaseRanking(
                        expression="reciprocal_rank_fusion(bm25sum, closeness(field, embedding))",
                        rerank_count=200),
                    match_features=["bm25sum", "closeness(field, embedding)"]),
    ]


# ---------- caterer schema ----------
caterer = Schema(
    name="caterer",
    document=Document(fields=[
        Field(name="id", type="string", indexing=["summary"]),
        Field(name="name", type="string", indexing=["index", "summary"], index="enable-bm25"),
        Field(name="cuisine", type="string", indexing=["index", "attribute", "summary"], index="enable-bm25"),
        Field(name="city", type="string", indexing=["attribute", "summary"]),
        Field(name="rating", type="float", indexing=["attribute", "summary"]),
        Field(name="min_order", type="int", indexing=["attribute", "summary"]),
        Field(name="lead_time", type="int", indexing=["attribute", "summary"]),
        Field(name="blurb", type="string", indexing=["index", "summary"], index="enable-bm25"),
        # n-gram field for substring typeahead (works mid-word, case-insensitive)
        Field(name="grams", type="string", indexing=["input name", "index"],
              match=["gram", "gram-size: 2"], is_document_field=False),
        Field(name="embedding", type=f"tensor<float>(x[{EMBED_DIM}])",
              indexing=['input name . " " . input cuisine . " " . input blurb', "embed", "index", "attribute"],
              ann=HNSW(distance_metric="angular"), is_document_field=False),
    ]),
    fieldsets=[FieldSet(name="default", fields=["name", "cuisine", "blurb"])],
    rank_profiles=_rank_profiles(["name", "cuisine", "blurb"]),
)

# ---------- dish schema ----------
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
        Field(name="caterer_id", type="string", indexing=["attribute", "summary"]),
        Field(name="caterer_name", type="string", indexing=["attribute", "summary"]),
        Field(name="popularity", type="int", indexing=["attribute", "summary"]),
        # n-gram field for substring typeahead (works mid-word, case-insensitive)
        Field(name="grams", type="string", indexing=["input name", "index"],
              match=["gram", "gram-size: 2"], is_document_field=False),
        Field(name="embedding", type=f"tensor<float>(x[{EMBED_DIM}])",
              indexing=['input name . " " . input cuisine . " " . input description', "embed", "index", "attribute"],
              ann=HNSW(distance_metric="angular"), is_document_field=False),
    ]),
    fieldsets=[FieldSet(name="default", fields=["name", "description", "cuisine"])],
    rank_profiles=_rank_profiles(["name", "description"]),
)

package = ApplicationPackage(name="ezcater", schema=[caterer, dish], components=[_e5()])
