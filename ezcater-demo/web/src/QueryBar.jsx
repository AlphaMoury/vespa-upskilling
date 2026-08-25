import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import './kg.css'

// ─────────────────────────────────────────────────────────────────────────────
// QueryBar — the slot-filling bar over the MATERIALIZED knowledge graph.
//
// Everything here talks to the Python sidecar's /api/kg/* family (contract §D).
// Vespa is not involved in any code path: a query is a set intersection over
// adjacency lists, which is why this surface still answers with :8080 down.
//
// The one invariant worth stating up front: the DSL string and the chip row are
// the SAME object in two skins (contract §E). Typing `diet:vegan -allergen:nuts`
// and clicking two chips must produce byte-identical requests, and every server
// response hands back `parse.slots` so the chips can re-sync to the canonical
// form. Both directions are implemented — parseDSL() text→chips, renderDSL()
// chips→text — because a bar that can only be filled by clicking is a filter,
// not a query language.
//
// This file imports NOTHING from KnowledgeGraph.jsx (contract §G.1): the API
// base and the colour table arrive as props with local defaults, so the import
// direction stays App → KnowledgeGraph → QueryBar with no cycle.
// ─────────────────────────────────────────────────────────────────────────────

const API = 'http://localhost:8009'

// Local default of the type palette (§G.5). A prop overrides it; the model's own
// entity_types[].color wins over both, so a user-invented type keeps its colour.
const KG_TYPE_COLOR = {
  dish: '#e35205', cuisine: '#00695c', dish_type: '#d81b60', dish_type_group: '#ad1457',
  ingredient: '#7c8698', allergen: '#c62828', diet: '#2e7d32', meal_type: '#1140d6',
  occasion: '#5b4b8a', course: '#a15c00', flavor: '#0277bd', price_band: '#00838f',
  serving_size: '#6a1b9a', caterer: '#455a64',
}

// §E.3 — typed queries are forgiving. Mirrored client-side so a locally parsed
// chip lands on the same tag the server would have chosen (no round-trip flicker).
const ALIASES = {
  dietary: 'diet', dietary_capabilities: 'diet', dietary_capability: 'diet', diets: 'diet',
  allergens: 'allergen', allergy: 'allergen', ingredients: 'ingredient',
  type: 'dish_type', dish: 'dish_type', family: 'dish_type_group', group: 'dish_type_group',
  meal: 'meal_type', price: 'price_pp', budget: 'price_pp', pp: 'price_pp',
  spice_level: 'spice', heat: 'spice', headcount: 'serves', people: 'serves', guests: 'serves',
  caterer_name: 'caterer', vendor: 'caterer',
}

// Measures are predicates on the dish node, never nodes. The slot descriptors from
// /api/kg/slots are authoritative; this is the fallback so the parser still works
// before the first slots fetch lands.
const FALLBACK_MEASURES = new Set(['price_pp', 'spice', 'serves', 'popularity'])

const OP_SYM = { gte: '>=', lte: '<=', gt: '>', lt: '<', eq: '=' }
const SYM_OP = { '>=': 'gte', '<=': 'lte', '>': 'gt', '<': 'lt', '=': 'eq' }

// §E.2 — one compiled token regex, scanned sticky with lastIndex. Precedence is
// encoded by the alternation order: a ( … ) group beats a bare run, a quoted run
// beats an unquoted run, `tag op value` beats `bare`, longest cmp_op first.
const TOKEN_RE = /\s*(?:(?<neg>[-!])?(?:(?<tag>[A-Za-z_][A-Za-z0-9_]*)\s*(?<op><=|>=|<|>|=|:)\s*(?<val>\([^)]*\)|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s"']+)|(?<bare>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s"']+)))/y

const collapseWs = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim()
const unquote = (s) => (/^["']/.test(s) ? s.slice(1, -1).replace(/\\(.)/g, '$1') : s)
const isNum = (s) => /^-?\d+(\.\d*)?$/.test(s)

// split on `|` but only OUTSIDE quotes, so ingredient:("pine nut"|peanut) works
function splitBar(raw) {
  const out = []
  let cur = '', q = null
  for (const ch of raw) {
    if (q) { cur += ch; if (ch === q) q = null; continue }
    if (ch === '"' || ch === "'") { q = ch; cur += ch; continue }
    if (ch === '|') { out.push(cur); cur = ''; continue }
    cur += ch
  }
  out.push(cur)
  return out
}

// A value needs quoting when it carries a character the tokenizer would treat as
// structure. The server pre-quotes its `insert` strings for the same reason; this
// is the client half so a locally built chip serialises to a parseable string.
const quoteVal = (v) => (/[\s|)("']/.test(String(v)) ? JSON.stringify(String(v)) : String(v))

let _uid = 0
const nextId = () => `c${++_uid}`

/**
 * text → slots (contract §E.1/§E.2). Returns { slots, text, exclude, warnings }.
 * `measures` is the set of tags that are numeric predicates; an unknown tag is
 * demoted to free text rather than dropped, so nothing the user typed is lost.
 */
function parseDSL(src, { known = null, measures = FALLBACK_MEASURES } = {}) {
  const slots = [], text = [], exclude = [], warnings = []
  const s = src || ''
  TOKEN_RE.lastIndex = 0
  let pos = 0
  while (pos < s.length) {
    TOKEN_RE.lastIndex = pos
    const m = TOKEN_RE.exec(s)
    if (!m || TOKEN_RE.lastIndex === pos) break     // zero-length match aborts the scan
    pos = TOKEN_RE.lastIndex
    const g = m.groups
    let neg = !!g.neg
    if (g.bare != null) {
      const w = collapseWs(unquote(g.bare))
      if (w) (neg ? exclude : text).push(w)
      continue
    }
    const rawTag = (g.tag || '').toLowerCase()
    const tag = ALIASES[rawTag] || rawTag
    const op = g.op
    const raw = g.val || ''
    const isMeasureOp = op !== ':' && op !== '='
    const knownTag = !known || known.has(tag)
    if (!knownTag) {                                  // §E.2 — demote, warn, keep going
      warnings.push({ code: 'unknown_tag', tag })
      text.push(collapseWs(m[0]))
      continue
    }
    if (isMeasureOp || (op === '=' && measures.has(tag))) {
      const num = unquote(raw).trim()
      if (!isNum(num)) { text.push(collapseWs(m[0])); continue }  // non-numeric operand → free text
      slots.push({
        id: nextId(), tag, kind: 'measure', op: SYM_OP[op] || 'eq',
        values: [{ value: parseFloat(num) }],
      })
      continue
    }
    let vals
    if (raw.startsWith('(')) vals = splitBar(raw.slice(1, -1))
    else if (raw[0] === '"' || raw[0] === "'") vals = [unquote(raw)]   // quoted values NEVER split on |
    else vals = splitBar(raw)
    vals = vals.map((v) => unquote(v.trim()))
    if (vals.length && /^[-!]/.test(vals[0]) && !neg) { neg = true; vals[0] = vals[0].slice(1) }
    vals = vals.map(collapseWs).filter(Boolean)
    if (!vals.length) continue
    slots.push({
      id: nextId(), tag, kind: 'entity', op: neg ? 'not' : 'is', match: 'any',
      values: vals.map((v) => ({ value: v })),
    })
  }
  return { slots, text: text.join(' '), exclude, warnings }
}

/**
 * slots → text. Canonical order (§E.4): includes → measures → excludes → free text.
 * This is what gets sent to the server, so round-tripping through it must be lossless.
 */
function renderDSL(slots, free = '', exclude = []) {
  const inc = [], mea = [], exc = []
  for (const sl of slots || []) {
    if (sl.kind === 'measure') {
      const v = sl.values?.[0]?.value
      if (v == null) continue
      mea.push(`${sl.tag}${OP_SYM[sl.op] || '='}${v}`)
      continue
    }
    const vals = (sl.values || []).map((v) => quoteVal(v.value)).filter(Boolean)
    if (!vals.length) continue
    const body = vals.length > 1 ? `(${vals.join('|')})` : vals[0]
    ;(sl.op === 'not' ? exc : inc).push(`${sl.op === 'not' ? '-' : ''}${sl.tag}:${body}`)
  }
  const negText = (exclude || []).map((t) => `-${quoteVal(t)}`)
  return [...inc, ...mea, ...exc, ...negText, (free || '').trim()].filter(Boolean).join(' ')
}

// The token the caret sits inside — used when a typeahead value is accepted, so the
// whole `cuisine:Ital` fragment is removed rather than leaving a dangling `cuisine:`.
function tokenAt(src, caret) {
  let a = caret, b = caret
  while (a > 0 && !/\s/.test(src[a - 1])) a--
  while (b < src.length && !/\s/.test(src[b])) b++
  return [a, b]
}

// ── vis-network options for the result subgraph (§G.4) ───────────────────────
// Small graph, one physics burst, then frozen. `smooth.type:'dynamic'` is banned
// everywhere in this feature — it allocates a physics body per edge, which is the
// reason the original explorer chewed a core for as long as the tab was open.
const RESULT_OPTIONS = {
  autoResize: true,
  layout: { improvedLayout: false, randomSeed: 11 },
  physics: {
    enabled: true, solver: 'forceAtlas2Based',
    forceAtlas2Based: {
      gravitationalConstant: -55, centralGravity: 0.014, springLength: 120,
      springConstant: 0.085, damping: 0.6, avoidOverlap: 0.6,
    },
    stabilization: { enabled: true, iterations: 140, updateInterval: 40, fit: true },
    adaptiveTimestep: true, maxVelocity: 30, timestep: 0.4,
  },
  interaction: {
    hover: true, tooltipDelay: 140, dragNodes: true, dragView: true, zoomView: true,
    hideEdgesOnDrag: true, navigationButtons: false, keyboard: false,
  },
  nodes: {
    shape: 'box', shapeProperties: { borderRadius: 10 }, borderWidth: 1.4,
    margin: { top: 7, right: 11, bottom: 7, left: 11 }, widthConstraint: { maximum: 150 },
    font: { size: 12.5, face: 'Inter, system-ui, sans-serif' },
    shadow: { enabled: true, size: 5, x: 0, y: 2, color: 'rgba(15,23,41,0.13)' },
  },
  edges: {
    smooth: { enabled: true, type: 'continuous', roundness: 0.18 },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } }, width: 1.4, hoverWidth: 0,
    font: { size: 10, color: '#6b7280', strokeWidth: 4, strokeColor: '#fff', align: 'middle' },
    color: { color: '#b6bcc6', highlight: '#e35205', hover: '#e35205', inherit: false },
  },
}

// ─────────────────────────────────────────────────────────────────────────────

export default function QueryBar({
  model,
  api = API,
  typeColor = KG_TYPE_COLOR,
  compact = false,
  initialSlots = [],
  autoRun = false,
  onNeedMaterialize = () => {},
  onResult = () => {},
  onSubgraph = null,
  onSlotsChange = () => {},
  onFocusNodes = () => {},
}) {
  const modelId = model?.id || ''
  const materialized = model?.materialized !== false

  const [slotDefs, setSlotDefs] = useState([])     // /api/kg/slots descriptors
  const [slots, setSlots] = useState(() => (initialSlots || []).map((s) => ({ id: nextId(), ...s })))
  const [text, setText] = useState('')
  const [armed, setArmed] = useState('')           // tag currently armed for value entry
  const [neg, setNeg] = useState(false)            // polarity of the NEXT commit
  const [sel, setSel] = useState('')               // chip id selected by backspace
  const [open, setOpen] = useState(false)          // typeahead panel visible
  const [sugg, setSugg] = useState([])
  const [cur, setCur] = useState(0)                // highlighted suggestion
  const [resp, setResp] = useState(null)
  const [analytics, setAnalytics] = useState(null) // analytics canned-query payload
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [canned, setCanned] = useState([])
  const [hotId, setHotId] = useState('')
  const [preview, setPreview] = useState(null)     // debounced count for the run button
  const [form, setForm] = useState(null)           // { title, err } while saving a rule
  const [focused, setFocused] = useState(false)

  const inputRef = useRef(null)
  const taSeq = useRef(0)
  const runSeq = useRef(0)
  const prevSeq = useRef(0)
  const taAbort = useRef(null)
  const prevAbort = useRef(null)
  const boxRef = useRef(null)
  const net = useRef(null)
  const nodesDS = useRef(null)
  const edgesDS = useRef(null)
  const settle = useRef(null)
  const sentDsl = useRef('')

  // ── derived -----------------------------------------------------------------
  const defByTag = useMemo(() => {
    const m = {}
    for (const d of slotDefs) m[d.tag] = d
    return m
  }, [slotDefs])

  const measureTags = useMemo(() => {
    const s = new Set(slotDefs.filter((d) => d.kind === 'measure').map((d) => d.tag))
    return s.size ? s : FALLBACK_MEASURES
  }, [slotDefs])

  const knownTags = useMemo(() => {
    if (!slotDefs.length) return null                 // null = accept everything until slots load
    const s = new Set(slotDefs.map((d) => d.tag))
    for (const [a, t] of Object.entries(ALIASES)) if (s.has(t)) s.add(a)
    return s
  }, [slotDefs])

  const colorFor = useCallback((tag) => {
    const et = (model?.entity_types || []).find((e) => e.tag === tag)
    return et?.color || defByTag[tag]?.color || typeColor[tag] || '#7c8698'
  }, [model, defByTag, typeColor])

  const labelFor = useCallback((tag) => defByTag[tag]?.label || tag.replace(/_/g, ' '), [defByTag])

  const dsl = useMemo(() => renderDSL(slots, text), [slots, text])
  const hasQuery = slots.length > 0 || !!text.trim()

  // ── data loads ---------------------------------------------------------------
  useEffect(() => {
    if (!modelId || !materialized) { setSlotDefs([]); return }
    let dead = false
    fetch(`${api}/api/kg/slots?model=${encodeURIComponent(modelId)}`)
      .then((r) => r.json())
      .then((d) => { if (!dead && d?.ok !== false) setSlotDefs(d.slots || []) })
      .catch(() => { if (!dead) setSlotDefs([]) })
    return () => { dead = true }
  }, [api, modelId, materialized])

  const loadCanned = useCallback(() => {
    if (!modelId) return
    fetch(`${api}/api/kg/queries?model=${encodeURIComponent(modelId)}&include_builtin=true`)
      .then((r) => r.json())
      .then((d) => setCanned(d?.queries || []))
      .catch(() => setCanned([]))
  }, [api, modelId])
  useEffect(() => { loadCanned() }, [loadCanned])

  // The Builder's ⌘⏎ hand-off arms the bar with a fresh slot set while this
  // component is already mounted, so initialSlots is adopted on change (not only
  // at mount) whenever the user has not started composing their own query.
  const initKey = JSON.stringify(initialSlots || [])
  useEffect(() => {
    if (!initialSlots?.length) return
    setSlots(initialSlots.map((s) => ({ id: nextId(), ...s })))
    setText(''); setArmed(''); setNeg(false)
  }, [initKey])   // eslint-disable-line react-hooks/exhaustive-deps

  // slot mutations are reported upward debounced, so a parent that mirrors them
  // into the canvas is not re-rendered on every keystroke
  useEffect(() => {
    const t = setTimeout(() => onSlotsChange(slots), 200)
    return () => clearTimeout(t)
  }, [slots])   // eslint-disable-line react-hooks/exhaustive-deps

  // remember the last query per browser so a reload does not lose the demo state
  useEffect(() => {
    if (!modelId) return
    try { localStorage.setItem('ezc_kg_slots', JSON.stringify({ model: modelId, dsl })) } catch { /* private mode */ }
  }, [modelId, dsl])

  // ── running ------------------------------------------------------------------
  const run = useCallback((q, { adopt = true } = {}) => {
    const expr = (q == null ? dsl : q).trim()
    if (!modelId) return
    if (!materialized) { setError('not-materialized'); return }
    const seq = ++runSeq.current
    sentDsl.current = expr
    setLoading(true); setError(''); setAnalytics(null); setOpen(false)
    fetch(`${api}/api/kg/query`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId, q: expr, limit: 24, rollup: true }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (seq !== runSeq.current) return                 // a newer run already won
        if (d?.ok === false) { setError(d?.error?.message || 'query failed'); setResp(null); onResult(null); return }
        setResp(d)
        onResult(d)
        if (onSubgraph) onSubgraph(d.subgraph || null)
        // Re-sync the chips to the server's canonical parse: values gain node ids,
        // counts and resolved/unresolved status, and the order becomes canonical.
        // Guarded on sentDsl so a user who kept typing is never yanked backwards.
        if (adopt && d.parse && sentDsl.current === expr) {
          const ps = (d.parse.slots || []).filter((s) => s.kind !== 'text')
          setSlots(ps.map((s) => ({ ...s, id: s.id || nextId() })))
          const ft = [...(d.parse.free_text || []), ...(d.parse.exclude_text || []).map((t) => `-${t}`)]
          setText(ft.join(' '))
        }
      })
      .catch(() => { if (seq === runSeq.current) { setError('unreachable'); setResp(null); onResult(null) } })
      .finally(() => { if (seq === runSeq.current) setLoading(false) })
  }, [api, modelId, materialized, dsl])   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (autoRun && modelId && materialized && hasQuery && !resp) run() }, [autoRun, modelId, materialized])   // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced preview count (§G.6, 200 ms). facets+subgraph off — this is the cheap
  // "how many would that be" readout on the run button, not a real execution.
  useEffect(() => {
    if (!modelId || !materialized || !hasQuery) { setPreview(null); return }
    const t = setTimeout(() => {
      const seq = ++prevSeq.current
      prevAbort.current?.abort?.()
      const ac = new AbortController(); prevAbort.current = ac
      const u = `${api}/api/kg/query?model=${encodeURIComponent(modelId)}&q=${encodeURIComponent(dsl)}`
        + '&limit=1&facets=false&subgraph=false&rollup=true'
      fetch(u, { signal: ac.signal }).then((r) => r.json()).then((d) => {
        if (seq !== prevSeq.current || d?.ok === false) return
        setPreview(d?.blocked ? null : (d?.counts?.distinct ?? d?.counts?.after_exclude ?? null))
      }).catch(() => { /* aborted or offline — the run button just loses its hint */ })
    }, 200)
    return () => clearTimeout(t)
  }, [api, modelId, materialized, dsl, hasQuery])

  // ── typeahead (§D.3) ----------------------------------------------------------
  // 130 ms to match the existing GraphExplorer; every request is both abortable and
  // sequence-guarded so a slow response can never clobber a newer one.
  useEffect(() => {
    if (!open || !modelId || !materialized) return
    const q = text
    const t = setTimeout(() => {
      const seq = ++taSeq.current
      taAbort.current?.abort?.()
      const ac = new AbortController(); taAbort.current = ac
      const m = encodeURIComponent(modelId)
      // An armed slot with nothing typed wants the vocabulary itself (top values by
      // document frequency), which is /values; anything typed goes through the
      // caret-aware /typeahead so tag-, value- and free-word modes all work.
      const url = armed && !q.trim()
        ? `${api}/api/kg/values?model=${m}&type=${encodeURIComponent(armed)}&q=&limit=10`
        : `${api}/api/kg/typeahead?model=${m}&limit=8&q=${encodeURIComponent(q)}&caret=${q.length}`
          + (armed ? `&tag=${encodeURIComponent(armed)}&mode=value` : '&mode=auto')
      fetch(url, { signal: ac.signal }).then((r) => r.json()).then((d) => {
        if (seq !== taSeq.current) return
        const rows = d?.suggestions
          || (d?.values || []).map((v) => ({ kind: 'value', tag: d.tag || armed, ...v }))
          || []
        setSugg(rows); setCur(0)
      }).catch(() => { if (seq === taSeq.current) setSugg([]) })
    }, 130)
    return () => clearTimeout(t)
  }, [api, modelId, materialized, text, armed, open])

  // With nothing typed and nothing armed the panel is the slot legend itself: the
  // available entity types, which is the honest answer to "what can I ask?".
  const rows = useMemo(() => {
    if (armed || text.trim()) return sugg
    return slotDefs.filter((d) => d.kind !== 'text').map((d) => ({
      kind: 'tag', tag: d.tag, label: d.label, count: d.value_count,
      hint: (d.examples || []).slice(0, 3).join(' · '), color: colorFor(d.tag),
    }))
  }, [armed, text, sugg, slotDefs, colorFor])

  const usedValues = useMemo(() => {
    const s = new Set()
    for (const sl of slots) for (const v of sl.values || []) s.add(`${sl.tag} ${String(v.value).toLowerCase()}`)
    return s
  }, [slots])

  // ── chip mutation -------------------------------------------------------------
  const commitValue = useCallback((tag, value, { negative = neg, extend = false, label = '' } = {}) => {
    setSlots((prev) => {
      const op = negative ? 'not' : 'is'
      if (extend) {                                    // `,` continuation ORs into the same chip
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].tag === tag && prev[i].op === op && prev[i].kind === 'entity') {
            if ((prev[i].values || []).some((v) => String(v.value).toLowerCase() === String(value).toLowerCase())) return prev
            const cp = prev.slice()
            cp[i] = { ...cp[i], values: [...cp[i].values, { value, label: label || value }] }
            return cp
          }
        }
      }
      return [...prev, {
        id: nextId(), tag, kind: 'entity', op, match: 'any',
        values: [{ value, label: label || value }],
        safety_critical: !!defByTag[tag]?.safety_critical,
      }]
    })
    setSel('')
  }, [neg, defByTag])

  const removeSlot = (id) => { setSlots((p) => p.filter((s) => s.id !== id)); setSel('') }
  const togglePolarity = (id) => setSlots((p) => p.map((s) => (
    s.id === id && s.kind === 'entity' ? { ...s, op: s.op === 'not' ? 'is' : 'not' } : s
  )))
  const clearAll = () => {
    setSlots([]); setText(''); setArmed(''); setNeg(false); setSel('')
    setResp(null); setAnalytics(null); setError(''); setPreview(null)
    onResult(null); if (onSubgraph) onSubgraph(null); onFocusNodes([])
  }

  const armTag = (tag) => { setArmed(tag); setText(''); setOpen(true); setSel(''); inputRef.current?.focus() }

  const accept = (row, { negative = neg, extend = false } = {}) => {
    if (!row) return
    if (row.kind === 'tag') { armTag(row.tag); return }
    if (row.kind === 'free') {                          // keep the word as free text
      const [a, b] = tokenAt(text, text.length)
      setText(`${text.slice(0, a)}${row.insert || row.value} ${text.slice(b)}`.replace(/\s+/g, ' ').trimStart())
      setSugg([]); return
    }
    const tag = row.tag || armed
    if (!tag) return
    commitValue(tag, row.value ?? row.label, { negative, extend, label: row.label })
    // Remove the fragment the caret sits in — the term has become a chip, so leaving
    // `cuisine:Ital` behind in the input would double-apply it on the next run.
    const caret = inputRef.current?.selectionStart ?? text.length
    const [a, b] = tokenAt(text, caret)
    setText(`${text.slice(0, a)}${text.slice(b)}`.replace(/\s+/g, ' ').trim())
    if (extend) { setArmed(tag); setOpen(true) } else { setArmed(''); setNeg(false); setOpen(true) }
  }

  // Enter with no highlighted suggestion means "take what I typed literally":
  // parse the raw text into chips and run. This is the text→chips direction.
  const commitTextAndRun = () => {
    const raw = text.trim()
    if (raw) {
      const p = parseDSL(raw, { known: knownTags, measures: measureTags })
      const nextSlots = [...slots, ...p.slots]
      setSlots(nextSlots)
      setText(p.text)
      setArmed(''); setOpen(false)
      run(renderDSL(nextSlots, p.text, p.exclude))
      return
    }
    setOpen(false)
    run()
  }

  // Push the whole canonical expression back into the input for hand-editing —
  // the chips→text direction, and the reason the bar is a language and not a form.
  const editAsText = () => {
    const expr = resp?.normalized_dsl || dsl
    setSlots([]); setText(expr); setArmed(''); setOpen(true)
    setTimeout(() => { inputRef.current?.focus(); inputRef.current?.setSelectionRange(expr.length, expr.length) }, 0)
  }

  const onKey = (e) => {
    const list = rows
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setCur((c) => Math.min(list.length - 1, c + 1)); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); setCur((c) => Math.max(0, c - 1)); return }
    if (e.key === 'Escape') { e.preventDefault(); if (open) setOpen(false); else if (armed) { setArmed(''); setNeg(false) } else setSel(''); return }
    if (e.key === 'Enter') {
      if ((e.metaKey || e.ctrlKey)) { e.preventDefault(); commitTextAndRun(); return }
      if (open && list.length && (armed || text.trim())) {
        e.preventDefault()
        accept(list[cur], { negative: e.altKey ? true : neg })    // Alt+Enter commits NEGATIVE
        return
      }
      e.preventDefault(); commitTextAndRun(); return
    }
    if (e.key === 'Tab' && open && list.length && (armed || text.trim())) {
      const r = list[cur]
      if (r?.kind === 'value' && r.tag) { e.preventDefault(); armTag(r.tag) }     // arm without committing
      return
    }
    if (e.key === ',' && open && list.length && (armed || text.trim())) {
      const r = list[cur]
      if (r?.kind === 'value') { e.preventDefault(); accept(r, { extend: true }) } // multi-value fast path
      return
    }
    if (e.key === 'Backspace' && !text) {
      if (armed) { e.preventDefault(); setArmed(''); setNeg(false); return }
      if (!slots.length) return
      e.preventDefault()
      const last = slots[slots.length - 1]
      if (sel === last.id) removeSlot(last.id)                   // second ⌫ deletes
      else setSel(last.id)                                       // first ⌫ selects
    }
  }

  // ── canned queries -------------------------------------------------------------
  const loadCannedQ = (q, { andRun = true } = {}) => {
    if (q.kind === 'analytics' || (!q.dsl && q.endpoint)) { runAnalytics(q); return }
    const p = parseDSL(q.dsl || '', { known: knownTags, measures: measureTags })
    setSlots(p.slots); setText(p.text); setArmed(''); setNeg(false); setOpen(false); setAnalytics(null)
    if (andRun) run(renderDSL(p.slots, p.text, p.exclude), { adopt: true })
    else { setResp(null); setError(''); inputRef.current?.focus() }
  }

  // Analytics rules are not slot queries — they answer questions a filter cannot
  // (degree, set cover, cross-tab). They run server-side and render as a flat list.
  const runAnalytics = (q) => {
    const seq = ++runSeq.current
    setLoading(true); setError(''); setResp(null); onResult(null)
    if (onSubgraph) onSubgraph(null)
    // Fall back to the endpoint the rule names when /run is unavailable — a saved
    // analytics rule carries everything needed to call its endpoint directly.
    const direct = () => {
      const params = new URLSearchParams(); params.set('model', modelId)
      for (const [k, v] of Object.entries(q.params || {})) {
        if (Array.isArray(v)) v.forEach((x) => params.append(k, String(x)))
        else params.set(k, String(v))
      }
      return fetch(`${api}${q.endpoint}?${params}`).then((r) => r.json())
    }
    fetch(`${api}/api/kg/queries/${encodeURIComponent(q.id)}/run?limit=24`, { method: 'POST' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('run failed'))))
      .catch(() => (q.endpoint ? direct() : Promise.reject(new Error('no endpoint'))))
      .then((d) => {
        if (seq !== runSeq.current) return
        const body = d?.result || d?.response || d
        if (body?.ok === false) { setError(body?.error?.message || 'analytics failed'); return }
        // /run on a slot rule hands back a normal query response — render it as one
        if (body?.parse && Array.isArray(body.rows)) {
          setResp(body); onResult(body); if (onSubgraph) onSubgraph(body.subgraph || null)
        } else setAnalytics({ q, payload: body })
      })
      .catch(() => { if (seq === runSeq.current) setError('unreachable') })
      .finally(() => { if (seq === runSeq.current) setLoading(false) })
  }

  const saveRule = () => {
    const title = (form?.title || '').trim()
    if (!title) { setForm({ ...form, err: 'name it first' }); return }
    fetch(`${api}/api/kg/queries`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId, title, dsl }),
    }).then((r) => r.json()).then((d) => {
      if (d?.ok === false) { setForm({ ...form, err: d?.error?.message || 'rejected' }); return }
      setForm(null); loadCanned()
    }).catch(() => setForm({ ...form, err: 'unreachable' }))
  }

  const deleteRule = (id) => {
    fetch(`${api}/api/kg/queries/${encodeURIComponent(id)}`, { method: 'DELETE' })
      .then(() => loadCanned()).catch(() => { /* leave the card in place */ })
  }

  // ── result subgraph ------------------------------------------------------------
  const sg = resp?.subgraph
  useEffect(() => {
    if (compact || !sg || !boxRef.current) return
    let dead = false
    const paint = (Network, DataSet) => {
      if (dead || !boxRef.current) return
      const legend = sg.legend || {}
      const vn = (n) => {
        const role = legend[n.role] || {}
        const c = role.color || colorFor(n.tag || n.kind) || '#9aa0a6'
        const faded = n.role === 'blocked'
        return {
          id: n.id, label: n.label,
          color: {
            background: faded ? '#f2f3f5' : (n.role === 'result' || n.role === 'include_hit' ? c : '#ffffff'),
            border: c, highlight: { background: c, border: c },
          },
          font: { color: !faded && (n.role === 'result' || n.role === 'include_hit') ? '#ffffff' : '#2b3440', size: 12.5 },
          borderWidth: n.role === 'exclude' ? 2 : 1.4,
          shapeProperties: { borderRadius: 10, borderDashes: !!role.dashes },
          opacity: faded ? 0.4 : 1,
        }
      }
      const ve = (e) => ({
        id: `${e.from}->${e.to}`, from: e.from, to: e.to, label: e.rel ? e.rel.toLowerCase().replace(/_/g, ' ') : '',
        dashes: e.role === 'blocked', color: {
          color: e.role === 'blocked' ? '#c62828' : e.role === 'matched' ? '#2e7d32' : '#b6bcc6',
          opacity: e.role === 'context' ? 0.4 : 0.85, highlight: '#e35205', inherit: false,
        },
      })
      if (!net.current) {
        nodesDS.current = new DataSet([]); edgesDS.current = new DataSet([])
        net.current = new Network(boxRef.current, { nodes: nodesDS.current, edges: edgesDS.current }, RESULT_OPTIONS)
        net.current.on('stabilizationIterationsDone', () => {
          net.current?.setOptions({ physics: { enabled: false } })
          net.current?.fit({ animation: { duration: 400 } })
        })
      }
      // Replace the DataSets IN PLACE and give physics one burst. Destroying and
      // recreating the Network on every query is the main cause of canvas flicker.
      nodesDS.current.clear(); edgesDS.current.clear()
      nodesDS.current.add((sg.nodes || []).map(vn))
      edgesDS.current.add((sg.edges || []).map(ve))
      net.current.setOptions({ physics: { enabled: true } })
      clearTimeout(settle.current)
      settle.current = setTimeout(() => {
        net.current?.setOptions({ physics: { enabled: false } })
        if (sg.focus?.length) { try { net.current?.fit({ nodes: sg.focus, animation: { duration: 450 } }) } catch { net.current?.fit() } }
      }, 1100)
    }
    import('vis-network/standalone').then(({ Network, DataSet }) => paint(Network, DataSet)).catch(() => { /* no canvas, list still works */ })
    return () => { dead = true; clearTimeout(settle.current) }
  }, [sg, compact, colorFor])

  useEffect(() => () => { net.current?.destroy?.(); net.current = null }, [])

  // ── render helpers ───────────────────────────────────────────────────────────
  // kg.css drives every colour off three custom properties, so JS passes the hue
  // and the stylesheet owns the treatment — an inline `color` here would beat the
  // `.neg` / `.filled` rules and quietly break the exclusion styling.
  const hue = (c) => ({ '--c': c, '--c-bg': `${c}14`, '--c-br': `${c}55` })

  const chipFor = (sl) => {
    const isNeg = sl.op === 'not'
    const isMeasure = sl.kind === 'measure'
    const vtext = isMeasure
      ? `${OP_SYM[sl.op] || '='} ${sl.values?.[0]?.value}`
      : (sl.values || []).map((v) => v.label || v.value).join(' | ')
    const unresolved = (sl.values || []).some((v) => v.status === 'unresolved')
    const crit = sl.safety_critical || defByTag[sl.tag]?.safety_critical
    return (
      <span
        key={sl.id}
        className={`kgq-chip${isNeg ? ' neg' : ''}${isMeasure ? ' num' : ''}${sel === sl.id ? ' sel' : ''}`}
        style={hue(colorFor(sl.tag))}
        onClick={() => setSel(sl.id === sel ? '' : sl.id)}
        title={unresolved
          ? `“${(sl.values || []).map((v) => v.value).join(', ')}” is not in the ontology`
          : `${labelFor(sl.tag)}${crit ? ' — safety-critical' : ''} · ¬ flips require/exclude, × removes`}
      >
        {!isMeasure && (
          <button className="kgq-chip-neg" title="require / exclude"
            onClick={(e) => { e.stopPropagation(); togglePolarity(sl.id) }}>
            {isNeg ? '¬' : '+'}
          </button>
        )}
        <span className="kgq-chip-t">{labelFor(sl.tag)}</span>
        <span className="kgq-chip-v">{vtext}</span>
        <button className="kgq-chip-x" title="remove"
          onClick={(e) => { e.stopPropagation(); removeSlot(sl.id) }}>×</button>
      </span>
    )
  }

  const filledTags = useMemo(() => new Set(slots.map((s) => s.tag)), [slots])

  // The legend is the answer to "what can I ask?" — every declared slot, with its
  // value count, so a 540-of-600 vocabulary reads as visibly weak rather than useful.
  const legend = (
    <div className="kgq-slots">
      <span className="kgq-slots-l">Slots</span>
      {slotDefs.filter((d) => d.kind !== 'text').map((d) => (
        <button
          key={d.tag}
          className={`kgq-slot${filledTags.has(d.tag) ? ' filled' : ''}`}
          style={hue(colorFor(d.tag))}
          onClick={() => armTag(d.tag)}
          title={`${d.value_count ?? '?'} values · ${Math.round((d.coverage || 0) * 100)}% of dishes carry it`
            + (d.safety_critical ? ' · safety-critical: unresolved exclusions refuse to answer' : '')}
        >
          <i />
          {d.icon ? `${d.icon} ` : ''}{d.label}
          {typeof d.value_count === 'number' ? ` ${d.value_count}` : ''}
        </button>
      ))}
      {hasQuery && <button className="kgq-clear-all" onClick={clearAll}>clear</button>}
    </div>
  )

  const panel = open && (rows.length > 0 || armed || text.trim()) && (
    <div className="kgq-panel">
      {armed ? (
        <div className="kgq-sec">
          <button className="kgq-back" onClick={() => { setArmed(''); setText(''); inputRef.current?.focus() }}>
            ← all slots
          </button>
          <span className="kgq-grp" style={hue(colorFor(armed))}>{labelFor(armed)}</span>
          <button className={`kgq-neg-toggle${neg ? ' on' : ''}`} onClick={() => setNeg((v) => !v)}
            title="alt+⏎ also commits the highlighted value as an exclusion">
            {neg ? '¬ exclude' : '+ require'}
          </button>
        </div>
      ) : (
        <div className="kgq-sec"><span>{text.trim() ? 'matches' : 'slots you can fill'}</span></div>
      )}
      {rows.length === 0 && (
        <div className="kgq-row none">
          nothing in the ontology matches “{text.trim()}”
          <span className="kgq-row-hint">⏎ searches it as free text</span>
        </div>
      )}
      {rows.map((r, i) => {
        const used = r.kind === 'value' && usedValues.has(`${r.tag} ${String(r.value).toLowerCase()}`)
        const c = r.color || colorFor(r.tag)
        return (
          <div
            key={`${r.kind}:${r.tag}:${r.value ?? r.label}:${i}`}
            className={`kgq-row${i === cur ? ' on' : ''}${r.kind === 'free' ? ' free' : ''}${used ? ' kgq-row-used' : ''}`}
            onMouseEnter={() => setCur(i)}
            onMouseDown={(e) => { e.preventDefault(); accept(r, { negative: e.altKey ? true : neg }) }}
            title={used ? 'already in the query' : ''}
          >
            <i style={{ width: 9, height: 9, borderRadius: 3, flex: 'none', background: c, display: 'inline-block' }} />
            <span>{r.label ?? r.value}</span>
            {r.kind === 'tag' && r.hint && <span className="kgq-row-hint">{r.hint}</span>}
            {r.kind === 'value' && r.tag && !armed && <span className="kgq-row-hint">{labelFor(r.tag)}</span>}
            {typeof r.count === 'number' && <span className="kgq-row-n">{r.count}</span>}
          </div>
        )
      })}
      {!!text.trim() && !armed && (
        <div className="kgq-row free" onMouseDown={(e) => { e.preventDefault(); commitTextAndRun() }}>
          <span>search “{text.trim()}” as free text</span>
          <span className="kgq-row-hint">⏎</span>
        </div>
      )}
    </div>
  )

  // A canned query is one click to run (the card) and one click to load-without-running
  // (the mini chip strip), because half of them are worth editing before they are asked.
  const cannedRow = (hero) => (
    <div className={`kgq-canned${hero ? ' hero' : ''}`}>
      <div className="kgq-canned-hd">
        <span>Common questions</span>
        {hasQuery && !form && <button className="kgc-save" onClick={() => setForm({ title: '', err: '' })}>+ save as a rule</button>}
      </div>
      {form && (
        <form className="kgc-form" onSubmit={(e) => { e.preventDefault(); saveRule() }}>
          <input autoFocus value={form.title} placeholder="name this rule — e.g. nut-free client dinner"
            onChange={(e) => setForm({ ...form, title: e.target.value, err: '' })}
            onKeyDown={(e) => { if (e.key === 'Escape') setForm(null) }} />
          <button className="kgb-btn primary" type="submit">save</button>
          <button className="kgb-btn ghost" type="button" onClick={() => setForm(null)}>cancel</button>
          {form.err && <em>{form.err}</em>}
        </form>
      )}
      <div>
        {canned.filter((q) => !q.hidden).map((q) => {
          const mini = q.dsl ? parseDSL(q.dsl, { known: null, measures: measureTags }).slots : []
          return (
            <div
              key={q.id}
              className={`kgc${q.builtin ? '' : ' mine'}${q.stale ? ' stale' : ''}`}
              onClick={() => loadCannedQ(q, { andRun: true })}
              title={q.stale ? (q.stale_reason || 'the ontology changed under this rule') : (q.why || q.expect || '')}
            >
              <span className="kgc-name">{q.icon ? `${q.icon} ` : ''}{q.title}</span>
              <span className="kgc-slots" title="load into the bar without running"
                onClick={(e) => { e.stopPropagation(); loadCannedQ(q, { andRun: false }) }}>
                {mini.slice(0, 5).map((s, i) => (
                  <span key={i} className={`kgc-mini${s.op === 'not' ? ' neg' : ''}`}>
                    {s.kind === 'measure'
                      ? `${s.tag}${OP_SYM[s.op] || '='}${s.values?.[0]?.value}`
                      : `${s.op === 'not' ? '¬' : ''}${(s.values || []).map((v) => v.value).join('|')}`}
                  </span>
                ))}
                {q.kind === 'analytics' && <span className="kgc-mini">graph analytics</span>}
                {q.stale && <span className="kgc-mini neg">stale</span>}
              </span>
              {!q.builtin && (
                <button className="kgc-x" title="delete this rule"
                  onClick={(e) => { e.stopPropagation(); deleteRule(q.id) }}>×</button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )

  // ── result rendering ─────────────────────────────────────────────────────────
  const PILL_ORDER = ['cuisine', 'dish_type', 'meal_type', 'course', 'occasion', 'diet', 'price_band', 'serving_size']

  const pillsFor = (row) => {
    const out = []
    for (const tag of PILL_ORDER) {
      for (const v of (row.slots?.[tag] || []).slice(0, tag === 'diet' ? 3 : 2)) {
        out.push(<span key={`${tag}:${v}`} className="kgr-pill" style={hue(colorFor(tag))}>{v}</span>)
      }
    }
    // a cleared allergen is a positive claim — "checked against nuts", not "nuts absent"
    for (const c of row.cleared || []) {
      out.push(<span key={`cl:${c.value}`} className="kgr-pill alg"
        title={`checked against ${c.node || c.value}`}>{c.value}-free</span>)
    }
    for (const a of (row.slots?.allergen || [])) {
      out.push(<span key={`al:${a}`} className="kgr-pill" style={hue('#c62828')}>⚠ {a}</span>)
    }
    return out
  }

  const resultRow = (row, i) => {
    const id = row.dish_id || row.node || String(i)
    return (
      <div
        key={id}
        className={`kgr-row${hotId === id ? ' hot' : ''}`}
        onMouseEnter={() => { setHotId(id); if (compact) onFocusNodes([row.node || `dish:${row.dish_id}`]) }}
        onMouseLeave={() => { setHotId(''); if (compact) onFocusNodes([]) }}
      >
        <div className="kgr-row-top">
          <span className="kgr-name">{row.name}</span>
          {typeof row.price_pp === 'number' && (
            <span className="kgr-price">
              ${row.price_pp.toFixed(2)}
              {row.price_pp_range && row.price_pp_range[1] > row.price_pp_range[0]
                ? `–${Number(row.price_pp_range[1]).toFixed(2)}` : ''}
              /head
            </span>
          )}
        </div>
        <div className="kgr-pills">{pillsFor(row)}</div>
        <div className="kgr-serves">
          {row.serves ? <>serves <b>{row.serves}{row.serves_max > row.serves ? `–${row.serves_max}` : ''}</b> · </> : null}
          {row.caterer}
          {row.caterer_count > 1 ? ` +${row.caterer_count - 1} more caterers` : ''}
          {typeof row.popularity === 'number' ? ` · popularity ${row.popularity}` : ''}
        </div>
        {(row.matched || []).length > 0 && (
          <div className="kgr-why">
            <b>why</b>{' '}
            {(row.matched || []).slice(0, 4).map((m, k) => (
              <span key={k} className="kgr-hop">
                {m.rel ? `${m.rel.toLowerCase().replace(/_/g, ' ')} → ` : ''}{m.tag}:{m.value}
              </span>
            ))}
            {typeof row.score === 'number' && <em> score {row.score.toFixed(3)}</em>}
          </div>
        )}
      </div>
    )
  }

  // The six analytics payloads are six different shapes; each is flattened into the
  // same {name, note, pills} row so one renderer serves all of them.
  const analyticsRows = (p) => {
    if (!p) return []
    if (Array.isArray(p.matrix) && p.row_values) {
      const gaps = new Set((p.gaps || []).map((g) => `${g.row} ${g.col}`))
      return p.row_values.map((rv, i) => ({
        name: rv,
        pills: (p.col_values || []).map((cv, j) => ({
          text: `${cv} ${p.matrix[i]?.[j] ?? 0}`,
          bad: gaps.has(`${rv} ${cv}`) || p.matrix[i]?.[j] === 0,
        })),
      }))
    }
    if (p.blockers) {
      return p.blockers.map((b) => ({
        name: `${b.tag}:${b.value}`,
        pills: [{ text: `${Math.round((b.pct ?? (b.blast / (p.base_count || 1))) * 100)}% of the shortlist`, bad: true }],
        note: `${b.blast} of ${p.base_count} dishes lost`
          + (b.unique_loss ? ` · ${b.unique_loss} with no replacement inside their own dish type` : ''),
      }))
    }
    if (p.levels) {                                    // substitute — a widening walk
      const out = []
      for (const lv of p.levels) {
        out.push({ name: `Level ${lv.level} — ${lv.rule}`, note: `${lv.count} candidate${lv.count === 1 ? '' : 's'}`, head: true })
        for (const r of lv.rows || []) {
          out.push({
            name: r.name,
            note: [r.cuisine, r.jaccard != null ? `similarity ${Number(r.jaccard).toFixed(2)}` : ''].filter(Boolean).join(' · '),
            hops: r.hops,
          })
        }
      }
      return out
    }
    if (Array.isArray(p.via)) {                        // bridge
      return p.via.map((v) => ({
        name: v.tag,
        note: `shared: ${(v.shared || []).join(', ') || '—'} · jaccard ${Number(v.jaccard || 0).toFixed(3)}`,
        pills: [
          ...(v.only_a || []).slice(0, 5).map((x) => ({ text: `A only · ${x}` })),
          ...(v.only_b || []).slice(0, 5).map((x) => ({ text: `B only · ${x}` })),
        ],
      }))
    }
    if (p.groups) {                                    // one_stop
      return p.groups.map((g) => ({
        name: g.value,
        note: `covers ${Math.round((g.coverage || 0) * 100)}% of the requirements · ${g.dish_count} dishes`
          + (g.mean_popularity != null ? ` · mean popularity ${Number(g.mean_popularity).toFixed(1)}` : ''),
      }))
    }
    if (Array.isArray(p.rows)) {                       // versatility
      return p.rows.map((r) => ({
        name: r.name || r.base_name,
        note: `${r.degree} distinct ${p.over || 'values'}`
          + (r.popularity != null ? ` · popularity ${r.popularity}` : '')
          + (r.cuisine ? ` · ${r.cuisine}` : ''),
        pills: (r.values || []).map((v) => ({ text: v })),
      }))
    }
    return []
  }

  // Dropping a ladder entry removes the matching chips and re-runs — a relaxation
  // suggestion is only worth showing if one click actually loosens the query.
  const dropConstraints = (drop) => {
    const targets = new Set((drop || []).map((d) => String(d).replace(/^[-!]/, '').toLowerCase()))
    const kept = slots.filter((s) => {
      const forms = s.kind === 'measure'
        ? [`${s.tag}${OP_SYM[s.op] || '='}${s.values?.[0]?.value}`]
        : (s.values || []).map((v) => `${s.tag}:${v.value}`)
      return !forms.some((f) => targets.has(f.toLowerCase()))
    })
    setSlots(kept)
    run(renderDSL(kept, text))
  }

  const results = () => {
    if (!materialized || error === 'not-materialized') {
      return (
        <div className="kgr-nomat">
          <b>This ontology has not been materialized</b>
          The instance graph is built from the model you saved — until it exists there is
          nothing to intersect, so the bar has no vocabulary to offer.
          <button className="kgb-btn primary" onClick={onNeedMaterialize}>Save &amp; materialize →</button>
        </div>
      )
    }
    if (loading) {
      return (
        <div className={`kgr${compact ? ' compact' : ''}`}>
          <div className="kgr-skel" /><div className="kgr-skel" /><div className="kgr-skel" />
        </div>
      )
    }
    if (error) {
      return (
        <div className="kgr-err">
          <b>The graph layer did not answer</b>
          {error === 'unreachable'
            ? 'The Knowledge Graph runs entirely in Python — check the sidecar on :8009. Vespa is not involved.'
            : error}
          <button className="kgb-btn" onClick={() => run()}>retry</button>
        </div>
      )
    }
    if (analytics) {
      const ar = analyticsRows(analytics.payload)
      return (
        <div className={`kgr${compact ? ' compact' : ''}`}>
          <div className="kgr-explain">
            <span className="kgr-expr">{analytics.q.title}</span>
            {analytics.q.why && <em>{analytics.q.why}</em>}
            <span className="kgr-took">{ar.length} rows</span>
          </div>
          {/* An analytic whose tag is missing from the CURRENT ontology still returns 200 with a
              well-formed, empty, wrong answer. The server flags that as `degraded`; refusing to
              render it here is how a blank coverage matrix stops reading as "no gaps". */}
          {analytics.payload?.degraded && (
            <div className="kgr-err" role="alert">
              <b>This rule does not fit the current ontology.</b>
              {(analytics.payload.warnings || []).map((w, i) => (
                <div key={i} className="kgr-why">{w.message || w.code}</div>
              ))}
            </div>
          )}
          <div className="kgr-list">
            {ar.length === 0 && (
              <div className="kgr-empty none"><span className="kgr-glyph">∅</span><b>Nothing to show</b></div>
            )}
            {ar.map((r, i) => (
              <div key={i} className={`kgr-row${r.head ? ' hot' : ''}`}>
                <div className="kgr-row-top"><span className="kgr-name">{r.name}</span></div>
                {r.pills && (
                  <div className="kgr-pills">
                    {r.pills.map((p, k) => (
                      <span key={k} className="kgr-pill" style={p.bad ? hue('#c62828') : undefined}>{p.text}</span>
                    ))}
                  </div>
                )}
                {r.note && <div className="kgr-serves">{r.note}</div>}
                {r.hops && <div className="kgr-why">{r.hops.map((h, k) => <span key={k} className="kgr-hop">{h}</span>)}</div>}
              </div>
            ))}
          </div>
        </div>
      )
    }
    if (!resp) {
      if (compact) return null                        // embedded mount: bar + canned only
      return (
        <div className="kgr-empty">
          <span className="kgr-glyph">⛓</span>
          <b>Fill a slot to traverse the graph</b>
          Every answer is an intersection of adjacency sets over the ontology you built —
          computed in Python, with Vespa uninvolved.
          {cannedRow(true)}
        </div>
      )
    }
    if (resp.blocked) {
      const bad = (resp.parse?.slots || []).flatMap((s) => (s.values || [])
        .filter((v) => v.status === 'unresolved').map((v) => `${s.tag}:${v.value}`))
      return (
        <div className="kgr-empty none">
          <span className="kgr-glyph">⚠</span>
          <b>{bad.join(', ') || 'That value'} is not in the ontology</b>
          It was used as a safety exclusion. Listing dishes as free of something the graph has
          never heard of would be a guess presented as a guarantee, so the query is refused
          rather than answered. Add the value to the ontology, or exclude one it knows.
        </div>
      )
    }
    const rowsOut = resp.rows || []
    const c = resp.counts || {}
    if (!rowsOut.length) {
      const relax = (resp.relaxation || []).filter((r) => !r.exhausted)
      return (
        <div className="kgr-empty none">
          <span className="kgr-glyph">∅</span>
          <b>No dish satisfies all {(resp.parse?.slots || []).length} constraints</b>
          {c.universe ? `${c.universe} dishes in the catalogue; the last step that emptied the set is shown below.` : ''}
          {(resp.steps || []).length > 0 && (
            <div className="kgr-explain">
              <span className="kgr-expr">{resp.normalized_dsl || resp.dsl}</span>
              <span className="kgr-hop">{(resp.steps || []).map((s) => s.out).join(' → ')}</span>
            </div>
          )}
          {relax.length > 0 && (
            <div className="kgr-relax">
              <span>drop one:</span>
              {relax.map((r, i) => (
                <button
                  key={i}
                  disabled={!!r.locked}
                  title={r.locked
                    ? 'safety-critical — the count is shown for transparency, but it is never dropped for you'
                    : `${r.count} dishes come back if this is dropped`}
                  onClick={() => { if (!r.locked) dropConstraints(r.drop || []) }}
                >
                  <code>{r.label || (r.drop || []).join(' + ')}</code><b>{r.count}</b>
                </button>
              ))}
            </div>
          )}
        </div>
      )
    }
    const chain = (resp.steps || []).map((s) => s.out)
    const explain = (
      <div className="kgr-explain">
        <span className="kgr-expr" title="click to edit the whole expression as text" onClick={editAsText}>
          {resp.normalized_dsl || resp.dsl}
        </span>
        {chain.length > 1 && <span className="kgr-hop">{chain.join(' → ')}</span>}
        <b>{c.distinct ?? rowsOut.length}</b>
        <em>
          dish{(c.distinct ?? rowsOut.length) === 1 ? '' : 'es'}
          {c.rolled_up_from && c.rolled_up_from !== c.distinct ? ` from ${c.rolled_up_from} listings` : ''}
        </em>
        {resp.timing_ms && <span className="kgr-took">{Number(resp.timing_ms.total).toFixed(1)} ms</span>}
      </div>
    )
    return (
      <div className={`kgr${compact ? ' compact' : ''}`}>
        {explain}
        {!compact && (resp.missing_slots || []).length > 0 && (
          <div className="kgq-slots">
            <span className="kgq-slots-l">Narrow further</span>
            {resp.missing_slots.slice(0, 6).map((ms) => (
              <button key={ms.tag} className="kgq-slot" style={hue(colorFor(ms.tag))}
                onClick={() => armTag(ms.tag)}
                title={ms.discriminative != null
                  ? `splits this result set most evenly (${Number(ms.discriminative).toFixed(2)})`
                  : 'add this slot'}>
                <i />{ms.label}{ms.values ? ` ${ms.values.length}` : ''}
              </button>
            ))}
          </div>
        )}
        <div className="kgr-split">
          <div className="kgr-list">{rowsOut.map(resultRow)}</div>
          {!compact && (
            <div className="kgr-graph">
              <div className="kgr-graph-hd">
                the walked subgraph
                {sg?.stats ? ` · ${sg.stats.nodes} nodes / ${sg.stats.edges} edges` : ''}
                {sg?.truncated?.blocked ? ` · ${sg.truncated.blocked} blocked, shown faded` : ''}
              </div>
              <div ref={boxRef} className="kgr-canvas" />
            </div>
          )}
        </div>
      </div>
    )
  }

  // ── shell ────────────────────────────────────────────────────────────────────
  return (
    <div className={`kgq${compact ? ' compact' : ''}`}>
      {!compact && (
        <div className="kgq-hd">
          <span className="kgq-hd-t">Ask the graph</span>
          <span className="kgq-hd-s">
            The slots are the entity types you modelled{model?.name ? ` in “${model.name}”` : ''} — fill them and
            the answer is an intersection of adjacency sets, not a text search.
          </span>
        </div>
      )}
      <div className="kgq-wrap">
        <div className={`kgq-field${focused ? ' focus' : ''}`} onClick={() => inputRef.current?.focus()}>
          {slots.map(chipFor)}
          {armed && <span className="kgq-armed" style={hue(colorFor(armed))}>{neg ? '¬ ' : ''}{labelFor(armed)}</span>}
          <input
            ref={inputRef}
            className="kgq-input"
            value={text}
            placeholder={armed
              ? `pick a ${labelFor(armed).toLowerCase()}…`
              : (slots.length ? 'add another slot…' : 'diet:vegan -allergen:nuts occasion:client — or just start typing')}
            onChange={(e) => { setText(e.target.value); setOpen(true); setSel('') }}
            onFocus={() => { setFocused(true); setOpen(true) }}
            onBlur={() => { setFocused(false); setTimeout(() => setOpen(false), 140) }}
            onKeyDown={onKey}
          />
          <button className="kgq-go" onClick={commitTextAndRun} disabled={loading || !modelId}>
            {loading ? '…' : preview != null && hasQuery ? `run · ${preview}` : 'run'}
          </button>
        </div>
        {panel}
      </div>
      {!compact && legend}
      {/* the hero gallery lives inside the empty state, so the plain row only appears
          once there is a result under it */}
      {(compact || resp || analytics || loading || error) && cannedRow(false)}
      {results()}
    </div>
  )
}
