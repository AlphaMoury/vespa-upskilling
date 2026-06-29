import { useState, useEffect, useRef, useCallback } from 'react'

const API = 'http://localhost:8009'

const EXAMPLES = [
  'healthy plant-based lunch for a client meeting',
  'gluten free options',
  'something spicy for the team',
  'comfort food crowd pleaser',
  'light breakfast for the office',
  'finger food for a celebration',
]

const DIET = {
  vegan: '#2e7d32', vegetarian: '#558b2f', 'gluten-free': '#6a1b9a',
  'dairy-free': '#00838f', 'nut-free': '#5d4037',
}

function Card({ hit, showScores }) {
  const price = typeof hit.price === 'number' ? `$${Math.round(hit.price)}` : null
  return (
    <div className="card">
      <div className="card-top">
        <span className="dish">{hit.name}</span>
        {price && <span className="price">{price}</span>}
      </div>
      <div className="caterer">
        {hit.caterer}<span className="dot">·</span>{hit.cuisine}
        {hit.serves ? <><span className="dot">·</span>serves {hit.serves}</> : null}
      </div>
      {hit.description && <div className="desc">{hit.description}</div>}
      <div className="chips">
        {(hit.dietary || []).map((d) => (
          <span key={d} className="chip"
            style={{ color: DIET[d] || '#555', background: (DIET[d] || '#555') + '14', borderColor: (DIET[d] || '#555') + '44' }}>
            {d}
          </span>
        ))}
      </div>
      {showScores && (hit.bm25 != null || hit.semantic != null) && (
        <div className="scores">
          keyword <b>{hit.bm25 ?? '—'}</b><span className="dot">·</span>
          meaning <b>{hit.semantic ?? '—'}</b><span className="dot">·</span>
          score <b>{hit.relevance}</b>
        </div>
      )}
    </div>
  )
}

function Column({ title, subtitle, accent, data, loading, showScores }) {
  return (
    <div className="col">
      <div className="col-head" style={{ borderColor: accent }}>
        <div className="col-title" style={{ color: accent }}>{title}</div>
        <div className="col-sub">{subtitle}</div>
      </div>
      {loading && <div className="muted">searching…</div>}
      {!loading && data && data.length === 0 && <div className="muted">No good matches.</div>}
      {!loading && (data || []).map((h, i) => <Card key={`${h.id}-${i}`} hit={h} showScores={showScores} />)}
    </div>
  )
}

export default function App() {
  const [q, setQ] = useState('')
  const [sugg, setSugg] = useState([])
  const [open, setOpen] = useState(false)
  const [kw, setKw] = useState(null)
  const [hy, setHy] = useState(null)
  const [loading, setLoading] = useState(false)
  const [health, setHealth] = useState(null)

  useEffect(() => {
    fetch(`${API}/api/health`).then((r) => r.json()).then(setHealth).catch(() => {})
  }, [])

  useEffect(() => {
    if (!q.trim()) { setSugg([]); return }
    const t = setTimeout(() => {
      fetch(`${API}/api/typeahead?q=${encodeURIComponent(q)}`)
        .then((r) => r.json()).then((d) => setSugg(d.suggestions || [])).catch(() => setSugg([]))
    }, 110)
    return () => clearTimeout(t)
  }, [q])

  const run = useCallback((query) => {
    const term = (query ?? q).trim()
    if (!term) return
    setQ(term); setOpen(false); setLoading(true); setKw(null); setHy(null)
    Promise.all([
      fetch(`${API}/api/search?mode=keyword&q=${encodeURIComponent(term)}`).then((r) => r.json()),
      fetch(`${API}/api/search?mode=hybrid&q=${encodeURIComponent(term)}`).then((r) => r.json()),
    ]).then(([a, b]) => { setKw(a.hits || []); setHy(b.hits || []) })
      .catch(() => { setKw([]); setHy([]) })
      .finally(() => setLoading(false))
  }, [q])

  return (
    <div className="app">
      <header className="hdr">
        <div className="brand"><span className="logo">ez</span>Cater
          <span className="x">×</span><span className="vespa">Vespa</span></div>
        <div className="tag">
          Catering search: <b>keyword</b> vs. <b>AI hybrid</b>
          {health?.dishes ? <span className="pill">{health.dishes} dishes · 2 indexes</span> : null}
        </div>
      </header>

      <div className="searchwrap">
        <input
          className="search"
          value={q}
          placeholder="Try: healthy plant-based lunch for a client meeting…"
          onChange={(e) => { setQ(e.target.value); setOpen(true) }}
          onKeyDown={(e) => { if (e.key === 'Enter') run() }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
        <button className="go" onClick={() => run()}>Search</button>
        {open && sugg.length > 0 && (
          <div className="suggest">
            {sugg.map((s, i) => (
              <div key={i} className="sg" onMouseDown={() => run(s.name)}>
                <span className="sg-name">{s.name}</span>
                <span className="sg-cui">{s.cuisine}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="examples">
        <span className="ex-lbl">try a natural request →</span>
        {EXAMPLES.map((e) => <button key={e} className="ex" onClick={() => run(e)}>{e}</button>)}
      </div>

      {(kw || hy || loading) && (
        <div className="cols">
          <Column title="Keyword search" subtitle="BM25 — matches the exact words" accent="#9aa0a6" data={kw} loading={loading} />
          <Column title="AI Hybrid — Vespa" subtitle="keyword + meaning, fused (RRF)" accent="#e35205" data={hy} loading={loading} showScores />
        </div>
      )}

      <footer className="ftr">
        One Vespa engine · two indexes (caterers + dishes) · hybrid = BM25 ⊕ e5 vectors, fused with reciprocal rank fusion.
      </footer>
    </div>
  )
}
