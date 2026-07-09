import { useState, useEffect, useCallback, useRef } from 'react'

const API = 'http://localhost:8009'
const PAGE_SIZE = 8              // results shown per page
const FETCH_N = 48              // window fetched per column, paginated client-side
const KIND_COLOR = { cuisine: '#00695c', allergen: '#c62828', diet: '#2e7d32', category: '#a15c00', ingredient: '#7c8698' }

const INDEXES = {
  dish: {
    label: 'Catering', icon: '🍽️', accent: '#e35205', unit: 'items',
    placeholder: 'Ask in plain English — e.g. an elegant Mediterranean spread with meat, nothing with nuts…',
    examples: ['an elegant Mediterranean spread with meat, nothing with nuts', 'spicy vegan lunch for 15 under $20 a head', 'nut-free dessert for a school event', 'office breakfast that travels well'],
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

// Deterministic dish -> real food PHOTO (no LLM — purely presentational). We map a dish to a
// food category by keyword, then show a bundled, curated photo (web/public/food/<cat>.jpg,
// sourced from TheMealDB / TheCocktailDB). Ordered most-specific first: dish FORM (taco, salad,
// soup…) beats a bare protein, which beats a raw ingredient.
const FOOD_RULES = [
  ['sushi', 'sushi|sashimi|maki|nigiri|poke'], ['burrito', 'burrito|quesadilla|enchilada|chimichanga'], ['taco', 'taco'],
  ['pizza', 'pizza|margherita|calzone'], ['pasta', 'pasta|spaghetti|linguine|fettuccine|penne|lasagna|carbonara|bolognese|gnocchi|mac and cheese|macaroni|alfredo|ravioli'],
  ['noodles', 'ramen|pho|noodle|lo mein|pad thai|udon|soba|chow mein'], ['soup', 'soup|bisque|chowder|broth|stew|gumbo|minestrone'],
  ['curry', 'curry|masala|tikka|korma|biryani| dal |jambalaya|vindaloo'], ['rice', 'risotto|paella|pilaf|fried rice|rice bowl|congee'],
  ['burger', 'burger|slider|cheeseburger'], ['wrap', 'shawarma|gyro|kebab|souvlaki|pita|wrap|spring roll'],
  ['sandwich', 'sandwich|panini|sub |club|blt|hoagie|baguette|bagel'], ['salad', 'salad|caprese|slaw|greens|caesar|cobb|tabbouleh|bowl'],
  ['dumpling', 'dumpling|gyoza|potsticker|empanada|samosa|wonton|pierogi'], ['falafel', 'falafel|kofta|meatball'],
  ['shrimp', 'shrimp|prawn|tempura'], ['lobster', 'lobster|crab|crawfish'], ['oyster', 'oyster|clam|mussel|scallop'],
  ['fish', 'salmon|tuna|cod|halibut|tilapia|trout|fish|seafood|ceviche|cedar|anchovy'],
  ['chicken', 'chicken|poultry|wing|drumstick|nugget'], ['turkey', 'turkey'],
  ['pork', 'pork|bacon|sausage|chorizo|ham|prosciutto|pastrami|ribs|bratwurst'],
  ['beef', 'steak|beef|brisket|barbacoa|ribeye|sirloin|filet|carne|bbq|barbecue|pulled|lamb|veal'],
  ['egg', 'omelet|frittata|quiche|scramble|benedict|egg|brunch'],
  ['tofu', 'tofu|tempeh|stir fry|stir-fry|teriyaki|edamame'], ['cheese', 'charcuterie|cheese board|mozzarella|burrata|brie|fondue|caprese'],
  ['pancake', 'pancake|waffle|french toast|crepe'], ['croissant', 'croissant|pastry|danish|scone'],
  ['cake', 'cake|cupcake|cheesecake|tiramisu'], ['pie', 'pie|tart|cobbler'], ['cookie', 'cookie|brownie|biscotti|macaron'],
  ['chocolate', 'chocolate|fudge|truffle|ganache'], ['icecream', 'ice cream|gelato|sorbet|sundae'], ['donut', 'donut|doughnut'],
  ['custard', 'custard|flan|pudding|panna cotta|dessert|honey|baklava'], ['fruit', 'berry|strawberry|fruit|parfait|melon'],
  ['coffee', 'coffee|espresso|latte|cappuccino|mocha'], ['tea', 'matcha|green tea|chai| tea'],
  ['beer', 'beer|ale|lager|ipa'], ['wine', 'wine|sangria|rose|prosecco|champagne'], ['cocktail', 'cocktail|margarita|mojito|punch|martini'],
  ['juice', 'juice|smoothie|lemonade|soda|milkshake|shake'],
  ['avocado', 'avocado|guacamole'], ['mushroom', 'mushroom|portobello|shiitake'], ['corn', 'corn|elote|cornbread'],
  ['potato', 'potato|fries|mashed|hash'], ['tomato', 'tomato|bruschetta|marinara'], ['chili', 'chili|jalape|spicy|buffalo|sriracha'],
  ['veg', 'broccoli|cauliflower|asparagus|brussels|kale|spinach|vegetable|veggie|carrot|zucchini|roasted veg'],
  ['eggplant', 'eggplant|aubergine|ratatouille|parmigiana'], ['beans', 'bean|chickpea|lentil|hummus|legume'],
  ['nuts', 'peanut|almond|cashew|pistachio|walnut|pecan| nut'], ['hotdog', 'hot dog|corn dog'],
]
const _FOOD_RE = FOOD_RULES.map(([cat, k]) => [cat, new RegExp('\\b(' + k + ')', 'i')])
// map a hit to a category key; defaults to a generic catering platter photo
function foodCat(hit) {
  const text = [hit.name, hit.desc, hit.tag, hit.sub, ...(hit.badges || [])].filter(Boolean).join(' ').toLowerCase()
  for (const [cat, re] of _FOOD_RE) if (re.test(text)) return cat
  const b = (hit.badges || []).map((x) => x.toLowerCase())
  if (b.includes('vegan') || b.includes('vegetarian')) return 'salad'
  return 'platter'
}
const foodImg = (hit) => `${import.meta.env.BASE_URL}food/${foodCat(hit)}.jpg`

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

function Card({ hit, showScores, hl, dish }) {
  const serves = typeof hit.serves === 'number' && hit.serves > 0 ? hit.serves : null
  const total = typeof hit.price === 'number' && hit.price > 0 ? Math.round(hit.price) : null
  const pp = typeof hit.price_pp === 'number' && hit.price_pp > 0 ? hit.price_pp.toFixed(2) : null
  const src = hit.source && SRC_LABEL[hit.source]
  const kw = hl?.kw || [], sem = hl?.sem || []
  return (
    <div className={`card${dish ? '' : ' card-noimg'}`}>
      {dish && <img className="food-img" src={foodImg(hit)} alt="" loading="lazy" aria-hidden="true"
        onError={(e) => { const f = `${import.meta.env.BASE_URL}food/platter.jpg`; if (!e.target.src.endsWith('platter.jpg')) e.target.src = f; else e.target.style.display = 'none' }} />}
      <div className="card-body">
        <div className="card-top">
          <span className="dish">{highlight(hit.name, kw, sem)}</span>
          {total != null && <span className="price">${total}</span>}
        </div>
        {(hit.sub || hit.tag || serves || pp) && (
          <div className="caterer">
            {hit.sub}{hit.tag ? <><span className="dot">·</span>{hit.tag}</> : null}
            {serves ? <><span className="dot">·</span><span className="serves">Serves {serves}</span></> : null}
            {pp ? <><span className="dot">·</span>${pp}/head</> : null}
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
    </div>
  )
}

function Column({ title, subtitle, ranking, accent, data, loading, showScores, hl, took, loadingLabel, stream, total, dish }) {
  return (
    <div className="col">
      <div className="col-head" style={{ borderColor: accent }}>
        <div className="col-title" style={{ color: accent }}>
          {title}{took?.total_ms != null && <span className="col-time" title="server time: understanding (if any) + Vespa retrieval">⏱ {took.total_ms} ms</span>}
        </div>
        <div className="col-sub">
          {!loading && typeof total === 'number' && (
            <span className="col-count" title="total matches in Vespa (showing the top few)"><b>{total.toLocaleString()}</b> {total === 1 ? 'result' : 'results'}</span>
          )}
          {!loading && typeof total === 'number' && <span className="dot">·</span>}
          {subtitle}{ranking && <> · <span className="col-rank">ranked by {ranking}</span></>}
        </div>
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
      {!loading && total === 0 && <div className="muted">No good matches.</div>}
      {!loading && (data || []).map((h, i) => <Card key={`${h.name}-${i}`} hit={h} showScores={showScores} hl={hl} dish={dish} />)}
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
  const bare = !c.dietary?.length && !c.include?.length && !c.exclude_allergens?.length && !c.exclude_ingredients?.length && c.spice_min == null && !c.cuisine && !c.max_price_pp && !c.headcount
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
        {(c.include || []).map((x) => <span key={`i${x}`} className="cc-chip incl">with {x}</span>)}
        {(c.exclude_allergens || []).map((a) => <span key={a} className="cc-chip bad">no {a}</span>)}
        {(c.exclude_ingredients || []).map((x) => <span key={`x${x}`} className="cc-chip bad">no {x}</span>)}
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
  const inc = graph?.include_terms || []
  const cuisineTerms = (graph?.added_terms || []).filter((t) => !inc.includes(t))
  if (concepts?.cuisine && cuisineTerms.length)
    groups.push({ hub: concepts.cuisine, kind: 'cuisine', targets: cuisineTerms.slice(0, 8) })
  if (inc.length)
    groups.push({ hub: `with ${(concepts?.include || []).join(', ') || 'meat'}`, kind: 'category', targets: inc.slice(0, 8) })
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
          <linearGradient id="gvCategory" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#d08a2e" /><stop offset="1" stopColor="#a15c00" /></linearGradient>
          <filter id="gvShadow" x="-30%" y="-40%" width="160%" height="180%"><feDropShadow dx="0" dy="1.5" stdDeviation="2" floodColor="#0b1a2a" floodOpacity="0.16" /></filter>
        </defs>
        {groups.map((g, gi) => {
          const originX = gi * colW
          const hubW = Math.min(150, g.hub.length * CW + 34), hubH = 42
          const hubX = originX + 16, hubCY = padTop + ((g.targets.length - 1) * rowH) / 2 + 10
          const hubRight = hubX + hubW, tx = originX + 196
          const grad = g.kind === 'cuisine' ? 'url(#gvCuisine)' : g.kind === 'category' ? 'url(#gvCategory)' : 'url(#gvAllergen)'
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
                ? (concepts._cache === 'exact'
                  ? <>This exact query was <b>reused from the cache</b> instantly — <b>no LLM call</b>.</>
                  : <>This paraphrase was <b>reused from the semantic cache</b>{concepts._sim ? ` (cosine sim ${concepts._sim})` : ''} — <b>no LLM call</b>.</>)
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
function UnderstandPanel({ concepts, graph, llmRaw }) {
  if (!concepts) return null
  const hit = concepts.cache === 'hit'
  const bits = [
    ...(concepts.dietary || []),
    ...(concepts.include || []).map((x) => `with ${x}`),
    ...(concepts.exclude_allergens || []).map((a) => `no ${a}`),
    ...(concepts.exclude_ingredients || []).map((x) => `no ${x}`),
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
        {concepts.method === 'llm' && concepts.cache === 'miss' && llmRaw && (
          <details className="llm-review">
            <summary>📝 review what the LLM generated (raw output)</summary>
            <pre className="stream-json">{llmRaw}</pre>
          </details>
        )}
      </div>
    </details>
  )
}

// Drag-and-drop a menu PDF -> STREAMS: rasterize -> vision transcribe -> per-item enrich +
// index (with graph additions) live -> searchable.
// Entity resolution — a small "gamified" pass over the ontology graph: the same real-world
// ingredient often lands as several nodes ("basil", "fresh basil", "basil leaves"). We surface
// look-alike clusters and let a human merge them (redirect edges, drop the duplicate) or keep
// them apart. This is exactly the messy-catalog problem a search platform faces at ingest.
function EntityResolution({ onDone }) {
  const [groups, setGroups] = useState(null)
  const [merged, setMerged] = useState(0)
  const [busy, setBusy] = useState(false)
  const [total, setTotal] = useState(0)

  const load = () => {
    setBusy(true)
    fetch(`${API}/api/graph/dupes?limit=40`).then((r) => r.json()).then((d) => {
      setGroups(d.groups || []); setTotal(d.total_dupes || 0); setBusy(false)
    }).catch(() => setBusy(false))
  }

  const merge = async (gi, member) => {
    const g = groups[gi]
    try {
      const r = await fetch(`${API}/api/graph/merge?keep=${encodeURIComponent(g.keep)}&drop=${encodeURIComponent(member)}`, { method: 'POST' }).then((x) => x.json())
      if (!r.ok) return
      setMerged((m) => m + 1)
      setGroups((gs) => {
        const next = gs.slice()
        const kept = next[gi].members.filter((m) => m !== member)
        if (kept.length <= 1) next.splice(gi, 1)
        else next[gi] = { ...next[gi], members: kept }
        return next
      })
      onDone?.()
    } catch { /* ignore */ }
  }

  const dismiss = (gi) => setGroups((gs) => gs.filter((_, i) => i !== gi))

  const diff = (member, keep) => {
    const kw = new Set(keep.toLowerCase().split(/\s+/))
    return member.toLowerCase().split(/\s+/).filter((w) => !kw.has(w))
  }

  return (
    <details className="entres" onToggle={(e) => { if (e.target.open && groups === null) load() }}>
      <summary className="entres-sum">🧩 Entity resolution<span className="entres-hint">merge look-alike ingredient nodes — sharpen the graph</span></summary>
      <div className="entres-body">
        <div className="entres-head">
          <span>The same ingredient lands as several nodes at ingest. Merge the look-alikes (edges are redirected) or keep them apart — your call.</span>
          <button className="entres-reload" onClick={load} disabled={busy}>{busy ? '…' : '↻ rescan'}</button>
        </div>
        {groups !== null && (
          <div className="entres-stat">
            {merged > 0 && <span className="entres-won">✨ merged {merged}</span>}
            <span>{groups.length} clusters · {total} redundant nodes to resolve</span>
          </div>
        )}
        {groups !== null && groups.length === 0 && (
          <div className="entres-clean">✓ graph is clean — no look-alike clusters left</div>
        )}
        <div className="entres-grid">
          {(groups || []).map((g, gi) => (
            <div key={g.canon} className="entres-card stream-in">
              <div className="entres-keep-row">
                <span className="entres-keep" title="the canonical node everything folds into">{g.keep}</span>
                <button className="entres-x" title="keep all of these as separate ingredients" onClick={() => dismiss(gi)}>keep separate</button>
              </div>
              <div className="entres-members">
                {g.members.filter((m) => m !== g.keep).map((m) => {
                  const d = diff(m, g.keep)
                  return (
                    <button key={m} className="entres-cand" onClick={() => merge(gi, m)} title={`merge "${m}" into "${g.keep}"`}>
                      <span className="entres-cand-name">{m}</span>
                      {d.length > 0 && <span className="entres-cand-diff">+{d.join(' ')}</span>}
                      <span className="entres-cand-go">⇢ merge</span>
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </details>
  )
}

function UploadMenu({ onDone }) {
  const [drag, setDrag] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [transcript, setTranscript] = useState('')
  const [items, setItems] = useState([])
  const [result, setResult] = useState(null)
  const inputRef = useRef(null)

  const send = async (files) => {
    const f = files?.[0]
    if (!f) return
    if (!f.name.toLowerCase().endsWith('.pdf')) { setResult({ ok: false, error: 'Please choose a .pdf file' }); return }
    setBusy(true); setResult(null); setStatus('uploading…'); setItems([]); setTranscript('')
    const live = []; let tx = ''
    try {
      const fd = new FormData(); fd.append('file', f)
      const resp = await fetch(`${API}/api/upload_pdf`, { method: 'POST', body: fd })
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n'); buf = parts.pop()
        for (const p of parts) {
          const line = p.trim(); if (!line.startsWith('data:')) continue
          let m; try { m = JSON.parse(line.slice(5)) } catch { continue }
          if (m.type === 'token') { tx += m.text; setTranscript(tx) }
          else if (m.type === 'status') setStatus(m.msg)
          else if (m.type === 'item') { live.push(m); setItems([...live]) }
          else if (m.type === 'done') { setResult({ ok: true, ...m }); setBusy(false); onDone?.() }
          else if (m.type === 'error') { setResult({ ok: false, error: m.error }); setBusy(false) }
        }
      }
    } catch (e) { setResult({ ok: false, error: String(e) }); setBusy(false) }
  }

  return (
    <details className="upload">
      <summary className="upload-sum">📄 Add a menu (PDF)<span className="upload-hint">upload → a vision-LLM transcribes it → instantly searchable</span></summary>
      <div className="upload-body">
        <div className={`dropzone ${drag ? 'drag' : ''} ${busy ? 'busy' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files) }}
          onClick={() => !busy && inputRef.current?.click()}>
          <input ref={inputRef} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => send(e.target.files)} />
          {busy ? <span className="drop-busy">🧠 {status || 'processing…'}</span>
            : <span>Drag a menu PDF here, or <b>click to choose</b></span>}
        </div>
        {busy && transcript && (
          <div className="stream-box">
            <div className="stream-lbl">🧠 vision-LLM transcribing the menu…</div>
            <pre className="stream-json">{transcript}<span className="stream-cursor">▍</span></pre>
          </div>
        )}
        {!busy && transcript && (
          <details className="upload-transcript">
            <summary>📝 review the vision-LLM transcript</summary>
            <pre className="stream-json">{transcript}</pre>
          </details>
        )}
        {result?.ok && (
          <div className="upload-ok-h">✓ Parsed <b>{result.count}</b> items from <b>{result.caterer || 'your menu'}</b> <span className="upload-method">{result.method}</span> · fed {result.fed} → now searchable</div>
        )}
        {result && !result.ok && <div className="upload-err">✕ {result.error}</div>}
        {items.length > 0 && (
          <div className="upload-items">
            {items.map((it, i) => (
              <div key={i} className="upload-item stream-in">
                <span className="ui-name">{it.name}</span>
                {typeof it.price === 'number' && it.price > 0 && <span className="ui-price">${Math.round(it.price)}</span>}
                {it.serves ? <span className="ui-serves">serves {it.serves}</span> : null}
                {it.graph_added > 0 && <span className="ui-graph" title="new ingredients this item added to the ontology graph">⛓ +{it.graph_added}</span>}
                {it.confidence < 0.7 && <span className="ui-flag" title="low confidence — flagged for review">review</span>}
                {(it.allergens || []).length > 0 && <span className="ui-alg">⚠ {it.allergens.join(' · ')}</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </details>
  )
}

// Interactive ontology graph: start from anchors (cuisines/allergens/diets), click a node
// to expand its neighbors, click again to collapse. Backed by vis-network.
function GraphExplorer() {
  const boxRef = useRef(null)
  const net = useRef(null)
  const nodesDS = useRef(null)
  const edgesDS = useRef(null)
  const expanded = useRef(new Set())
  const [stats, setStats] = useState(null)
  const [full, setFull] = useState(false)
  const [sq, setSq] = useState('')
  const [sugg, setSugg] = useState([])

  const toVisNode = (n) => {
    const anchor = n.kind !== 'ingredient'
    const c = KIND_COLOR[n.kind] || '#9aa0a6'
    return {
      id: n.id, label: n.label, _anchor: anchor,
      shape: 'box', margin: anchor ? 12 : 8, shapeProperties: { borderRadius: anchor ? 20 : 11 },
      color: {
        background: anchor ? c : '#ffffff', border: c,
        highlight: { background: anchor ? c : '#eef7f4', border: c },
        hover: { background: anchor ? c : '#f3f8f7', border: c },
      },
      font: { color: anchor ? '#ffffff' : '#2b3440', size: anchor ? 18 : 14, face: 'Inter, system-ui, sans-serif' },
      borderWidth: anchor ? 0 : 1.5, borderWidthSelected: 3,
    }
  }
  const REL = {
    FEATURES: { color: '#17a08e', dashes: false, label: 'features' },       // cuisine -> ingredient
    MEMBER: { color: '#a15c00', dashes: false, label: 'is a' },             // ingredient -> category
    CONTAINS: { color: '#c62828', dashes: true, label: 'contains' },        // ingredient -> allergen
    CONFLICTS: { color: '#c62828', dashes: true, label: 'not allowed in' }, // ingredient -> diet it violates
  }
  const toVisEdge = (e) => {
    const r = REL[e.rel] || { color: '#8a94a6', dashes: false, label: (e.rel || '').toLowerCase() }
    return {
      id: `${e.from}->${e.to}`, from: e.from, to: e.to,
      arrows: { to: { enabled: true, scaleFactor: 0.55 } }, dashes: r.dashes, label: r.label,
      font: { size: 11, color: '#6b7280', strokeWidth: 4, strokeColor: '#ffffff', align: 'middle' },
      color: { color: r.color, opacity: 0.5, highlight: '#5b4b8a', hover: '#5b4b8a' },
    }
  }

  const expand = (id) => {
    const nodes = nodesDS.current, edges = edgesDS.current
    if (!nodes || expanded.current.has(id)) return null
    expanded.current.add(id)
    return fetch(`${API}/api/graph/neighbors?node=${encodeURIComponent(id)}`).then((r) => r.json()).then((d) => {
      nodes.update((d.nodes || []).map(toVisNode)); edges.update((d.edges || []).map(toVisEdge))
    }).catch(() => {})
  }
  const collapse = (id) => {
    const nodes = nodesDS.current, edges = edgesDS.current
    expanded.current.delete(id)
    const nbrs = edges.get().filter((e) => e.from === id || e.to === id).map((e) => (e.from === id ? e.to : e.from))
    nbrs.forEach((nid) => {
      const nd = nodes.get(nid)
      if (!nd || nd._anchor || expanded.current.has(nid)) return
      if (edges.get().filter((e) => e.from === nid || e.to === nid).length <= 1) nodes.remove(nid)
    })
    const present = new Set(nodes.getIds())
    edges.remove(edges.get().filter((e) => !present.has(e.from) || !present.has(e.to)).map((e) => e.id))
  }
  const toggle = (id) => (expanded.current.has(id) ? collapse(id) : expand(id))

  useEffect(() => {
    let dead = false
    import('vis-network/standalone').then(({ Network, DataSet }) => {
      if (dead || !boxRef.current) return
      const nodes = new DataSet([]), edges = new DataSet([])
      nodesDS.current = nodes; edgesDS.current = edges
      net.current = new Network(boxRef.current, { nodes, edges }, {
        autoResize: true,
        physics: {
          solver: 'barnesHut',
          barnesHut: { gravitationalConstant: -8500, centralGravity: 0.22, springLength: 150, springConstant: 0.035, damping: 0.55, avoidOverlap: 0.5 },
          stabilization: { enabled: true, iterations: 200, updateInterval: 25 },
        },
        interaction: { hover: true, tooltipDelay: 100, zoomView: true, dragView: true, dragNodes: true },
        nodes: { shadow: { enabled: true, size: 6, x: 0, y: 2, color: 'rgba(15,23,41,0.14)' } },
        edges: { smooth: { enabled: true, type: 'dynamic' }, width: 1.6, hoverWidth: 0.8 },
      })
      net.current.on('click', (p) => { if (p.nodes.length) toggle(p.nodes[0]) })
      fetch(`${API}/api/graph/roots`).then((r) => r.json()).then((d) => {
        nodes.add((d.nodes || []).map(toVisNode)); setStats(d.stats)
        setTimeout(() => net.current?.fit?.({ animation: { duration: 500 } }), 300)
      }).catch(() => {})
    })
    return () => { dead = true; net.current?.destroy?.() }
  }, [])

  // typeahead search over all graph nodes
  useEffect(() => {
    if (!sq.trim()) { setSugg([]); return }
    const t = setTimeout(() => {
      fetch(`${API}/api/graph/search?q=${encodeURIComponent(sq)}`).then((r) => r.json())
        .then((d) => setSugg(d.nodes || [])).catch(() => setSugg([]))
    }, 130)
    return () => clearTimeout(t)
  }, [sq])

  // force vis-network to recompute its canvas size when entering/leaving fullscreen, + Escape
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') setFull(false) }
    window.addEventListener('keydown', onKey)
    const resize = () => { try { net.current?.setSize?.('100%', '100%'); net.current?.redraw?.(); net.current?.fit?.({ animation: { duration: 350 } }) } catch { /* noop */ } }
    const t1 = setTimeout(resize, 80)
    const t2 = setTimeout(resize, 320)   // second pass after layout settles
    return () => { window.removeEventListener('keydown', onKey); clearTimeout(t1); clearTimeout(t2) }
  }, [full])

  const focusNode = (n) => {
    const nodes = nodesDS.current
    if (!nodes) return
    if (!nodes.get(n.id)) nodes.add(toVisNode(n))
    const done = () => { net.current?.selectNodes?.([n.id]); net.current?.focus?.(n.id, { scale: 1.1, animation: { duration: 600 } }) }
    if (!expanded.current.has(n.id)) { const p = expand(n.id); (p && p.then ? p : Promise.resolve()).then(() => setTimeout(done, 80)) }
    else setTimeout(done, 20)
    setSq(''); setSugg([])
  }
  const reset = () => {
    const nodes = nodesDS.current, edges = edgesDS.current
    if (!nodes) return
    expanded.current = new Set(); edges.clear(); nodes.clear()
    fetch(`${API}/api/graph/roots`).then((r) => r.json()).then((d) => {
      nodes.add((d.nodes || []).map(toVisNode)); setTimeout(() => net.current?.fit?.({ animation: { duration: 400 } }), 200)
    })
  }

  return (
    <div className={`gexp ${full ? 'full' : ''}`}>
      <div className="gexp-bar">
        <span className="gexp-title">🕸 Ontology graph</span>
        <div className="gexp-search">
          <input value={sq} onChange={(e) => setSq(e.target.value)} placeholder="search a node — tahini, nuts, Italian…"
            onBlur={() => setTimeout(() => setSugg([]), 150)} />
          {sugg.length > 0 && (
            <div className="gexp-sugg">
              {sugg.map((n) => (
                <div key={n.id} className="gexp-sg" onMouseDown={() => focusNode(n)}>
                  <i style={{ background: KIND_COLOR[n.kind] || '#9aa0a6' }} /><span>{n.label}</span><em>{n.kind}</em>
                </div>
              ))}
            </div>
          )}
        </div>
        <button className="gexp-btn" onClick={() => net.current?.fit?.({ animation: { duration: 400 } })} title="fit to view">⤢ fit</button>
        <button className="gexp-btn" onClick={reset} title="reset to anchors">↺ reset</button>
        <button className="gexp-btn" onClick={() => setFull((v) => !v)}>{full ? '✕ exit' : '⛶ fullscreen'}</button>
        {stats && <span className="gexp-stats">{stats.ingredient} ingredients · {stats.cuisine} cuisines · {stats.allergen} allergens · {stats.edges} edges</span>}
      </div>
      <div className="gexp-legend">
        <span><i style={{ background: KIND_COLOR.cuisine }} />cuisine</span>
        <span><i style={{ background: KIND_COLOR.category }} />category</span>
        <span><i style={{ background: KIND_COLOR.allergen }} />allergen</span>
        <span><i style={{ background: KIND_COLOR.diet }} />diet</span>
        <span><i style={{ background: '#fff', borderColor: '#9aa0a6' }} />ingredient</span>
        <span className="gexp-tip">click a node to expand · click again to collapse</span>
      </div>
      <div ref={boxRef} className="gexp-canvas" />
    </div>
  )
}

export default function App() {
  const [schema, setSchema] = useState('dish')
  const [q, setQ] = useState('')
  const [lastQ, setLastQ] = useState('')
  const [sugg, setSugg] = useState([])
  const [open, setOpen] = useState(false)
  const [cols, setCols] = useState([])    // progressive result columns (each fills independently)
  const [page, setPage] = useState(0)     // client-side pagination over the fetched window
  const [concepts, setConcepts] = useState(null)
  const [graph, setGraph] = useState(null)
  const [health, setHealth] = useState(null)
  const [sourceOpts, setSourceOpts] = useState({})
  const [cuisine, setCuisine] = useState('')
  const [diet, setDiet] = useState([])
  const [maxprice, setMaxprice] = useState('')
  const [headcount, setHeadcount] = useState('')
  const [source, setSource] = useState('')
  const [understand, setUnderstand] = useState(false)
  const [showGraph, setShowGraph] = useState(false)
  const cfg = INDEXES[schema]

  const refreshHealth = useCallback(() => {
    fetch(`${API}/api/health`).then((r) => r.json()).then(setHealth).catch(() => { })
    fetch(`${API}/api/sources?schema=dish`).then((r) => r.json()).then((d) => setSourceOpts(d.sources || {})).catch(() => { })
  }, [])

  useEffect(() => {
    refreshHealth()
    const id = setInterval(refreshHealth, 5000)
    return () => clearInterval(id)
  }, [refreshHealth])

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
    const c = opts.cuisine ?? cuisine, dt = opts.diet ?? diet, mp = opts.maxprice ?? maxprice, hc = opts.headcount ?? headcount
    const src = opts.source ?? source, und = opts.understand ?? understand
    setQ(term); setLastQ(term); setOpen(false); setConcepts(null); setGraph(null)
    const sp = (schema === 'dish' && src) ? `&source=${encodeURIComponent(src)}` : ''
    const fp = schema === 'dish' ? `&cuisine=${encodeURIComponent(c)}&dietary=${dt.join(',')}&maxprice=${mp}&headcount=${hc}` : ''
    const url = (mode) => `${API}/api/search?schema=${schema}&mode=${mode}&q=${encodeURIComponent(term)}&hits=${FETCH_N}${sp}${fp}`

    // build the columns for this run (2, or 3 when understanding is on)
    const specs = [
      { key: 'keyword', mode: 'keyword', title: 'Keyword', subtitle: 'BM25 — exact words', ranking: 'BM25', accentKind: 'muted' },
      { key: 'hybrid', mode: 'hybrid', title: 'AI Hybrid', subtitle: 'keyword + meaning', ranking: 'RRF(BM25, vector)', accentKind: (und && schema === 'dish') ? 'muted' : 'hero' },
    ]
    if (und && schema === 'dish')
      specs.push({ key: 'understood', mode: 'understood', title: 'Query understanding', subtitle: 'NL → filters + graph', ranking: 'RRF(BM25, vector) + hard filters', accentKind: 'hero' })

    // render all columns immediately in a loading state, then fill each as its fetch resolves
    setCols(specs.map((s) => ({ ...s, loading: true, data: null, resp: null, stream: '', phase: s.key === 'understood' ? 'understanding' : null })))
    setPage(0)
    specs.forEach((s, i) => {
      const retrieve = () => fetch(url(s.mode)).then((r) => r.json()).then((resp) => {
        setCols((prev) => prev.map((col, ci) => (ci === i ? { ...col, loading: false, data: resp.hits || [], resp } : col)))
        if (s.mode === 'understood') { setConcepts(resp.concepts || null); setGraph(resp.graph || null) }
      }).catch(() => setCols((prev) => prev.map((col, ci) => (ci === i ? { ...col, loading: false, data: [] } : col))))

      if (s.key === 'understood') {
        // one stream: tokens live → concepts → results (understanding + search run once, server-side)
        const es = new EventSource(`${API}/api/understand_stream?q=${encodeURIComponent(term)}&hits=${FETCH_N}${sp}${fp}`)
        let closed = false
        es.onmessage = (ev) => {
          let m; try { m = JSON.parse(ev.data) } catch { return }
          if (m.type === 'token') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, stream: (c.stream || '') + m.text } : c)))
          else if (m.type === 'cached') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, streamCached: true, phase: 'retrieving' } : c)))
          else if (m.type === 'done') setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, phase: 'retrieving' } : c)))
          else if (m.type === 'results') {
            closed = true; es.close()
            const resp = { concepts: m.concepts, graph: m.graph, debug: m.debug, timing: m.timing, hits: m.hits, total: m.total }
            setCols((prev) => prev.map((c, ci) => (ci === i ? { ...c, loading: false, data: m.hits || [], resp } : c)))
            setConcepts(m.concepts || null); setGraph(m.graph || null)
          }
        }
        es.onerror = () => { if (closed) return; closed = true; es.close(); retrieve() }  // fallback to plain fetch
      } else {
        retrieve()
      }
    })
  }, [q, schema, cuisine, diet, maxprice, headcount, source, understand])

  const switchIndex = (s) => {
    setSchema(s); setQ(''); setLastQ(''); setCols([]); setSugg([]); setConcepts(null); setGraph(null)
    setCuisine(''); setDiet([]); setMaxprice(''); setHeadcount(''); setSource(''); setUnderstand(false)
  }
  const toggleDiet = (d) => { const next = diet.includes(d) ? diet.filter((x) => x !== d) : [...diet, d]; setDiet(next); if (lastQ) run(lastQ, { diet: next }) }
  const onCuisine = (v) => { setCuisine(v); if (lastQ) run(lastQ, { cuisine: v }) }
  const onPrice = (v) => { setMaxprice(v); if (lastQ) run(lastQ, { maxprice: v }) }
  const onHeadcount = (v) => { setHeadcount(v); if (lastQ) run(lastQ, { headcount: v }) }
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
          <select className="select" value={headcount} onChange={(e) => onHeadcount(e.target.value)} title="Only platters that serve at least this many (matches 'for N people')">
            <option value="">Any headcount</option>
            <option value="10">for 10 people</option>
            <option value="20">for 20 people</option>
            <option value="25">for 25 people</option>
            <option value="50">for 50 people</option>
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
        <span className="ex-lbl">Try</span>
        <div className="ex-scroll">
          {cfg.examples.map((e) => (
            <button key={e} className="ex" onClick={() => run(e)}>
              <span className="ex-ic">💬</span><span className="ex-tx">{e}</span>
            </button>
          ))}
        </div>
        {count != null && <span className="idx-count">{count.toLocaleString()} {cfg.unit} indexed</span>}
      </div>

      {/* 1) reasoning first: what was understood + how the search works (above results, as before) */}
      {understand && schema === 'dish' && concepts && <UnderstandPanel concepts={concepts} graph={graph} llmRaw={cols.find((c) => c.key === 'understood')?.stream} />}
      {cols.some((c) => c.resp) && (
        <Pipeline q={lastQ} understand={understand && schema === 'dish'} concepts={concepts} graph={graph}
          runs={cols.map((c) => ({ label: c.title, accent: c.accentKind === 'hero' ? 'orange' : 'muted', resp: c.resp }))} />
      )}

      {/* 2) the searches (results) */}
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
                  accent={col.accentKind === 'hero' ? cfg.accent : '#9aa0a6'}
                  data={col.loading ? col.data : (col.data || []).slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)}
                  loading={col.loading} loadingLabel={col.key === 'understood' ? '🧠 understanding…' : 'searching…'}
                  stream={col.key === 'understood' ? { text: col.stream, phase: col.phase, cached: col.streamCached } : undefined}
                  showScores={col.key !== 'keyword'} hl={hl} took={col.resp?.timing} total={col.resp?.total} dish={schema === 'dish'} />
              )
            })}
          </div>
          {(() => {
            const maxLen = Math.max(0, ...cols.map((c) => (c.data || []).length))
            const pages = Math.ceil(maxLen / PAGE_SIZE)
            if (pages <= 1) return null
            return (
              <div className="pager">
                <button className="pg-btn arw" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} aria-label="Previous page">←</button>
                {Array.from({ length: pages }, (_, i) => (
                  <button key={i} className={`pg-btn${i === page ? ' on' : ''}`} onClick={() => setPage(i)} aria-current={i === page ? 'page' : undefined}>{i + 1}</button>
                ))}
                <button className="pg-btn arw" disabled={page >= pages - 1} onClick={() => setPage((p) => Math.min(pages - 1, p + 1))} aria-label="Next page">→</button>
              </div>
            )
          })()}
        </>
      )}

      {/* 3) everything else, LAST, behind a collapsible "More" */}
      {cfg.filters && (
        <details className="more">
          <summary className="more-sum">✨ More<span className="more-hint">explore the ontology graph · upload a menu</span></summary>
          <div className="more-body">
            <div className="tools">
              <button className={`tool-btn ${showGraph ? 'on' : ''}`} onClick={() => setShowGraph((v) => !v)}>
                🕸 {showGraph ? 'Hide' : 'Explore'} the ontology graph{health?.graph ? ` (${health.graph.ingredient} ingredients)` : ''}
              </button>
            </div>
            {showGraph && schema === 'dish' && <GraphExplorer />}
            {schema === 'dish' && <EntityResolution onDone={refreshHealth} />}
            <UploadMenu onDone={refreshHealth} />
          </div>
        </details>
      )}

      <footer className="ftr">One Vespa engine · three indexes · multi-source ingestion (sample catalog · Food.com recipes · menu PDFs) → one schema → hybrid BM25 ⊕ e5 vectors (RRF). Allergens/diet are index-time enriched; excluded as hard filters.</footer>
    </div>
  )
}
