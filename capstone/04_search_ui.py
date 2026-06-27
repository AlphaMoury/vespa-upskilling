"""
Optional: a tiny web UI for a LIVE demo during your presentation.

Requires streamlit (not in the core requirements to keep setup light):
    uv pip install streamlit      # (inside the activated .venv)

Run it with streamlit, NOT plain python:
    streamlit run 04_search_ui.py

Then type a query and flip between bm25 / semantic / fusion to show the difference live.
"""

import streamlit as st

from app_package import connect_local, search_ids

st.set_page_config(page_title="Vespa Hybrid Search", layout="wide")
st.title("Vespa hybrid search demo")
st.caption("Same query, three rank profiles. Watch how hybrid (fusion) blends keyword + meaning.")


@st.cache_resource
def get_app():
    return connect_local()


app = get_app()

query = st.text_input("Query", value="How do fruits and vegetables help with asthma?")
hits = st.slider("Results per method", 3, 10, 5)

if query:
    cols = st.columns(3)
    for col, mode in zip(cols, ["bm25", "semantic", "fusion"]):
        with col:
            st.subheader(mode)
            try:
                results = search_ids(app, query, mode, hits=hits)
            except Exception as e:  # noqa: BLE001
                st.error(f"failed: {e}")
                continue
            for rank, (doc_id, title, rel) in enumerate(results, 1):
                st.markdown(f"**{rank}. {(title or '<no title>').strip()}**")
                st.caption(f"score {rel:.4f} · {doc_id}")
