import { useState, useEffect, useRef, useCallback } from 'react'

const API = 'http://localhost:8009'

const INDEXES = {
  dish: {
    label: 'Catering', icon: '🍽️', accent: '#e35205', unit: 'dishes',
    placeholder: 'Try: healthy plant-based lunch for a client meeting…',
    examples: ['impressive client dinner', 'spicy food that is also gluten free', 'healthy lunch vegans and meat-eaters will both like', 'office breakfast that travels well'],
    filters: true,
  },
  covid: {
    label: 'COVID research', icon: '🦠', accent: '#1140d6', unit: 'papers',
    placeholder: 'Try: how is the virus transmitted through the air…',
    examples: ['airborne transmission of respiratory viruses', 'does vitamin D reduce severity', 'loss of smell and taste', 'remdesivir treatment outcomes'],
    filters: false,
  },
  question: {
    label: 'Quora questions', icon: '❓', accent: '#7b1fa2', unit: 'questions',
    placeholder: 'Try: how do I become a better programmer…',
    examples: ['how do I become a better programmer', 'best way to lose weight fast', 'how does bitcoin actually work', 'why is the sky blue'],
    filters: false,
  },
}
const CUISINES = ['Italian', 'Mexican', 'Japanese', 'Indian', 'Thai', 'Mediterranean', 'American', 'Chinese', 'Salads & Bowls', 'Breakfast']
const DIETS = ['vegan', 'vegetarian', 'gluten-free', 'dairy-free']
const DIET_COLOR = { vegan: '#2e7d32', vegetarian: '#558b2f', 'gluten-free': '#6a1b9a', 'dairy-free': '#00838f' }

function Card({ hit, showScores }) {
  const price = typeof hit.price === 'number' ? `$${Math.round(hit.price)}` : null
  return (
    <div className="card">
      <div className="card-top">
        <span className="dish">{hit.name}</span>
        {price && <span className="price">{price}</span>}
      </div>
      {(hit.sub || hit.tag) && (
        <div className="caterer">{hit.sub}{hit.tag ? <><span className="dot">·</span>{hit.tag}</> : null}</div>
      )}
      {hit.desc && <div className="desc">{hit.desc}</div>}
      {hit.badges?.length > 0 && (
        <div className="chips">
          {hit.badges.map((d) => (
            <span key={d} className="chip" style={{ color: DIET_COLOR[d] || '#555', background: (DIET_COLOR[d] || '#555') + '14', borderColor: (DIET_COLOR[d] || '#555') + '44' }}>{d}</span>
          ))}
        </div>
      )}
      {showScores && (hit.bm25 != null || hit.semantic != null) && (
        <div className="scores">keyword <b>{hit.bm25 ?? '—'}</b><span className="dot">·</span>meaning <b>{hit.semantic ?? '—'}</b><span className="dot">·</span>score <b>{hit.relevance}</b></div>
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
      {!loading && (data || []).map((h, i) => <Card key={`${h.name}-${i}`} hit={h} showScores={showScores} />)}
    </div>
  )
}

export default function App() {
  const [schema, setSchema] = useState('dish')
  const [q, setQ] = useState('')
  const [lastQ, setLastQ] = useState('')
  const [sugg, setSugg] = useState([])
  const [open, setOpen] = useState(false)
  const [kw, setKw] = useState(null)
  const [hy, setHy] = useState(null)
  const [loading, setLoading] = useState(false)
  const [health, setHealth] = useState(null)
  const [cuisine, setCuisine] = useState('')
  const [diet, setDiet] = useState([])
  const [maxprice, setMaxprice] = useState('')
  const cfg = INDEXES[schema]

  useEffect(() => {
    const load = () => fetch(`${API}/api/health`).then((r) => r.json()).then(setHealth).catch(() => {})
    load()
    const id = setInterval(load, 5000) // poll so tab counts climb live during indexing
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (!q.trim()) { setSugg([]); return }
    const t = setTimeout(() => {
      fetch(`${API}/api/typeahead?schema=${schema}&q=${encodeURIComponent(q)}`)
        .then((r) => r.json()).then((d) => setSugg(d.suggestions || [])).catch(() => setSugg([]))
    }, 110)
    return () => clearTimeout(t)
  }, [q, schema])

  const run = useCallback((query, opts = {}) => {
    const term = (query ?? q).trim()
    if (!term) return
    const c = opts.cuisine ?? cuisine, dt = opts.diet ?? diet, mp = opts.maxprice ?? maxprice
    setQ(term); setLastQ(term); setOpen(false); setLoading(true); setKw(null); setHy(null)
    const fp = schema === 'dish' ? `&cuisine=${encodeURIComponent(c)}&dietary=${dt.join(',')}&maxprice=${mp}` : ''
    Promise.all([
      fetch(`${API}/api/search?schema=${schema}&mode=keyword&q=${encodeURIComponent(term)}${fp}`).then((r) => r.json()),
      fetch(`${API}/api/search?schema=${schema}&mode=hybrid&q=${encodeURIComponent(term)}${fp}`).then((r) => r.json()),
    ]).then(([a, b]) => { setKw(a.hits || []); setHy(b.hits || []) })
      .catch(() => { setKw([]); setHy([]) }).finally(() => setLoading(false))
  }, [q, schema, cuisine, diet, maxprice])

  const switchIndex = (s) => {
    setSchema(s); setQ(''); setLastQ(''); setKw(null); setHy(null); setSugg([])
    setCuisine(''); setDiet([]); setMaxprice('')
  }
  const toggleDiet = (d) => {
    const next = diet.includes(d) ? diet.filter((x) => x !== d) : [...diet, d]
    setDiet(next); if (lastQ) run(lastQ, { diet: next })
  }
  const onCuisine = (v) => { setCuisine(v); if (lastQ) run(lastQ, { cuisine: v }) }
  const onPrice = (v) => { setMaxprice(v); if (lastQ) run(lastQ, { maxprice: v }) }

  const count = health?.counts?.[schema]

  return (
    <div className="app">
      <header className="hdr">
        <div className="brand"><span className="logo">ez</span>Cater<span className="x">×</span><span className="vespa">Vespa</span></div>
        <div className="tag">One engine, three use cases — <b>keyword</b> vs. <b>AI hybrid</b></div>
      </header>

      <div className="tabs">
        {Object.entries(INDEXES).map(([key, v]) => (
          <button key={key} className={`tab ${schema === key ? 'on' : ''}`}
            style={schema === key ? { borderColor: v.accent, color: v.accent } : {}}
            onClick={() => switchIndex(key)}>
            <span className="tab-ic">{v.icon}</span>{v.label}
            {health?.counts?.[key] != null && <span className="tab-n">{health.counts[key].toLocaleString()}</span>}
          </button>
        ))}
      </div>

      <div className="searchwrap">
        <input className="search" value={q} placeholder={cfg.placeholder}
          onChange={(e) => { setQ(e.target.value); setOpen(true) }}
          onKeyDown={(e) => { if (e.key === 'Enter') run() }}
          onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)} />
        <button className="go" style={{ background: cfg.accent }} onClick={() => run()}>Search</button>
        {open && sugg.length > 0 && (
          <div className="suggest">
            {sugg.map((s, i) => <div key={i} className="sg" onMouseDown={() => run(s.name)}><span className="sg-name">{s.name}</span></div>)}
          </div>
        )}
      </div>

      {cfg.filters && (
        <div className="filters">
          <select className="select" value={cuisine} onChange={(e) => onCuisine(e.target.value)}>
            <option value="">All cuisines</option>
            {CUISINES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          {DIETS.map((d) => (
            <button key={d} className={`fchip ${diet.includes(d) ? 'on' : ''}`} onClick={() => toggleDiet(d)}
              style={diet.includes(d) ? { borderColor: DIET_COLOR[d], color: DIET_COLOR[d], background: DIET_COLOR[d] + '14' } : {}}>{d}</button>
          ))}
          <select className="select" value={maxprice} onChange={(e) => onPrice(e.target.value)}>
            <option value="">Any price</option>
            <option value="100">under $100</option>
            <option value="200">under $200</option>
            <option value="300">under $300</option>
          </select>
        </div>
      )}

      <div className="examples">
        <span className="ex-lbl">try →</span>
        {cfg.examples.map((e) => <button key={e} className="ex" onClick={() => run(e)}>{e}</button>)}
        {count != null && <span className="idx-count">{count.toLocaleString()} {cfg.unit} indexed</span>}
      </div>

      {(kw || hy || loading) && (
        <div className="cols">
          <Column title="Keyword search" subtitle="BM25 — matches the exact words" accent="#9aa0a6" data={kw} loading={loading} />
          <Column title="AI Hybrid — Vespa" subtitle="keyword + meaning, fused (RRF)" accent={cfg.accent} data={hy} loading={loading} showScores />
        </div>
      )}

      <footer className="ftr">One Vespa engine · three indexes (catering · COVID papers · Quora) · hybrid = BM25 ⊕ e5 vectors, fused with reciprocal rank fusion. Same setup fed 522,931 docs on a laptop.</footer>
    </div>
  )
}
