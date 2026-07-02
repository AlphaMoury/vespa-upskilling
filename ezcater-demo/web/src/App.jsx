import { useState, useEffect, useCallback } from 'react'

const API = 'http://localhost:8009'

const INDEXES = {
  dish: {
    label: 'Catering', icon: '🍽️', accent: '#e35205', unit: 'items',
    placeholder: 'Try: an elegant Mediterranean spread to impress a client, nothing with nuts…',
    examples: ['an elegant Mediterranean spread to impress a client, nothing with nuts', 'spicy vegan lunch for 15 under $20 a head', 'plant-based finger food with a kick, nut-free', 'office breakfast that travels well'],
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
const SRC_LABEL = { synthetic: 'sample', 'hf:foodcom': 'Food.com', 'hf:datahive': 'DataHive', 'pdf:vision': 'menu PDF', 'pdf:text': 'menu PDF' }
const STOP = new Set(['the', 'a', 'an', 'and', 'or', 'for', 'with', 'to', 'of', 'in', 'on', 'that', 'is', 'are', 'be', 'some', 'something', 'nothing', 'no', 'not', 'my', 'our', 'me', 'i', 'we', 'it', 'this', 'these', 'those', 'at', 'as', 'by', 'from', 'per', 'head', 'people', 'client', 'a', 'under', 'over', 'less', 'than'])

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const tokenize = (q) => [...new Set((q || '').toLowerCase().match(/[a-z][a-z-]{2,}/g) || [])].filter((w) => !STOP.has(w))

// Highlight query keywords (yellow) and graph/semantic terms (teal) inside result text.
function highlight(text, kw = [], sem = []) {
  if (!text) return text
  const kwset = new Set(kw.map((x) => x.toLowerCase()))
  const all = [...new Set([...kw, ...sem])].filter((t) => t && t.length >= 3).sort((a, b) => b.length - a.length)
  if (!all.length) return text
  let re
  try { re = new RegExp('\\b(' + all.map(esc).join('|') + ')\\b', 'ig') } catch { return text }
  const out = []
  let last = 0, m
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const cls = kwset.has(m[0].toLowerCase()) ? 'hl-kw' : 'hl-sem'
    out.push(<mark key={m.index} className={cls}>{m[0]}</mark>)
    last = m.index + m[0].length
    if (m.index === re.lastIndex) re.lastIndex++
  }
  out.push(text.slice(last))
  return out
}

function Card({ hit, showScores, hl }) {
  const pp = typeof hit.price_pp === 'number' && hit.price_pp > 0 ? `$${hit.price_pp.toFixed(2)}/head` : null
  const total = typeof hit.price === 'number' && hit.price > 0 ? `$${Math.round(hit.price)} total` : null
  const src = hit.source && SRC_LABEL[hit.source]
  const kw = hl?.kw || [], sem = hl?.sem || []
  return (
    <div className="card">
      <div className="card-top">
        <span className="dish">{highlight(hit.name, kw, sem)}</span>
        {(pp || total) && <span className="price">{pp || total}</span>}
      </div>
      {(hit.sub || hit.tag || (pp && total)) && (
        <div className="caterer">
          {hit.sub}{hit.tag ? <><span className="dot">·</span>{hit.tag}</> : null}
          {pp && total ? <><span className="dot">·</span>{total}</> : null}
        </div>
      )}
      {hit.desc && <div className="desc">{highlight(hit.desc, kw, sem)}</div>}
      <div className="chips">
        {hit.badges?.map((d) => (
          <span key={d} className="chip" style={{ color: DIET_COLOR[d] || '#555', background: (DIET_COLOR[d] || '#555') + '14', borderColor: (DIET_COLOR[d] || '#555') + '44' }}>{d}</span>
        ))}
        {hit.allergens?.length > 0 && (
          <span className="chip alg" title={`contains: ${hit.allergens.join(', ')}`}>⚠ {hit.allergens.join(' · ')}</span>
        )}
        {src && <span className="chip src">{src}</span>}
      </div>
      {showScores && (hit.bm25 != null || hit.semantic != null) && (
        <div className="scores">BM25 <b>{hit.bm25 ?? '—'}</b><span className="dot">·</span>meaning <b>{hit.semantic ?? '—'}</b><span className="dot">·</span>score <b>{hit.relevance}</b></div>
      )}
    </div>
  )
}

function Column({ title, subtitle, ranking, accent, data, loading, showScores, hl, took, loadingLabel, stream }) {
  return (
    <div className="col">
      <div className="col-head" style={{ borderColor: accent }}>
        <div className="col-title" style={{ color: accent }}>
          {title}{took?.total_ms != null && <span className="col-time" title="server time: understanding (if any) + Vespa retrieval">⏱ {took.total_ms} ms</span>}
        </div>
        <div className="col-sub">{subtitle}{ranking && <> · <span className="col-rank">ranked by {ranking}</span></>}</div>
      </div>
      {loading && stream ? (
        stream.phase === 'retrieving' ? <div className="col-loading">ranking in Vespa…</div>
          : stream.cached ? <div className="stream-cache">⚡ reused from cache — no LLM call</div>
            : <div className="stream-box">
                <div className="stream-lbl">🧠 LLM generating concepts…</div>
                <pre className="stream-json">{stream.text}<span className="stream-cursor">▍</span></pre>
              </div>
      ) : loading ? (
        <div className="col-loading">{loadingLabel || 'searching…'}</div>
      ) : null}
      {!loading && data && data.length === 0 && <div className="muted">No good matches.</div>}
      {!loading && (data || []).map((h, i) => <Card key={`${h.name}-${i}`} hit={h} showScores={showScores} hl={hl} />)}
    </div>
  )
}

// "What the query understanding + ontology graph did" — the frontend LLM use case, made visible.
function Concepts({ c, graph }) {
  if (!c) return null
  const isLLM = c.method === 'llm'
  const hit = c.cache === 'hit'
  const added = graph?.added_terms || []
  const algIng = graph?.allergen_ingredients || {}
  const bare = !c.dietary?.length && !c.exclude_allergens?.length && c.spice_min == null && !c.cuisine && !c.max_price_pp && !c.headcount
  return (
    <div className="concepts">
      <div className="cc-head">
        <span className="cc-title">🧠 understood</span>
        <span className={`cc-method ${isLLM ? 'llm' : 'det'}`}>{isLLM ? 'LLM' : 'deterministic parser'}</span>
        {c.cache && <span className={`cc-cache ${hit ? 'hit' : 'miss'}`}>{hit ? (c._cache === 'exact' ? '⚡ exact cache · instant' : `⚡ semantic cache · sim ${c._sim}`) : 'cache miss · called LLM'}</span>}
        {c.free_text && <span className="cc-free">“{c.free_text}”</span>}
      </div>
      <div className="cc-chips">
        {(c.dietary || []).map((d) => <span key={d} className="cc-chip good">{d}</span>)}
        {(c.exclude_allergens || []).map((a) => <span key={a} className="cc-chip bad">no {a}</span>)}
        {c.spice_min != null && <span className="cc-chip hot">🌶 spice ≥ {c.spice_min}</span>}
        {c.cuisine && <span className="cc-chip">{c.cuisine}</span>}
        {c.max_price_pp && <span className="cc-chip">≤ ${c.max_price_pp}/head</span>}
        {c.headcount && <span className="cc-chip">for {c.headcount}</span>}
        {bare && <span className="cc-none">no structured constraints — pure semantic search</span>}
      </div>
      {(added.length > 0 || Object.keys(algIng).length > 0) && (
        <div className="cc-graph">
          <span className="cc-glabel">⛓ ontology graph</span>
          {added.map((t) => <span key={t} className="cc-chip add" title="related term added to the semantic query">+{t}</span>)}
          {Object.entries(algIng).map(([a, ings]) => (
            <span key={a} className="cc-chip bad" title={ings.join(', ')}>no {a} → −{ings.length} ingredients</span>
          ))}
        </div>
      )}
    </div>
  )
}

// A polished SVG of the ontology subgraph the query touched: cuisine → featured
// ingredients, and each excluded allergen → its ingredients. Rendered from the search
// response (no dependency) — pill hubs, curved edges, gradients, soft shadows.
function GraphView({ concepts, graph }) {
  const groups = []
  if (concepts?.cuisine && graph?.added_terms?.length)
    groups.push({ hub: concepts.cuisine, kind: 'cuisine', targets: graph.added_terms.slice(0, 8) })
  for (const [a, ings] of Object.entries(graph?.allergen_ingredients || {}))
    if (ings?.length) groups.push({ hub: `no ${a}`, kind: 'allergen', targets: ings.slice(0, 8) })
  if (!groups.length) return null

  const colW = 340, rowH = 40, padTop = 28, CW = 7.7
  const maxT = Math.max(...groups.map((g) => g.targets.length))
  const H = padTop * 2 + (maxT - 1) * rowH + 20
  const W = groups.length * colW
  return (
    <div className="graphwrap">
      <div className="gv-cap">⛓ ontology graph · the query expanded into related concepts</div>
      <svg className="graphsvg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="gvCuisine" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#17a08e" /><stop offset="1" stopColor="#00695c" /></linearGradient>
          <linearGradient id="gvAllergen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#e35b5b" /><stop offset="1" stopColor="#c62828" /></linearGradient>
          <filter id="gvShadow" x="-30%" y="-40%" width="160%" height="180%"><feDropShadow dx="0" dy="1.5" stdDeviation="2" floodColor="#0b1a2a" floodOpacity="0.16" /></filter>
        </defs>
        {groups.map((g, gi) => {
          const originX = gi * colW
          const hubW = Math.min(150, g.hub.length * CW + 34), hubH = 42
          const hubX = originX + 16, hubCY = padTop + ((g.targets.length - 1) * rowH) / 2 + 10
          const hubRight = hubX + hubW, tx = originX + 196
          const grad = g.kind === 'cuisine' ? 'url(#gvCuisine)' : 'url(#gvAllergen)'
          return (
            <g key={gi}>
              {g.targets.map((t, ti) => {
                const ty = padTop + ti * rowH + 10
                return <path key={`e${t}`} d={`M ${hubRight} ${hubCY} C ${hubRight + 48} ${hubCY}, ${tx - 48} ${ty}, ${tx} ${ty}`} className={`gv-edge ${g.kind}`} />
              })}
              <rect x={hubX} y={hubCY - hubH / 2} width={hubW} height={hubH} rx={hubH / 2} fill={grad} filter="url(#gvShadow)" />
              <text x={hubX + hubW / 2} y={hubCY + 4} className="gv-hublabel">{g.hub}</text>
              {g.targets.map((t, ti) => {
                const ty = padTop + ti * rowH + 10
                const w = Math.min(172, t.length * CW + 46)
                return (
                  <g key={t}>
                    <rect x={tx} y={ty - 14} width={w} height="28" rx="14" className={`gv-node ${g.kind}`} filter="url(#gvShadow)" />
                    <circle cx={tx + 15} cy={ty} r="3.6" className={`gv-dot ${g.kind}`} />
                    <text x={tx + 27} y={ty + 4} className="gv-nodelabel">{t}</text>
                  </g>
                )
              })}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

const CONCEPT_KEYS = ['dietary', 'exclude_allergens', 'spice_min', 'cuisine', 'occasion', 'max_price_pp', 'headcount']
function prettyConcepts(c) {
  const o = {}
  for (const k of CONCEPT_KEYS) {
    const v = c[k]
    if (v == null || (Array.isArray(v) && v.length === 0)) continue
    o[k] = v
  }
  return o
}

// A collapsible accordion step.
function Step({ n, title, badge, open = true, children }) {
  return (
    <details className="pstep" open={open}>
      <summary className="pstep-sum">
        <span className="pstep-n">{n}</span><span className="pstep-t">{title}</span>
        {badge && <span className="pstep-badge">{badge}</span>}<span className="pstep-caret">▾</span>
      </summary>
      <div className="pstep-body">{children}</div>
    </details>
  )
}

// The whole pipeline, elegant + collapsible: query → LLM output → graph → the actual
// Vespa query each approach ran.
function Pipeline({ q, understand, concepts, graph, runs }) {
  if (!runs?.length) return null
  const added = graph?.added_terms || []
  const algIng = graph?.allergen_ingredients || {}
  const hasExp = added.length || Object.keys(algIng).length
  const hit = concepts?.cache === 'hit'
  return (
    <details className="pipe">
      <summary className="pipe-sum">
        <span className="pipe-title">⚙️ How this search works</span>
        <span className="pipe-hint">{understand ? 'query → LLM → ontology graph → hybrid retrieval' : 'keyword vs. hybrid retrieval'}</span>
      </summary>
      <div className="pipe-body">
        <Step n="1" title="Query">
          <div className="pipe-q">“{q}”</div>
        </Step>
        {understand && concepts && (
          <Step n="2" title="LLM query understanding" badge={concepts.method === 'llm' ? 'LLM' : 'deterministic'}>
            <div className="pipe-desc">
              The model reads the free-text query and returns structured search concepts.{' '}
              {hit
                ? <>This one was <b>reused from the semantic cache</b> (cosine sim {concepts._sim}) — <b>no LLM call</b>.</>
                : <>The result is cached by intent embedding, so paraphrases reuse it — one LLM call per intent.</>}
            </div>
            <pre className="pipe-json">{JSON.stringify(prettyConcepts(concepts), null, 2)}</pre>
          </Step>
        )}
        {understand && hasExp && (
          <Step n="3" title="Ontology graph expansion">
            <div className="pipe-desc">Concepts are expanded through the food ontology graph before retrieval — broadening the vector query and resolving allergen sets.</div>
            <div className="pipe-chips">
              {added.map((t) => <span key={t} className="cc-chip add">+{t}</span>)}
              {Object.entries(algIng).map(([a, ings]) => <span key={a} className="cc-chip bad" title={ings.join(', ')}>no {a} → −{ings.length}</span>)}
            </div>
          </Step>
        )}
        <Step n={understand ? '4' : '2'} title="Retrieval — the query sent to Vespa in each approach" open={!understand}>
          {runs.map((r, i) => (
            <div className="pipe-run" key={i}>
              <div className="pipe-run-h">
                <span className={`pipe-run-dot ${r.accent}`} /><b>{r.label}</b>
                <span className="pipe-run-meta">ranking: {r.resp?.debug?.ranking || '—'} · {(r.resp?.hits || []).length} shown</span>
              </div>
              {r.resp?.timing && (
                <div className="pipe-time">⏱ {r.resp.timing.understand_ms != null
                  ? <>understanding <b>{r.resp.timing.understand_ms} ms</b> + Vespa <b>{r.resp.timing.vespa_ms} ms</b> = <b>{r.resp.timing.total_ms} ms</b></>
                  : <>Vespa <b>{r.resp.timing.vespa_ms} ms</b></>}</div>
              )}
              {r.resp?.debug?.keyword_query && <div className="pipe-kv"><span>keyword</span><code>{r.resp.debug.keyword_query}</code></div>}
              {r.resp?.debug?.vector_query && <div className="pipe-kv"><span>vector</span><code>{r.resp.debug.vector_query}</code></div>}
              {r.resp?.debug?.yql && <pre className="pipe-yql">{r.resp.debug.yql}</pre>}
            </div>
          ))}
        </Step>
      </div>
    </details>
  )
}

// Unified, collapsed-by-default container: the understood concepts + the ontology graph
// live together here. A compact peek shows in the summary so you know what's inside.
function UnderstandPanel({ concepts, graph }) {
  if (!concepts) return null
  const hit = concepts.cache === 'hit'
  const bits = [
    ...(concepts.dietary || []),
    ...(concepts.exclude_allergens || []).map((a) => `no ${a}`),
    concepts.cuisine,
    concepts.spice_min != null ? `spice ≥ ${concepts.spice_min}` : null,
    concepts.max_price_pp ? `≤ $${concepts.max_price_pp}/head` : null,
    ...(concepts.occasion || []),
  ].filter(Boolean)
  return (
    <details className="upanel">
      <summary className="upanel-sum">
        <span className="upanel-title">🧠 Query understanding</span>
        <span className="upanel-peek">{bits.slice(0, 6).join('  ·  ') || 'no constraints — semantic only'}</span>
        {concepts.cache && <span className={`cc-cache ${hit ? 'hit' : 'miss'}`}>{hit ? (concepts._cache === 'exact' ? '⚡ exact' : '⚡ semantic') : 'LLM'}</span>}
        <span className="upanel-caret">▾</span>
      </summary>
      <div className="upanel-body">
        <Concepts c={concepts} graph={graph} />
        <GraphView concepts={concepts} graph={graph} />
      </div>
    </details>
  )
}

export default function App() {
  const [schema, setSchema] = useState('dish')
  const [q, setQ] = useState('')
  const [lastQ, setLastQ] = useState('')
  const [sugg, setSugg] = useState([])
  const [open, setOpen] = useState(false)
  const [cols, setCols] = useState([])    // progressive result columns (each fills independently)
  const [concepts, setConcepts] = useState(null)
  const [graph, setGraph] = useState(null)
  const [health, setHealth] = useState(null)
  const [sourceOpts, setSourceOpts] = useState({})
  const [cuisine, setCuisine] = useState('')
  const [diet, setDiet] = useState([])
  const [maxprice, setMaxprice] = useState('')
  const [source, setSource] = useState('')
  const [understand, setUnderstand] = useState(false)
  const cfg = INDEXES[schema]

  useEffect(() => {
    const load = () => {
      fetch(`${API}/api/health`).then((r) => r.json()).then(setHealth).catch(() => { })
      fetch(`${API}/api/sources?schema=dish`).then((r) => r.json()).then((d) => setSourceOpts(d.sources || {})).catch(() => { })
    }
    load()
    const id = setInterval(load, 5000)
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
    const src = opts.source ?? source, und = opts.understand ?? understand
    setQ(term); setLastQ(term); setOpen(false); setConcepts(null); setGraph(null)
    const sp = (schema === 'dish' && src) ? `&source=${encodeURIComponent(src)}` : ''
    const fp = schema === 'dish' ? `&cuisine=${encodeURIComponent(c)}&dietary=${dt.join(',')}&maxprice=${mp}` : ''
    const url = (mode) => `${API}/api/search?schema=${schema}&mode=${mode}&q=${encodeURIComponent(term)}${sp}${fp}`

    // build the columns for this run (2, or 3 when understanding is on)
    const specs = [
      { key: 'keyword', mode: 'keyword', title: 'Keyword', subtitle: 'BM25 — exact words', ranking: 'BM25', accentKind: 'muted' },
      { key: 'hybrid', mode: 'hybrid', title: 'AI Hybrid', subtitle: 'keyword + meaning', ranking: 'RRF(BM25, vector)', accentKind: (und && schema === 'dish') ? 'muted' : 'hero' },
    ]
    if (und && schema === 'dish')
      specs.push({ key: 'understood', mode: 'understood', title: 'Query understanding', subtitle: 'NL → filters + graph', ranking: 'RRF(BM25, vector) + hard filters', accentKind: 'hero' })

    // render all columns immediately in a loading state, then fill each as its fetch resolves
    setCols(specs.map((s) => ({ ...s, loading: true, data: null, resp: null, stream: '', phase: s.key === 'understood' ? 'understanding' : null })))
    specs.forEach((s, i) => {
      const retrieve = () => fetch(url(s.mode)).then((r) => r.json()).then((resp) => {
        setCols((prev) => prev.map((col, ci) => (ci === i ? { ...col, loading: false, data: resp.hits || [], resp } : col)))
        if (s.mode === 'understood') { setConcepts(resp.concepts || null); setGraph(resp.graph || null) }
      }).catch(() => setCols((prev) => prev.map((col, ci) => (ci === i ? { ...col, loading: false, data: [] } : col))))

      if (s.key === 'understood') {
        // one stream: tokens live → concepts → results (understanding + search run once, server-side)
        const es = new EventSource(`${API}/api/understand_stream?q=${encodeURIComponent(term)}&hits=8${sp}`)
        let closed = false
        es.onmessage = (ev) => {
          let m; try { m = JSON.parse(ev.data) } catch { return }
          if (m.type === 'token') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, stream: (c.stream || '') + m.text } : c)))
          else if (m.type === 'cached') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, streamCached: true, phase: 'retrieving' } : c)))
          else if (m.type === 'done') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, phase: 'retrieving' } : c)))
          else if (m.type === 'results') {
            closed = true; es.close()
            const resp = { concepts: m.concepts, graph: m.graph, debug: m.debug, timing: m.timing, hits: m.hits }
            setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, loading: false, data: m.hits || [], resp } : c)))
            setConcepts(m.concepts || null); setGraph(m.graph || null)
          }
        }
        es.onerror = () => { if (closed) return; closed = true; es.close(); retrieve() }  // fallback to plain fetch
      } else {
        retrieve()
      }
    })
  }, [q, schema, cuisine, diet, maxprice, source, understand])

  const switchIndex = (s) => {
    setSchema(s); setQ(''); setLastQ(''); setCols([]); setSugg([]); setConcepts(null); setGraph(null)
    setCuisine(''); setDiet([]); setMaxprice(''); setSource(''); setUnderstand(false)
  }
  const toggleDiet = (d) => { const next = diet.includes(d) ? diet.filter((x) => x !== d) : [...diet, d]; setDiet(next); if (lastQ) run(lastQ, { diet: next }) }
  const onCuisine = (v) => { setCuisine(v); if (lastQ) run(lastQ, { cuisine: v }) }
  const onPrice = (v) => { setMaxprice(v); if (lastQ) run(lastQ, { maxprice: v }) }
  const onSource = (v) => { setSource(v); if (lastQ) run(lastQ, { source: v }) }
  const toggleUnderstand = () => { const next = !understand; setUnderstand(next); if (lastQ) run(lastQ, { understand: next }) }

  const count = health?.counts?.[schema]
  const qTokens = tokenize(lastQ)
  const llm = health?.llm_status
  const srcEntries = Object.entries(sourceOpts).filter(([k]) => k && k !== '(none)').sort()

  return (
    <div className="app">
      <header className="hdr">
        <div className="brand"><span className="logo">ez</span>Cater<span className="x">×</span><span className="vespa">Vespa</span></div>
        <div className="tag">
          multi-source ingestion → food ontology → <b>hybrid</b> retrieval
          {llm && <span className="llm-pill" title="LLM structures data at index time (cached) and understands queries (semantic-cached by intent)">
            LLM · index {llm.index_llm ? '✓' : '✗'} · query {llm.query_llm ? '✓' : 'off'}
          </span>}
          {health?.graph && <span className="llm-pill" title="the food ontology graph: ingredients, allergens, diets, cuisines and their edges">
            ⛓ {health.graph.ingredient} ingredients · {health.graph.edges} edges
          </span>}
          {health?.semcache && <span className="llm-pill" title="semantic cache: cached query intents, hits vs misses">
            ⚡ {health.semcache.size} intents · {health.semcache.hits}/{health.semcache.hits + health.semcache.misses} hits
          </span>}
        </div>
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
          <select className="select" value={maxprice} onChange={(e) => onPrice(e.target.value)} title="Per-head budget (matches how 'under $X a head' is understood)">
            <option value="">Any price</option>
            <option value="10">under $10/head</option>
            <option value="15">under $15/head</option>
            <option value="25">under $25/head</option>
          </select>
          {srcEntries.length > 1 && (
            <select className="select src-select" value={source} onChange={(e) => onSource(e.target.value)} title="Filter by ingestion source (provenance)">
              <option value="">All sources</option>
              {srcEntries.map(([s, n]) => <option key={s} value={s}>{SRC_LABEL[s] || s} ({n})</option>)}
            </select>
          )}
        </div>
      )}

      {cfg.filters && (
        <div className="understand-row">
          <label className="switch">
            <input type="checkbox" checked={understand} onChange={toggleUnderstand} />
            <span className="slider" />
          </label>
          <span className="understand-lbl">
            🧠 Query understanding{understand && llm?.query_llm ? <span className="understand-on"> · LLM + ontology graph</span> : null}
            <span className="understand-sub">parse the natural-language query into structured filters and expand it via the ontology graph</span>
          </span>
        </div>
      )}

      <div className="examples">
        <span className="ex-lbl">try →</span>
        {cfg.examples.map((e) => <button key={e} className="ex" onClick={() => run(e)}>{e}</button>)}
        {count != null && <span className="idx-count">{count.toLocaleString()} {cfg.unit} indexed</span>}
      </div>

      {understand && schema === 'dish' && concepts && <UnderstandPanel concepts={concepts} graph={graph} />}
      {cols.some((c) => c.resp) && (
        <Pipeline q={lastQ} understand={understand && schema === 'dish'} concepts={concepts} graph={graph}
          runs={cols.map((c) => ({ label: c.title, accent: c.accentKind === 'hero' ? 'orange' : 'muted', resp: c.resp }))} />
      )}

      {cols.length > 0 && (
        <>
          <div className="hl-legend">
            <span><mark className="hl-kw">keyword</mark> matched query word (BM25)</span>
            {understand && schema === 'dish' && <span><mark className="hl-sem">graph term</mark> added via the ontology (vector leg)</span>}
          </div>
          <div className="cols" style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(0, 1fr))` }}>
            {cols.map((col) => {
              const hl = col.key === 'understood'
                ? { kw: tokenize(concepts?.free_text || lastQ), sem: graph?.added_terms || [] }
                : { kw: qTokens, sem: [] }
              return (
                <Column key={col.key} title={col.title} subtitle={col.subtitle} ranking={col.ranking}
                  accent={col.accentKind === 'hero' ? cfg.accent : '#9aa0a6'} data={col.data} loading={col.loading}
                  loadingLabel={col.key === 'understood' ? '🧠 understanding…' : 'searching…'}
                  stream={col.key === 'understood' ? { text: col.stream, phase: col.phase, cached: col.streamCached } : undefined}
                  showScores={col.key !== 'keyword'} hl={hl} took={col.resp?.timing} />
              )
            })}
          </div>
        </>
      )}

      <footer className="ftr">One Vespa engine · three indexes · multi-source ingestion (sample catalog · Food.com recipes · menu PDFs) → one schema → hybrid BM25 ⊕ e5 vectors (RRF). Allergens/diet are index-time enriched; excluded as hard filters.</footer>
    </div>
  )
}
