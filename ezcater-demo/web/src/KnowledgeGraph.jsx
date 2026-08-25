/**
 * KnowledgeGraph.jsx — the "Knowledge Graph" top-level tab.
 *
 * Three subtabs over ONE model document:
 *   builder  — a TYPE-LEVEL canvas (a TBox). Nodes are entity TYPES, edges are relation TYPES
 *              whose names the user invents at connect time. Saving MATERIALIZES the schema into
 *              an instance graph built from data/dishes.jsonl.
 *   explorer — the old GraphExplorer, moved here, plus a source toggle between the pre-existing
 *              food ontology (/api/graph/*) and this model's materialized graph (/api/kg/graph/*).
 *   query    — the slot-filling query bar over the materialized graph.
 *
 * The whole feature is pure Python + networkx behind :8009. Vespa is not involved anywhere; every
 * surface below must still work with Vespa down, which is why nothing here talks to :8080.
 *
 * Two vis-network rules that the old explorer broke and this file must not:
 *   1. `smooth: {type:'dynamic'}` allocates one physics body PER EDGE (~1650 extra bodies on the
 *      food ontology). Never used here — 'curvedCW' on the builder, 'continuous' on the explorer.
 *   2. Physics always has a stop. Every Network either starts physics:false or turns physics off in
 *      a stabilizationIterationsDone handler and a 1100ms backstop timeout. A solver that never
 *      settles is a permanent requestAnimationFrame loop burning a core for as long as the tab lives.
 */
import { useState, useEffect, useRef, useCallback, useMemo, forwardRef, useImperativeHandle } from 'react'
import './kg.css'
import QueryBar from './QueryBar.jsx'

const DEFAULT_API = 'http://localhost:8009'

// Every colour already exists elsewhere in the product (KIND_COLOR / DIET_COLOR / --orange), so the
// new tab reads as the same app. Graph accent is #5b4b8a; the brand accent stays #e35205.
export const KG_TYPE_COLOR = {
  dish: '#e35205', cuisine: '#00695c', dish_type: '#d81b60', dish_type_group: '#ad1457',
  ingredient: '#7c8698', allergen: '#c62828', diet: '#2e7d32', meal_type: '#1140d6',
  occasion: '#5b4b8a', course: '#a15c00', flavor: '#0277bd', price_band: '#00838f',
  serving_size: '#6a1b9a', caterer: '#455a64',
}
export const KG_SWATCHES = Object.values(KG_TYPE_COLOR)
// the food ontology's own kinds (mirrors App.jsx KIND_COLOR — the explorer renders both graphs)
const KIND_COLOR = { cuisine: '#00695c', allergen: '#c62828', diet: '#2e7d32', category: '#a15c00', ingredient: '#7c8698' }
const colorForKind = (k, over) => (over && over[k]) || KIND_COLOR[k] || KG_TYPE_COLOR[k] || '#9aa0a6'

/* ────────────────────────────────────────────────────────────────────────────
   vis-network options — literal, per contract §G.4
   ──────────────────────────────────────────────────────────────────────── */

// BUILDER: ~14 hand-placed nodes. No solver, ever — positions are the user's, not a simulation's.
export const BUILDER_OPTIONS = {
  autoResize: true,
  physics: false,
  layout: { randomSeed: 7, improvedLayout: false, hierarchical: false },
  interaction: {
    hover: true, tooltipDelay: 140, hideEdgesOnDrag: false, hideEdgesOnZoom: false,
    dragNodes: true, dragView: true, zoomView: true, multiselect: true,
    selectConnectedEdges: false, navigationButtons: false, keyboard: false,
  },
  manipulation: {          // REQUIRED for addEdgeMode(); vis's own toolbar is hidden in kg.css
    enabled: true, initiallyActive: false,
    addNode: false, editNode: false, deleteNode: false, editEdge: false, deleteEdge: false,
    addEdge: null,         // replaced at construction; ALWAYS callback(null) — see onAddEdge
  },
  nodes: {
    shape: 'box', margin: { top: 11, right: 16, bottom: 11, left: 16 },
    shapeProperties: { borderRadius: 12 }, widthConstraint: { maximum: 168 },
    borderWidth: 1.5, borderWidthSelected: 3,
    font: { size: 14.5, face: 'Inter, system-ui, sans-serif', color: '#ffffff' },
    shadow: { enabled: true, size: 6, x: 0, y: 2, color: 'rgba(15,23,41,0.16)' },
  },
  edges: {
    smooth: { enabled: true, type: 'curvedCW', roundness: 0.14 },   // analytic — NO support nodes
    arrows: { to: { enabled: true, scaleFactor: 0.62, type: 'arrow' } },
    width: 1.7, hoverWidth: 0, selectionWidth: 1.4,
    font: {
      size: 11, face: 'Inter, system-ui, sans-serif', color: '#5b6472',
      strokeWidth: 5, strokeColor: '#ffffff', align: 'horizontal',
    },
    color: { color: '#98a2b3', highlight: '#e35205', hover: '#e35205', inherit: false, opacity: 0.95 },
    chosen: { edge: (v) => { v.width = 2.6 } },
  },
}

// EXPLORER: size-adaptive. Both graphs go through this — the food ontology (~1k nodes) and the
// materialized model graph (815). Above 320 nodes we drop hover/shadows; above 700, smoothing too.
export function explorerOptions(n, e) {
  const big = n > 320, huge = n > 700, manyEdges = e > 260
  return {
    autoResize: true,
    layout: { improvedLayout: false, randomSeed: 4 },   // skip the O(n²)-ish Kamada-Kawai pre-pass
    physics: {
      enabled: true, solver: 'barnesHut',
      barnesHut: {
        theta: big ? 0.75 : 0.55, gravitationalConstant: -8500, centralGravity: 0.22,
        springLength: 150, springConstant: 0.035, damping: big ? 0.7 : 0.55, avoidOverlap: big ? 0 : 0.5,
      },
      stabilization: { enabled: true, iterations: big ? 120 : 200, updateInterval: 40, fit: true },
      adaptiveTimestep: true, timestep: 0.4, maxVelocity: big ? 24 : 40, minVelocity: 1.2,
    },
    interaction: {
      hover: !big, tooltipDelay: 140, zoomView: true, dragView: true, dragNodes: true,
      hideEdgesOnDrag: big, hideEdgesOnZoom: huge, navigationButtons: false, keyboard: false,
    },
    nodes: {
      shadow: big ? false : { enabled: true, size: 6, x: 0, y: 2, color: 'rgba(15,23,41,0.14)' },
      scaling: { label: { enabled: false } },
    },
    edges: {
      smooth: huge ? false : { enabled: true, type: 'continuous', roundness: 0.2 },
      width: 1.6, hoverWidth: 0, selectionWidth: 1.2,
      arrows: { to: { enabled: true, scaleFactor: 0.55 } },
      font: manyEdges
        ? { size: 0 }
        : { size: 11, color: '#6b7280', strokeWidth: 4, strokeColor: '#ffffff', align: 'middle', face: 'Inter, system-ui, sans-serif' },
      color: { opacity: 0.5, highlight: '#5b4b8a', hover: '#5b4b8a', inherit: false },
    },
  }
}

// One forceAtlas2Based burst for auto-arrange (A), then frozen and written back into model.layout.
const ARRANGE_PHYSICS = {
  enabled: true, solver: 'forceAtlas2Based',
  forceAtlas2Based: {
    gravitationalConstant: -95, centralGravity: 0.02, springLength: 210,
    springConstant: 0.09, damping: 0.6, avoidOverlap: 0.9,
  },
  stabilization: { enabled: true, iterations: 200, updateInterval: 40, fit: true },
  adaptiveTimestep: true, maxVelocity: 30, timestep: 0.4,
}

/* ────────────────────────────────────────────────────────────────────────────
   model helpers
   ──────────────────────────────────────────────────────────────────────── */

// Names the user is likely to reach for, keyed by the type pair they just connected. Only a
// suggestion — the invented name always wins; this exists so the fast path is three keystrokes.
const REL_SUGGEST = {
  'dish>dish_type': 'IS_DISH_TYPE', 'dish>dish_type_group': 'IN_DISH_FAMILY',
  'dish>ingredient': 'CONTAINS_INGREDIENT', 'dish>cuisine': 'HAS_CUISINE',
  'dish>meal_type': 'SERVED_AT', 'dish>occasion': 'FITS_OCCASION',
  'dish>diet': 'SUITABLE_FOR', 'dish>allergen': 'HAS_ALLERGEN',
  'dish>course': 'IS_COURSE', 'dish>flavor': 'TASTES', 'dish>price_band': 'PRICED',
  'dish>serving_size': 'SERVES_GROUP', 'dish>caterer': 'OFFERED_BY',
  'dish_type>dish_type_group': 'IS_A', 'caterer>dish': 'OFFERS',
  'ingredient>allergen': 'CONTAINS', 'ingredient>cuisine': 'TYPICAL_OF',
}
const INVERSE = {
  CONTAINS_INGREDIENT: 'USED_IN', HAS_CUISINE: 'CUISINE_OF', OFFERS: 'OFFERED_BY',
  IS_A: 'HAS_KIND', CONTAINS: 'CONTAINED_IN', SUITABLE_FOR: 'SATISFIED_BY',
}
const relName = (s) => (s || '').toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_|_$/g, '')
const slug = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')
const relKey = (r) => `${r.rel}|${r.from}|${r.to}`
const titleCase = (s) => (s || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

// /api/kg/palette speaks the COMPACT binding string; the model document speaks the object form of
// §B.3. These two functions are the only place the two spellings meet on the client.
function expandBinding(compact) {
  const s = String(compact || '').trim()
  if (!s || s === 'none') return { source: 'field', field: '' }
  if (s === 'row' || s === 'doc') {
    return {
      source: 'doc', id_field: 'id', label_field: 'name',
      payload_fields: ['id', 'name', 'description', 'cuisine', 'caterer_name', 'price',
        'price_pp', 'serves', 'popularity', 'course', 'spice_level'],
    }
  }
  const i = s.indexOf(':')
  const kind = i < 0 ? s : s.slice(0, i), arg = i < 0 ? '' : s.slice(i + 1)
  if (kind === 'field') return { source: 'field', field: arg }
  if (kind === 'list') return { source: 'list_field', field: arg }
  if (kind === 'derive') return { source: 'derived', deriver: arg }
  return { source: 'field', field: arg || s }
}
function collapseBinding(b) {
  if (!b) return 'none'
  if (b.source === 'doc') return 'row'
  if (b.source === 'field') return `field:${b.field || ''}`
  if (b.source === 'list_field') return `list:${b.field || ''}`
  if (b.source === 'derived') return b.deriver ? `derive:${b.deriver}` : 'derive:(spec)'
  return 'none'
}
const bindingLabel = (b) => {
  if (!b) return 'unbound'
  if (b.source === 'doc') return 'one node per dish (the join spine)'
  if (b.source === 'field') return `field · ${b.field}`
  if (b.source === 'list_field') return `list field · ${b.field}`
  if (b.source === 'derived') return b.deriver ? `derived · ${b.deriver}()` : `derived · ${specLabel(b.spec)}`
  return b.source
}
// A `derived` binding may carry an inline spec instead of a named deriver (price_band and
// serving_size in the built-in ontology both do). There is no compact spelling for one, so
// collapseBinding hands back the 'derive:(spec)' sentinel — which matches no <option>. A
// <select> whose value matches no option silently displays option 0, so the Inspector used to
// report price_band as "field:caterer_id". This describes the spec instead.
function specLabel(spec) {
  if (!spec || typeof spec !== 'object') return 'inline spec'
  if (spec.kind === 'bucket') return `bucket over ${spec.field || '?'}`
  if (spec.kind === 'rules') return `rules over ${(spec.fields || []).join(' + ') || '?'}`
  if (spec.kind === 'map') return 'value map'
  return spec.kind ? `${spec.kind} spec` : 'inline spec'
}
const docTagOf = (m) => (m?.entity_types || []).find((t) => t.binding?.source === 'doc')?.tag || 'dish'

// Positions ride with the model, so a schema always reopens exactly as it was left. Anything the
// canvas places goes on the first free ring slot rather than on top of an existing node.
function nextSlot(layout, taken) {
  const used = Object.entries(layout || {}).filter(([k]) => taken.has(k)).map(([, p]) => p)
  for (const r of [340, 500, 660, 820]) {
    for (let i = 0; i < 14; i++) {
      const a = (i / 14) * Math.PI * 2 - Math.PI / 2
      const p = { x: Math.round(Math.cos(a) * r), y: Math.round(Math.sin(a) * r) }
      if (!used.some((q) => Math.hypot(q.x - p.x, q.y - p.y) < 120)) return p
    }
  }
  return { x: Math.round((Math.random() - 0.5) * 900), y: Math.round((Math.random() - 0.5) * 700) }
}

// The dirty flag must ignore pure repositioning — dragging a node is not an unsaved schema change,
// even though the positions do ride along on the next save.
const stripVolatile = (m) => {
  if (!m) return ''
  const { layout, updated_at, stats, materialized, version, ...rest } = m   // eslint-disable-line no-unused-vars
  return JSON.stringify(rest)
}

async function readJSON(r) {
  const d = await r.json().catch(() => null)
  if (d && d.ok === false && d.error) {
    const e = new Error(d.error.message || d.error.code || 'request failed')
    e.code = d.error.code; e.payload = d
    throw e
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return d
}
const getJSON = (url) => fetch(url).then(readJSON)
const postJSON = (url, body) => fetch(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
}).then(readJSON)

/* ────────────────────────────────────────────────────────────────────────────
   Builder — palette
   ──────────────────────────────────────────────────────────────────────── */

function Palette({ palette, model, onAdd, onNewType }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [bind, setBind] = useState('')
  const [color, setColor] = useState(KG_SWATCHES[4])
  const inUse = useMemo(() => new Set((model?.entity_types || []).map((t) => t.tag)), [model])
  const types = palette?.types || []
  const fields = palette?.fields || { scalar: [], list: [], derive: [] }
  const swatches = palette?.swatches?.length ? palette.swatches : KG_SWATCHES

  const submit = () => {
    const tag = slug(name)
    if (!tag || inUse.has(tag)) return
    onNewType({
      tag, label: titleCase(name.trim()), color,
      binding: expandBinding(bind || `field:${tag}`),
    })
    setName(''); setBind(''); setOpen(false)
  }

  return (
    <div className="kgb-palette">
      <div className="kgb-pal-hd">Entity types<span>{inUse.size} on canvas</span></div>
      <div className="kgb-pal-list">
        {types.map((t, i) => {
          const on = inUse.has(t.tag)
          const dead = (t.values ?? 0) === 0
          return (
            <button
              key={t.tag}
              className={`kgb-pal${on ? ' on' : ''}${dead ? ' dead' : ''}`}
              draggable={!on}
              onDragStart={(e) => { e.dataTransfer.setData('text/kg-type', t.tag); e.dataTransfer.effectAllowed = 'copy' }}
              onClick={() => !on && onAdd(t.tag)}
              title={`${t.binding} · ${t.values ?? 0} values${t.samples?.length ? ` — e.g. ${t.samples.slice(0, 3).join(', ')}` : ''}`}
            >
              <span className="kgb-pal-c" style={{ background: t.color || colorForKind(t.tag) }} />
              <span>{t.icon ? `${t.icon} ` : ''}{t.label}</span>
              <span className="kgb-pal-n">{t.values ?? 0}</span>
              {i < 10 && <span className="kgb-pal-key">{(i + 1) % 10}</span>}
            </button>
          )
        })}
        {!types.length && <div className="kgb-empty">palette unavailable — is the sidecar on :8009 up?</div>}
      </div>

      <button className="kgb-pal-new" onClick={() => setOpen((v) => !v)}>{open ? '× cancel' : '＋ new entity type  (N)'}</button>
      {open && (
        <div className="kgb-newtype">
          <input
            autoFocus value={name} placeholder="type name — e.g. Region"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') setOpen(false) }}
          />
          <select value={bind} onChange={(e) => setBind(e.target.value)}>
            <option value="">bind to…</option>
            <optgroup label="scalar field">{fields.scalar.map((f) => <option key={f} value={`field:${f}`}>{f}</option>)}</optgroup>
            <optgroup label="list field">{fields.list.map((f) => <option key={f} value={`list:${f}`}>{f}</option>)}</optgroup>
            <optgroup label="derived">{fields.derive.map((f) => <option key={f} value={`derive:${f}`}>{f}()</option>)}</optgroup>
          </select>
          <div className="kgb-swatches">
            {swatches.map((c) => (
              <button key={c} className={`kgb-sw${c === color ? ' on' : ''}`} style={{ background: c }}
                onClick={() => setColor(c)} title={c} />
            ))}
          </div>
          <button className="kgb-newtype-go" onClick={submit} disabled={!slug(name) || inUse.has(slug(name))}>add</button>
        </div>
      )}
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────────────
   Builder — relation popover (the on-the-fly relation name)
   ──────────────────────────────────────────────────────────────────────── */

function RelationPopover({ pending, model, onCommit, onCancel }) {
  const [from, setFrom] = useState(pending.from)
  const [to, setTo] = useState(pending.to)
  const [raw, setRaw] = useState('')
  const [label, setLabel] = useState('')
  const [both, setBoth] = useState(false)
  const inputRef = useRef(null)

  const suggested = REL_SUGGEST[`${from}>${to}`] || ''
  const rel = relName(raw) || suggested || relName(`${from}_${to}`)
  const dup = (model.relation_types || []).some((r) => r.rel === rel && r.from === from && r.to === to)
  const chips = useMemo(() => {
    const s = [suggested, 'HAS', 'IS_A', 'BELONGS_TO', 'CONTAINS', 'RELATED_TO'].filter(Boolean)
    return [...new Set(s)].slice(0, 5)
  }, [suggested])

  useEffect(() => { inputRef.current?.focus() }, [])
  useEffect(() => { setFrom(pending.from); setTo(pending.to) }, [pending.from, pending.to])

  const swap = () => { setFrom(to); setTo(from) }
  const commit = () => {
    if (!rel || dup) return
    onCommit({ rel, from, to, label: label.trim() || rel.toLowerCase().replace(/_/g, ' '), both })
  }
  const key = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit() }
    else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
    // Tab swaps direction -- but ONLY from the name field where focus starts. Intercepting it on
    // the wrapper made the whole popover a keyboard trap: the edge-label input, the inverse
    // checkbox and the buttons were mouse-only, and Tab could never leave the popover at all.
    else if (e.key === 'Tab' && !e.shiftKey && e.target === inputRef.current) {
      e.preventDefault(); swap()
    }
  }
  const labelOf = (tag) => (model.entity_types || []).find((t) => t.tag === tag)?.label || tag

  return (
    <div className="kgb-relpop" style={{ left: pending.x, top: pending.y }} onKeyDown={key}>
      <div className="kgb-relpop-hd">Name this relationship</div>
      <div className="kgb-relpop-ep">
        <span>{labelOf(from)}</span>
        <button className="kgb-dir" onClick={swap} title="swap direction (Tab)">→</button>
        <span>{labelOf(to)}</span>
      </div>
      <input
        ref={inputRef} value={raw} placeholder={suggested || 'is served at'}
        onChange={(e) => setRaw(e.target.value)}
      />
      <div className="kgb-relpop-norm">{rel}{dup && <em> — already exists on this pair</em>}</div>
      <div className="kgb-relpop-sugg">
        {chips.map((c) => (
          <button key={c} className="kgb-rel-chip" onClick={() => setRaw(c)}>{c}</button>
        ))}
      </div>
      <div className="kgb-relpop-row">
        <input value={label} placeholder="edge label — e.g. contains" onChange={(e) => setLabel(e.target.value)} />
      </div>
      <label className="kgb-both">
        <input type="checkbox" checked={both} onChange={(e) => setBoth(e.target.checked)} />
        also create the inverse ({INVERSE[rel] || `${rel}_OF`})
      </label>
      <div className="kgb-relpop-row">
        <button className="kgb-btn primary" onClick={commit} disabled={!rel || dup}>⏎ create</button>
        <button className="kgb-btn ghost" onClick={onCancel}>esc</button>
      </div>
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────────────
   Builder — inspector
   ──────────────────────────────────────────────────────────────────────── */

function Inspector({ sel, model, palette, onPatchType, onPatchRel, onDelete, delReq }) {
  const [confirm, setConfirm] = useState(null)
  useEffect(() => { setConfirm(null) }, [sel?.kind, sel?.id])
  // the ⌫ key asks for a delete; the confirm still has to be answered — no browser dialogs anywhere
  useEffect(() => { if (delReq) setConfirm(true) }, [delReq])

  if (!sel) {
    return (
      <div className="kgb-insp">
        <div className="kgb-insp-hd">Inspector</div>
        <div className="kgb-empty">
          Click a type or a relationship to edit it.<br />
          <b>R</b> starts relate mode — drag one type onto another and name the edge.
        </div>
      </div>
    )
  }

  if (sel.kind === 'entity') {
    const t = (model.entity_types || []).find((x) => x.tag === sel.id)
    if (!t) return <div className="kgb-insp"><div className="kgb-insp-hd">Inspector</div></div>
    const pal = (palette?.types || []).find((p) => p.tag === t.tag)
    const doc = t.binding?.source === 'doc'
    const cascade = (model.relation_types || []).filter((r) => r.from === t.tag || r.to === t.tag)
    const fields = palette?.fields || { scalar: [], list: [], derive: [] }
    const swatches = palette?.swatches?.length ? palette.swatches : KG_SWATCHES
    return (
      <div className="kgb-insp">
        <div className="kgb-insp-hd">{t.icon ? `${t.icon} ` : ''}{t.label}<span>{t.tag}</span></div>
        <div className="kgb-field">
          <label>Label</label>
          <input value={t.label} onChange={(e) => onPatchType(t.tag, { label: e.target.value })} />
        </div>
        <div className="kgb-field">
          <label>Bound to</label>
          <select value={collapseBinding(t.binding)} disabled={doc}
            onChange={(e) => onPatchType(t.tag, { binding: expandBinding(e.target.value) })}>
            {doc && <option value="row">one node per dish (the spine)</option>}
            {/* an inline-spec binding has no compact spelling; without this option the browser
                would fall back to option 0 and misreport what the type is bound to */}
            {collapseBinding(t.binding) === 'derive:(spec)' && (
              <option value="derive:(spec)" disabled>{bindingLabel(t.binding)}</option>
            )}
            <optgroup label="scalar field">{fields.scalar.map((f) => <option key={f} value={`field:${f}`}>{f}</option>)}</optgroup>
            <optgroup label="list field">{fields.list.map((f) => <option key={f} value={`list:${f}`}>{f}</option>)}</optgroup>
            <optgroup label="derived">{fields.derive.map((f) => <option key={f} value={`derive:${f}`}>{f}()</option>)}</optgroup>
          </select>
        </div>
        <div className="kgb-field">
          <label>Colour</label>
          <div className="kgb-swatches">
            {swatches.map((c) => (
              <button key={c} className={`kgb-sw${c === t.color ? ' on' : ''}`} style={{ background: c }}
                onClick={() => onPatchType(t.tag, { color: c })} title={c} />
            ))}
          </div>
        </div>
        <div className="kgb-field">
          <label>Vocabulary{pal ? ` — ${pal.values ?? 0} values` : ''}</label>
          <div className="kgb-samples">
            {(pal?.samples || []).map((s) => <span key={s} className="kgb-samp">{s}</span>)}
            {!pal?.samples?.length && <span className="kgb-samp">{bindingLabel(t.binding)}</span>}
          </div>
        </div>
        {doc ? (
          <div className="kgb-empty">The document type is the join spine — every relation hangs off it. It cannot be deleted.</div>
        ) : confirm ? (
          <div className="kgb-confirm">
            Delete “{t.label}”? {cascade.length} relation{cascade.length === 1 ? '' : 's'} go with it.
            <button className="kgb-btn danger" onClick={() => onDelete('entity', t.tag)}>delete</button>
            <button className="kgb-btn ghost" onClick={() => setConfirm(null)}>keep</button>
          </div>
        ) : (
          <button className="kgb-del" onClick={() => setConfirm(true)}>delete type</button>
        )}
      </div>
    )
  }

  const r = (model.relation_types || []).find((x) => relKey(x) === sel.id)
  if (!r) return <div className="kgb-insp"><div className="kgb-insp-hd">Inspector</div></div>
  const labelOf = (tag) => (model.entity_types || []).find((t) => t.tag === tag)?.label || tag
  return (
    <div className="kgb-insp">
      <div className="kgb-insp-hd">{r.rel}<span>{labelOf(r.from)} → {labelOf(r.to)}</span></div>
      <div className="kgb-field">
        <label>Name</label>
        <input value={r.rel} onChange={(e) => onPatchRel(sel.id, { rel: relName(e.target.value) })} />
      </div>
      <div className="kgb-field">
        <label>Edge label</label>
        <input value={r.label || ''} onChange={(e) => onPatchRel(sel.id, { label: e.target.value })} />
      </div>
      <div className="kgb-field">
        <label>Inverse label</label>
        <input value={r.inverse_label || ''} placeholder={`← ${r.label || ''}`}
          onChange={(e) => onPatchRel(sel.id, { inverse_label: e.target.value })} />
      </div>
      <div className="kgb-field">
        <label>Cardinality</label>
        <select value={r.cardinality || 'many_to_many'} onChange={(e) => onPatchRel(sel.id, { cardinality: e.target.value })}>
          <option value="one_to_one">one_to_one</option>
          <option value="one_to_many">one_to_many</option>
          <option value="many_to_one">many_to_one</option>
          <option value="many_to_many">many_to_many</option>
        </select>
      </div>
      <div className="kgb-field">
        <label>Direction</label>
        <button className="kgb-btn ghost" onClick={() => onPatchRel(sel.id, { from: r.to, to: r.from })}>
          ⇄ reverse to {labelOf(r.to)} → {labelOf(r.from)}
        </button>
      </div>
      {confirm ? (
        <div className="kgb-confirm">
          Delete “{r.rel}”?
          <button className="kgb-btn danger" onClick={() => onDelete('relation', sel.id)}>delete</button>
          <button className="kgb-btn ghost" onClick={() => setConfirm(null)}>keep</button>
        </div>
      ) : (
        <button className="kgb-del" onClick={() => setConfirm(true)}>delete relationship</button>
      )}
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────────────
   Builder — the canvas
   ──────────────────────────────────────────────────────────────────────── */

function Builder({
  model, models, palette, validation, dirty, saving, saveResult,
  mutate, onSave, onSwitch, onDuplicate, onReset, onRename, onJumpToQuery,
  undo, redo, canUndo, canRedo, full, setFull, draft, onRestoreDraft, onDiscardDraft,
}) {
  const boxRef = useRef(null)
  const net = useRef(null)
  const nodesDS = useRef(null)
  const edgesDS = useRef(null)
  const modelRef = useRef(model)
  const arrangeRef = useRef(false)
  const [ready, setReady] = useState(false)
  const [relate, setRelate] = useState(false)
  const [pending, setPending] = useState(null)     // an in-flight, unnamed connection
  const [sel, setSel] = useState(null)             // {kind:'entity'|'relation', id}
  const [dropping, setDropping] = useState(false)
  const [nameEdit, setNameEdit] = useState(false)
  const [delReq, setDelReq] = useState(0)

  useEffect(() => { modelRef.current = model }, [model])

  // error anchors from the live validator, so a bad edge flashes red on the canvas rather than
  // only in a list the user has to read
  const badTags = useMemo(() => new Set((validation?.errors || [])
    .filter((e) => e.anchor?.kind === 'entity').map((e) => e.anchor.tag)), [validation])
  const badRels = useMemo(() => new Set((validation?.errors || [])
    .filter((e) => e.anchor?.kind === 'relation').map((e) => `${e.anchor.rel}|${e.anchor.from}|${e.anchor.to}`)), [validation])

  const palCount = useCallback((tag) => (palette?.types || []).find((p) => p.tag === tag)?.values, [palette])

  const typeNode = useCallback((t, m) => {
    const c = t.color || colorForKind(t.tag)
    const doc = t.binding?.source === 'doc'
    const n = palCount(t.tag)
    const pos = m.layout?.[t.tag] || { x: 0, y: 0 }
    const bad = badTags.has(t.tag)
    return {
      id: t.tag,
      label: `${t.icon ? `${t.icon} ` : ''}${t.label}${n == null ? '' : `\n${n} value${n === 1 ? '' : 's'}`}`,
      x: pos.x, y: pos.y,
      color: {
        background: c, border: bad ? '#c62828' : doc ? '#8a2f02' : c,
        highlight: { background: c, border: '#e35205' }, hover: { background: c, border: '#e35205' },
      },
      borderWidth: bad ? 3.5 : doc ? 3 : 1.5,
      font: { color: '#ffffff', size: doc ? 16 : 14.5, face: 'Inter, system-ui, sans-serif', multi: false },
      title: `${t.tag} — ${bindingLabel(t.binding)}`,
    }
  }, [badTags, palCount])

  const relEdge = useCallback((r, m) => {
    const to = (m.entity_types || []).find((t) => t.tag === r.to)
    const bad = badRels.has(relKey(r))
    const c = bad ? '#c62828' : r.color || to?.color || colorForKind(r.to)
    return {
      id: relKey(r), from: r.from, to: r.to,
      label: r.label || r.rel.toLowerCase().replace(/_/g, ' '),
      dashes: !!r.dashes || bad,
      color: { color: c, highlight: '#e35205', hover: '#e35205', inherit: false, opacity: bad ? 1 : 0.9 },
      width: bad ? 2.6 : 1.7,
      title: `${r.rel} · ${r.from} → ${r.to} · via ${r.via || (r.from === docTagOf(m) || r.to === docTagOf(m) ? 'doc' : 'cooccurrence')}`,
    }
  }, [badRels])

  // Keep the DataSets in step with the model document. Positions come straight from model.layout,
  // which dragEnd writes back — so this never fights the user's hand placement.
  const sync = useCallback(() => {
    const nodes = nodesDS.current, edges = edgesDS.current, m = modelRef.current
    if (!nodes || !edges || !m) return
    const wantN = (m.entity_types || []).map((t) => typeNode(t, m))
    const keepN = new Set(wantN.map((n) => n.id))
    nodes.remove(nodes.getIds().filter((id) => !keepN.has(id)))
    nodes.update(wantN)
    const wantE = (m.relation_types || []).map((r) => relEdge(r, m))
    const keepE = new Set(wantE.map((e) => e.id))
    edges.remove(edges.getIds().filter((id) => !keepE.has(id)))
    edges.update(wantE)
  }, [typeNode, relEdge])

  // The connection gesture: vis hands us the endpoints, we ALWAYS callback(null) and draw nothing.
  // The edge only exists once the user has named it — an unnamed relation is not a relation.
  const onAddEdge = useCallback((data, cb) => {
    cb(null)
    if (!data?.from || !data?.to || data.from === data.to) { net.current?.addEdgeMode?.(); return }
    let x = 200, y = 140
    try {
      const p = net.current.getPositions([data.from, data.to])
      const mid = { x: (p[data.from].x + p[data.to].x) / 2, y: (p[data.from].y + p[data.to].y) / 2 }
      const dom = net.current.canvasToDOM(mid)
      x = Math.max(8, Math.min(dom.x, (boxRef.current?.clientWidth || 600) - 280))
      y = Math.max(8, Math.min(dom.y, (boxRef.current?.clientHeight || 480) - 260))
    } catch { /* popover falls back to the top-left corner */ }
    setPending({ from: data.from, to: data.to, x, y })
  }, [])

  useEffect(() => {
    let dead = false
    import('vis-network/standalone').then(({ Network, DataSet }) => {
      if (dead || !boxRef.current) return
      const nodes = new DataSet([]), edges = new DataSet([])
      nodesDS.current = nodes; edgesDS.current = edges
      net.current = new Network(boxRef.current, { nodes, edges }, {
        ...BUILDER_OPTIONS,
        manipulation: { ...BUILDER_OPTIONS.manipulation, addEdge: onAddEdge },
      })
      net.current.on('click', (p) => {
        if (p.nodes.length) setSel({ kind: 'entity', id: p.nodes[0] })
        else if (p.edges.length) setSel({ kind: 'relation', id: p.edges[0] })
        else setSel(null)
      })
      // dragEnd is the only writer of model.layout — snapshot:false so repositioning never fills
      // the undo ring and never trips the dirty flag on its own
      net.current.on('dragEnd', (p) => {
        if (!p.nodes?.length) return
        const pos = net.current.getPositions(p.nodes)
        mutate((m) => ({
          ...m,
          layout: { ...m.layout, ...Object.fromEntries(p.nodes.map((id) => [id, { x: Math.round(pos[id].x), y: Math.round(pos[id].y) }])) },
        }), { snapshot: false })
      })
      // auto-arrange is a single burst; the moment it settles we freeze and keep the positions
      net.current.on('stabilizationIterationsDone', () => {
        if (!arrangeRef.current) return
        arrangeRef.current = false
        try {
          const pos = net.current.getPositions()
          mutate((m) => ({
            ...m,
            layout: Object.fromEntries(Object.entries(pos).map(([id, p]) => [id, { x: Math.round(p.x), y: Math.round(p.y) }])),
          }), { snapshot: true })
        } catch { /* noop */ }
        net.current.setOptions({ physics: false })
        net.current.fit({ animation: { duration: 420 } })
      })
      setReady(true)
      sync()
      setTimeout(() => net.current?.fit?.({ animation: { duration: 420 } }), 220)
    })
    return () => { dead = true; net.current?.destroy?.(); net.current = null; nodesDS.current = null; edgesDS.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { if (ready) sync() }, [ready, model, sync])

  // relate mode is STICKY — commitRel re-arms it, so three edges take about six seconds
  useEffect(() => {
    if (!ready || !net.current) return
    if (relate) net.current.addEdgeMode()
    else { net.current.disableEditMode(); setPending(null) }
  }, [relate, ready])

  const fit = () => net.current?.fit?.({ animation: { duration: 420 } })
  const arrange = () => {
    if (!net.current) return
    arrangeRef.current = true
    net.current.setOptions({ physics: ARRANGE_PHYSICS })
    // backstop: if vis never emits stabilizationIterationsDone we still stop the solver
    setTimeout(() => {
      if (!arrangeRef.current || !net.current) return
      arrangeRef.current = false
      net.current.setOptions({ physics: false })
    }, 2600)
  }

  /* ---- model mutations -------------------------------------------------- */

  const addType = useCallback((tag, at) => {
    const m = modelRef.current
    if (!m || (m.entity_types || []).some((t) => t.tag === tag)) return
    const p = (palette?.types || []).find((x) => x.tag === tag)
    const taken = new Set((m.entity_types || []).map((t) => t.tag))
    const pos = at || nextSlot(m.layout, taken)
    mutate((mm) => ({
      ...mm,
      entity_types: [...(mm.entity_types || []), {
        tag,
        label: p?.label || titleCase(tag),
        plural: p?.plural,
        color: p?.color || colorForKind(tag),
        icon: p?.icon,
        binding: expandBinding(p?.binding || `field:${tag}`),
      }],
      layout: { ...mm.layout, [tag]: pos },
    }))
    setSel({ kind: 'entity', id: tag })
  }, [mutate, palette])

  const addCustomType = useCallback((t) => {
    const m = modelRef.current
    if (!m || (m.entity_types || []).some((x) => x.tag === t.tag)) return
    const taken = new Set((m.entity_types || []).map((x) => x.tag))
    mutate((mm) => ({
      ...mm,
      entity_types: [...(mm.entity_types || []), t],
      layout: { ...mm.layout, [t.tag]: nextSlot(mm.layout, taken) },
    }))
    setSel({ kind: 'entity', id: t.tag })
  }, [mutate])

  const commitRel = useCallback(({ rel, from, to, label, both }) => {
    const m = modelRef.current
    const doc = docTagOf(m)
    const toType = (m.entity_types || []).find((t) => t.tag === to)
    const fromType = (m.entity_types || []).find((t) => t.tag === from)
    const mk = (r, f, t, l, ty) => ({
      rel: r, label: l, inverse_label: `← ${l}`, from: f, to: t,
      cardinality: 'many_to_many', directed: true,
      via: f === doc || t === doc ? 'doc' : 'cooccurrence',
      color: ty?.color || colorForKind(t), user_created: true,
    })
    const add = [mk(rel, from, to, label, toType)]
    if (both) {
      const inv = INVERSE[rel] || `${rel}_OF`
      if (!(m.relation_types || []).some((r) => r.rel === inv && r.from === to && r.to === from)) {
        add.push(mk(inv, to, from, inv.toLowerCase().replace(/_/g, ' '), fromType))
      }
    }
    mutate((mm) => ({ ...mm, relation_types: [...(mm.relation_types || []), ...add] }))
    setPending(null)
    setSel({ kind: 'relation', id: relKey(add[0]) })
    if (relate) setTimeout(() => net.current?.addEdgeMode?.(), 0)   // sticky
  }, [mutate, relate])

  const patchType = useCallback((tag, patch) => {
    mutate((m) => ({ ...m, entity_types: (m.entity_types || []).map((t) => (t.tag === tag ? { ...t, ...patch } : t)) }))
  }, [mutate])

  const patchRel = useCallback((key, patch) => {
    let nextKey = key
    mutate((m) => ({
      ...m,
      relation_types: (m.relation_types || []).map((r) => {
        if (relKey(r) !== key) return r
        const n = { ...r, ...patch }
        nextKey = relKey(n)
        return n
      }),
    }))
    if (nextKey !== key) setSel({ kind: 'relation', id: nextKey })
  }, [mutate])

  const del = useCallback((kind, id) => {
    if (kind === 'entity') {
      mutate((m) => {
        const layout = { ...m.layout }; delete layout[id]
        return {
          ...m,
          entity_types: (m.entity_types || []).filter((t) => t.tag !== id),
          relation_types: (m.relation_types || []).filter((r) => r.from !== id && r.to !== id),
          layout,
        }
      })
    } else {
      mutate((m) => ({ ...m, relation_types: (m.relation_types || []).filter((r) => relKey(r) !== id) }))
    }
    setSel(null)
  }, [mutate])

  /* ---- keyboard --------------------------------------------------------- */

  useEffect(() => {
    const onKey = (e) => {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return
      const meta = e.metaKey || e.ctrlKey
      if (meta && e.key === 'Enter') { e.preventDefault(); onJumpToQuery(); return }
      if (meta && (e.key === 's' || e.key === 'S')) { e.preventDefault(); onSave(); return }
      if (meta && (e.key === 'd' || e.key === 'D')) { e.preventDefault(); onDuplicate(); return }
      if (meta && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); e.shiftKey ? redo() : undo(); return }
      if (meta) return
      if (e.key === 'Escape') {
        if (pending) setPending(null)
        else if (relate) setRelate(false)
        else if (full) setFull(false)
        return
      }
      if (/^[0-9]$/.test(e.key)) {
        const i = e.key === '0' ? 9 : Number(e.key) - 1
        const t = (palette?.types || [])[i]
        if (t && !(model.entity_types || []).some((x) => x.tag === t.tag)) { e.preventDefault(); addType(t.tag) }
        return
      }
      const k = e.key.toLowerCase()
      if (k === 'r') { e.preventDefault(); setRelate((v) => !v) }
      else if (k === 'f') { e.preventDefault(); fit() }
      else if (k === 'a') { e.preventDefault(); arrange() }
      else if (k === 'e') {
        if (sel?.kind === 'entity') { e.preventDefault(); document.querySelector('.kgb-insp input')?.focus() }
        else { e.preventDefault(); setNameEdit(true) }
      } else if ((e.key === 'Backspace' || e.key === 'Delete') && sel) {
        e.preventDefault()
        setDelReq((v) => v + 1)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [palette, model, sel, relate, pending, full, addType, onSave, onDuplicate, onJumpToQuery, undo, redo, setFull])

  /* ---- drag & drop from the palette ------------------------------------- */

  const onDrop = (e) => {
    e.preventDefault(); setDropping(false)
    const tag = e.dataTransfer.getData('text/kg-type')
    if (!tag || !net.current) return
    const box = boxRef.current.getBoundingClientRect()
    let at = null
    try {
      const p = net.current.DOMtoCanvasPosition({ x: e.clientX - box.left, y: e.clientY - box.top })
      at = { x: Math.round(p.x), y: Math.round(p.y) }
    } catch { /* fall back to the ring */ }
    addType(tag, at)
  }

  const est = validation?.estimate
  const errs = validation?.errors?.length || 0
  const warns = validation?.warnings?.length || 0
  const nTypes = model?.entity_types?.length || 0
  const nRels = model?.relation_types?.length || 0

  return (
    <div className={`kgb${full ? ' full' : ''}`}>
      <div className="kgb-bar">
        {saving && <div className="kgb-bar-prog" />}
        <div className="kgb-model">
          <select className="kgb-model-sel" value={model?.id || ''} onChange={(e) => onSwitch(e.target.value)}>
            {(models || []).map((m) => (
              <option key={m.id} value={m.id}>{m.name}{m.builtin ? ' · built-in' : ''}</option>
            ))}
          </select>
          {nameEdit ? (
            <input className="kgb-name-edit" autoFocus defaultValue={model?.name || ''}
              onBlur={(e) => { onRename(e.target.value); setNameEdit(false) }}
              onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); if (e.key === 'Escape') setNameEdit(false) }} />
          ) : (
            <button className="kgb-btn ghost" onClick={() => setNameEdit(true)} title="rename (E)">✎</button>
          )}
          {dirty && <span className="kgb-dirty" title="unsaved schema changes">●</span>}
        </div>
        <span className="kgb-sep" />
        <button className={`kgb-btn${relate ? ' primary' : ''}`} onClick={() => setRelate((v) => !v)} title="relate mode (R)">
          ⇢ relate {relate ? 'on' : ''}
        </button>
        <button className="kgb-btn ghost" onClick={fit} title="fit (F)">⤢ fit</button>
        <button className="kgb-btn ghost" onClick={arrange} title="auto-arrange (A)">✧ arrange</button>
        <button className="kgb-btn ghost" onClick={undo} disabled={!canUndo} title="undo (⌘Z)">↶</button>
        <button className="kgb-btn ghost" onClick={redo} disabled={!canRedo} title="redo (⇧⌘Z)">↷</button>
        <div className="kgb-right">
          <button className="kgb-btn ghost" onClick={onDuplicate} title="fork this model (⌘D)">⧉ fork</button>
          <button className="kgb-btn ghost" onClick={onReset} title="reset to the built-in ontology">↺ reset</button>
          <button className="kgb-btn ghost" onClick={() => setFull((v) => !v)}>{full ? '✕ exit' : '⛶ fullscreen'}</button>
          <button className="kgb-btn primary" onClick={onSave} disabled={!!saving || errs > 0} title="save & materialize (⌘S)">
            {saving ? <><span className="kgb-spin" />{saving === 'building' ? 'materializing…' : 'saving…'}</> : '⤓ Save & materialize'}
          </button>
        </div>
      </div>

      {draft && (
        <div className="kgb-confirm">
          An unsaved draft of this ontology was restored from your last session.
          <button className="kgb-btn primary" onClick={onRestoreDraft}>use the draft</button>
          <button className="kgb-btn ghost" onClick={onDiscardDraft}>discard</button>
        </div>
      )}

      <div className="kgb-main">
        <Palette palette={palette} model={model} onAdd={addType} onNewType={addCustomType} />

        <div className="kgb-canvas-wrap">
          <div
            ref={boxRef}
            className={`kgb-canvas${relate ? ' relate' : ''}${saving ? ' busy' : ''}${dropping ? ' dropping' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDropping(true) }}
            onDragLeave={() => setDropping(false)}
            onDrop={onDrop}
          />
          {!nTypes && (
            <div className="kgb-canvas-empty">
              Drop an entity type from the left, then press <b>R</b> and drag one type onto another to
              invent a relationship.
            </div>
          )}
          <div className="kgb-hud">
            <span className="phase">{nTypes} types · {nRels} relationships</span>
            {est && <span className="phase">≈ {est.est_nodes?.toLocaleString?.() ?? est.est_nodes} nodes · ≈ {est.est_edges?.toLocaleString?.() ?? est.est_edges} edges</span>}
            {errs > 0 && <span className="phase">{errs} error{errs === 1 ? '' : 's'}</span>}
            {errs === 0 && warns > 0 && <span className="phase">{warns} warning{warns === 1 ? '' : 's'}</span>}
          </div>
          {relate && <div className="kgb-mode">relate mode — drag one type onto another · esc to stop</div>}
          <div className="kgb-hint">click a node to inspect · drag to place · <b>?</b> for shortcuts</div>
          {pending && (
            <RelationPopover pending={pending} model={model} onCommit={commitRel} onCancel={() => setPending(null)} />
          )}
        </div>

        <Inspector sel={sel} model={model} palette={palette} delReq={delReq}
          onPatchType={patchType} onPatchRel={patchRel} onDelete={del} />
      </div>

      {errs > 0 && (
        <div className="kgb-saved err">
          <div className="kgb-saved-h">{errs} problem{errs === 1 ? '' : 's'} block materialization</div>
          {(validation.errors || []).slice(0, 5).map((er, i) => (
            <div key={i} className="kgb-warn">{er.message}{er.hint ? ` — ${er.hint}` : ''}</div>
          ))}
        </div>
      )}

      {saveResult && (
        <div className={`kgb-saved${saveResult.error ? ' err' : ''}`}>
          {saveResult.error ? (
            <>
              <div className="kgb-saved-h">Materialization failed</div>
              <div className="kgb-warn">{saveResult.error}</div>
            </>
          ) : (
            <>
              <div className="kgb-saved-h">
                <span className="kgb-stat">{saveResult.stats.nodes.toLocaleString()} nodes</span>
                <span className="kgb-stat">{saveResult.stats.edges.toLocaleString()} edges</span>
                <span className="kgb-stat">{saveResult.stats.orphans} orphans</span>
                <span className="kgb-took">{Math.round(saveResult.stats.elapsed_ms)} ms · {saveResult.stats.docs_read} docs</span>
                <button className="kgb-saved-go" onClick={onJumpToQuery}>query it →</button>
              </div>
              <div className="kgb-stats">
                {(saveResult.stats.entity_counts || []).map((c) => (
                  <span key={c.tag} className="kgb-stat-pill" style={{ borderColor: `${c.color || colorForKind(c.tag)}55`, color: c.color || colorForKind(c.tag) }}>
                    {c.label || c.tag} <b>{c.nodes}</b>
                  </span>
                ))}
                {(saveResult.stats.relation_counts || []).map((c) => (
                  <span key={`${c.rel}|${c.from}|${c.to}`} className="kgb-stat-pill rel">{c.rel} <b>{c.edges.toLocaleString()}</b></span>
                ))}
              </div>
              {(saveResult.stats.warnings || []).map((w, i) => (
                <div key={i} className="kgb-warn">{w.message || String(w)}</div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────────────
   Explorer — the old GraphExplorer, moved here, over EITHER graph
   ──────────────────────────────────────────────────────────────────────── */

// How a query subgraph paints onto the explorer canvas. This is the difference between a filter
// and a graph: you see the concepts you asked for, the dishes that matched, and — faded — the
// dishes an exclusion removed.
const ROLE_STYLE = {
  include_hit: { bg: '#2e7d32', br: '#2e7d32', fg: '#ffffff', dashes: false },
  include_miss: { bg: '#ffffff', br: '#b0854a', fg: '#7a5c2e', dashes: true },
  exclude: { bg: '#fff5f5', br: '#c62828', fg: '#c62828', dashes: true },
  result: { bg: '#e35205', br: '#e35205', fg: '#ffffff', dashes: false },
  blocked: { bg: '#ffffff', br: '#c9ced8', fg: '#9aa0a6', dashes: true },
  context: { bg: '#ffffff', br: '#7c8698', fg: '#4b5563', dashes: false },
}

const ExplorerPane = forwardRef(function ExplorerPane({ api, model, health, onNeedMaterialize }, ref) {
  const boxRef = useRef(null)
  const net = useRef(null)
  const nodesDS = useRef(null)
  const edgesDS = useRef(null)
  const expanded = useRef(new Set())
  const settle = useRef(null)
  const srcRef = useRef('food')
  const [src, setSrc] = useState(() => {
    try { return localStorage.getItem('ezc_kg_src') || 'food' } catch { return 'food' }
  })
  const [stats, setStats] = useState(null)
  const [full, setFull] = useState(false)
  const [sq, setSq] = useState('')
  const [sugg, setSugg] = useState([])
  const [painted, setPainted] = useState(null)
  const [ready, setReady] = useState(false)

  const modelId = model?.id
  const materialized = !!model?.materialized
  useEffect(() => { srcRef.current = src; try { localStorage.setItem('ezc_kg_src', src) } catch { /* noop */ } }, [src])

  const typeColor = useMemo(() => Object.fromEntries(
    (model?.entity_types || []).map((t) => [t.tag, t.color || colorForKind(t.tag)])), [model])
  const relMeta = useMemo(() => Object.fromEntries(
    (model?.relation_types || []).map((r) => [r.rel, r])), [model])
  const doc = docTagOf(model)

  const base = useCallback((path, params = '') => (srcRef.current === 'model'
    ? `${api}/api/kg/graph/${path}?model=${encodeURIComponent(modelId || '')}${params}`
    : `${api}/api/graph/${path}?${params.replace(/^&/, '')}`), [api, modelId])

  const toVisNode = useCallback((n) => {
    const model_ = srcRef.current === 'model'
    const anchor = model_ ? n.kind !== doc : n.kind !== 'ingredient'
    const c = model_ ? (typeColor[n.kind] || colorForKind(n.kind)) : colorForKind(n.kind)
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
      title: n.n != null ? `${n.kind} · ${n.n} dish${n.n === 1 ? '' : 'es'}` : n.kind,
    }
  }, [doc, typeColor])

  // food-ontology relations have fixed semantics; model-graph relations are whatever the user named
  const REL = {
    FEATURES: { color: '#17a08e', dashes: false, label: 'features' },
    MEMBER: { color: '#a15c00', dashes: false, label: 'is a' },
    CONTAINS: { color: '#c62828', dashes: true, label: 'contains' },
    CONFLICTS: { color: '#c62828', dashes: true, label: 'not allowed in' },
  }
  const toVisEdge = useCallback((e) => {
    const m = srcRef.current === 'model' ? relMeta[e.rel] : null
    const r = m
      ? { color: m.color || typeColor[m.to] || '#8a94a6', dashes: !!m.dashes, label: e.label || m.label || '' }
      : REL[e.rel] || { color: '#8a94a6', dashes: false, label: (e.rel || '').toLowerCase() }
    return {
      id: `${e.from}->${e.to}`, from: e.from, to: e.to,
      arrows: { to: { enabled: true, scaleFactor: 0.55 } }, dashes: r.dashes, label: r.label,
      font: { size: 11, color: '#6b7280', strokeWidth: 4, strokeColor: '#ffffff', align: 'middle' },
      color: { color: r.color, opacity: 0.5, highlight: '#5b4b8a', hover: '#5b4b8a' },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relMeta, typeColor])

  // Every load re-tunes the solver for the new size, then hard-stops it. The 1100ms backstop is
  // what keeps the tab from holding a requestAnimationFrame loop open forever.
  const nudge = useCallback(() => {
    const n = nodesDS.current, e = edgesDS.current
    if (!net.current || !n) return
    net.current.setOptions(explorerOptions(n.length, e ? e.length : 0))
    net.current.setOptions({ physics: { enabled: true } })
    clearTimeout(settle.current)
    settle.current = setTimeout(() => net.current?.setOptions?.({ physics: { enabled: false } }), 1100)
  }, [])

  const expand = useCallback((id, limit = 25) => {
    const nodes = nodesDS.current, edges = edgesDS.current
    if (!nodes) return null
    expanded.current.add(id)
    return fetch(`${base('neighbors', `&node=${encodeURIComponent(id)}&limit=${limit}`)}`)
      .then((r) => r.json())
      .then((d) => {
        nodes.update((d.nodes || []).map(toVisNode))
        edges.update((d.edges || []).map(toVisEdge))
        // an expansion that would have exploded the canvas is offered as a ghost, never taken
        const ghost = `more:${id}`
        nodes.remove(ghost)
        if (d.truncated && d.total > limit) {
          nodes.add({
            id: ghost, label: `+${d.total - limit} more`, shape: 'box',
            shapeProperties: { borderRadius: 11 }, margin: 8,
            color: { background: '#ffffff', border: '#c9ced8', highlight: { background: '#f6f7fa', border: '#5b4b8a' } },
            font: { color: '#6b7280', size: 12.5, face: 'Inter, system-ui, sans-serif' },
            borderWidth: 1.5, _ghost: true, _for: id, _limit: Math.min(d.total, limit * 3),
          })
          edges.update({ id: `${id}->${ghost}`, from: id, to: ghost, dashes: true, color: { color: '#c9ced8', opacity: 0.6 } })
        }
        nudge()
      })
      .catch(() => { })
  }, [base, toVisNode, toVisEdge, nudge])

  const collapse = useCallback((id) => {
    const nodes = nodesDS.current, edges = edgesDS.current
    expanded.current.delete(id)
    nodes.remove(`more:${id}`)
    const nbrs = edges.get().filter((e) => e.from === id || e.to === id).map((e) => (e.from === id ? e.to : e.from))
    nbrs.forEach((nid) => {
      const nd = nodes.get(nid)
      if (!nd || nd._anchor || expanded.current.has(nid)) return
      if (edges.get().filter((e) => e.from === nid || e.to === nid).length <= 1) nodes.remove(nid)
    })
    const present = new Set(nodes.getIds())
    edges.remove(edges.get().filter((e) => !present.has(e.from) || !present.has(e.to)).map((e) => e.id))
    nudge()
  }, [nudge])

  const roots = useCallback(() => {
    const nodes = nodesDS.current, edges = edgesDS.current
    if (!nodes) return
    expanded.current = new Set(); edges.clear(); nodes.clear(); setPainted(null)
    fetch(base('roots', '&limit=40')).then((r) => r.json()).then((d) => {
      nodes.add((d.nodes || []).map(toVisNode))
      setStats(d.stats || null)
      nudge()
      setTimeout(() => net.current?.fit?.({ animation: { duration: 450 } }), 260)
    }).catch(() => setStats(null))
  }, [base, toVisNode, nudge])

  useEffect(() => {
    let dead = false
    import('vis-network/standalone').then(({ Network, DataSet }) => {
      if (dead || !boxRef.current) return
      const nodes = new DataSet([]), edges = new DataSet([])
      nodesDS.current = nodes; edgesDS.current = edges
      net.current = new Network(boxRef.current, { nodes, edges }, explorerOptions(0, 0))
      net.current.on('click', (p) => {
        if (!p.nodes.length) return
        const id = p.nodes[0]
        const nd = nodes.get(id)
        if (nd?._ghost) { nodes.remove(id); expand(nd._for, nd._limit); return }
        if (expanded.current.has(id)) collapse(id); else expand(id)
      })
      // the one guaranteed stop: the solver switches itself off the moment it settles
      net.current.on('stabilizationIterationsDone', () => {
        net.current?.setOptions?.({ physics: { enabled: false } })
        net.current?.fit?.({ animation: { duration: 450 } })
      })
      setReady(true)
    })
    return () => {
      dead = true; clearTimeout(settle.current)
      net.current?.destroy?.(); net.current = null; nodesDS.current = null; edgesDS.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // source / model switches reload the DataSets IN PLACE — the Network is never rebuilt, which is
  // what keeps the canvas from flickering white on every toggle
  useEffect(() => {
    if (!ready) return
    if (src === 'model' && !materialized) {
      nodesDS.current?.clear(); edgesDS.current?.clear(); setStats(null); setPainted(null)
      return
    }
    roots()
  }, [ready, src, modelId, materialized, roots])

  useEffect(() => {
    if (!sq.trim()) { setSugg([]); return }
    const t = setTimeout(() => {
      fetch(base('search', `&q=${encodeURIComponent(sq)}&limit=12`)).then((r) => r.json())
        .then((d) => setSugg(d.nodes || [])).catch(() => setSugg([]))
    }, 130)
    return () => clearTimeout(t)
  }, [sq, base])

  // vis needs an explicit resize when the canvas box changes size (fullscreen in/out)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') setFull(false) }
    window.addEventListener('keydown', onKey)
    const resize = () => {
      try {
        net.current?.setSize?.('100%', '100%'); net.current?.redraw?.()
        net.current?.fit?.({ animation: { duration: 350 } })
      } catch { /* noop */ }
    }
    const t1 = setTimeout(resize, 80), t2 = setTimeout(resize, 320)
    return () => { window.removeEventListener('keydown', onKey); clearTimeout(t1); clearTimeout(t2) }
  }, [full])

  const focusNode = (n) => {
    const nodes = nodesDS.current
    if (!nodes) return
    if (!nodes.get(n.id)) nodes.add(toVisNode(n))
    const done = () => {
      net.current?.selectNodes?.([n.id])
      net.current?.focus?.(n.id, { scale: 1.1, animation: { duration: 600 } })
    }
    if (!expanded.current.has(n.id)) { const p = expand(n.id); (p && p.then ? p : Promise.resolve()).then(() => setTimeout(done, 80)) }
    else setTimeout(done, 20)
    setSq(''); setSugg([])
  }

  // ---- the imperative surface the query bar drives -------------------------

  const focusNodes = useCallback((ids) => {
    const nodes = nodesDS.current
    if (!nodes || !net.current) return
    const present = (ids || []).filter((id) => nodes.get(id))
    if (!present.length) { net.current.unselectAll?.(); return }
    net.current.selectNodes(present)
    net.current.fit({ nodes: present, animation: { duration: 420 } })
  }, [])

  // A query result is repainted onto the SAME Network — DataSets are replaced in place and physics
  // gets exactly one burst. Destroying and recreating the canvas is the main cause of flicker.
  const paintSub = useCallback((sub) => {
    const nodes = nodesDS.current, edges = edgesDS.current
    if (!nodes || !edges || !sub?.nodes?.length) return
    expanded.current = new Set()
    nodes.clear(); edges.clear()
    nodes.add(sub.nodes.map((n) => {
      const st = ROLE_STYLE[n.role] || ROLE_STYLE.context
      const filled = n.role === 'include_hit' || n.role === 'result'
      return {
        id: n.id, label: n.label, _anchor: n.role !== 'result' && n.role !== 'blocked',
        shape: 'box', margin: filled ? 11 : 8,
        shapeProperties: { borderRadius: filled ? 18 : 11, borderDashes: st.dashes ? [5, 4] : false },
        color: {
          background: st.bg, border: st.br,
          highlight: { background: st.bg, border: '#5b4b8a' }, hover: { background: st.bg, border: '#5b4b8a' },
        },
        font: { color: st.fg, size: filled ? 16 : 13.5, face: 'Inter, system-ui, sans-serif' },
        borderWidth: st.dashes ? 2 : 1.6,
        opacity: n.role === 'blocked' ? 0.45 : 1,
        title: `${n.role}${n.blocked_by?.length ? ` — removed by ${n.blocked_by.join(', ')}` : ''}`,
      }
    }))
    edges.add(sub.edges.map((e) => ({
      id: `${e.from}->${e.to}`, from: e.from, to: e.to,
      label: (e.rel || '').toLowerCase().replace(/_/g, ' '),
      dashes: e.role === 'blocked', arrows: { to: { enabled: true, scaleFactor: 0.55 } },
      font: { size: 11, color: '#6b7280', strokeWidth: 4, strokeColor: '#ffffff', align: 'middle' },
      color: {
        color: e.role === 'blocked' ? '#c9ced8' : e.role === 'matched' ? '#2e7d32' : '#b6bcc6',
        opacity: e.role === 'blocked' ? 0.35 : 0.6, highlight: '#5b4b8a', hover: '#5b4b8a',
      },
    })))
    setPainted(sub)
    nudge()
    // fit on the query's own concepts, so the canvas opens centred on what was asked
    setTimeout(() => {
      const f = (sub.focus || []).filter((id) => nodes.get(id))
      try {
        if (f.length) net.current?.fit?.({ nodes: f, animation: { duration: 500 } })
        else net.current?.fit?.({ animation: { duration: 500 } })
      } catch { /* noop */ }
    }, 300)
  }, [nudge])

  useImperativeHandle(ref, () => ({ focus: focusNodes, paint: paintSub, clear: roots }), [focusNodes, paintSub, roots])

  const foodStats = health?.graph
  const modelDead = src === 'model' && !materialized

  return (
    <div className={`gexp ${full ? 'full' : ''}`}>
      <div className="gexp-bar">
        <span className="gexp-title">🕸 {src === 'model' ? model?.name || 'Model graph' : 'Food ontology'}</span>
        <div className="kg-src">
          <button className={src === 'food' ? 'on' : ''} onClick={() => setSrc('food')}
            title="the hand-built ingredient ontology behind query understanding">
            food ontology{foodStats ? ` · ${foodStats.nodes ?? foodStats.ingredient ?? ''}` : ''}
          </button>
          <button className={src === 'model' ? 'on' : ''} onClick={() => setSrc('model')}
            title="the graph materialized from the ontology you built">
            this model{model?.stats?.nodes ? ` · ${model.stats.nodes}` : ''}
          </button>
        </div>
        <div className="gexp-search">
          <input value={sq} onChange={(e) => setSq(e.target.value)} disabled={modelDead}
            placeholder={src === 'model' ? 'search a node — vegan, Italian, Chana Masala…' : 'search a node — tahini, nuts, Italian…'}
            onBlur={() => setTimeout(() => setSugg([]), 150)} />
          {sugg.length > 0 && (
            <div className="gexp-sugg">
              {sugg.map((n) => (
                <div key={n.id} className="gexp-sg" onMouseDown={() => focusNode(n)}>
                  <i style={{ background: src === 'model' ? (typeColor[n.kind] || colorForKind(n.kind)) : colorForKind(n.kind) }} />
                  <span>{n.label}</span><em>{n.kind}{n.n ? ` · ${n.n}` : ''}</em>
                </div>
              ))}
            </div>
          )}
        </div>
        <button className="gexp-btn" onClick={() => net.current?.fit?.({ animation: { duration: 400 } })} title="fit to view">⤢ fit</button>
        <button className="gexp-btn" onClick={roots} title="reset to anchors">↺ reset</button>
        <button className="gexp-btn" onClick={() => setFull((v) => !v)}>{full ? '✕ exit' : '⛶ fullscreen'}</button>
        {stats && (
          <span className="gexp-stats">
            {src === 'model'
              ? `${stats.nodes} nodes · ${stats.edges} edges`
              : `${stats.ingredient} ingredients · ${stats.cuisine} cuisines · ${stats.allergen} allergens · ${stats.edges} edges`}
          </span>
        )}
      </div>

      <div className="gexp-legend">
        {src === 'model'
          ? (model?.entity_types || []).slice(0, 9).map((t) => (
            <span key={t.tag}><i style={{ background: t.color || colorForKind(t.tag) }} />{t.label}</span>
          ))
          : (
            <>
              <span><i style={{ background: KIND_COLOR.cuisine }} />cuisine</span>
              <span><i style={{ background: KIND_COLOR.category }} />category</span>
              <span><i style={{ background: KIND_COLOR.allergen }} />allergen</span>
              <span><i style={{ background: KIND_COLOR.diet }} />diet</span>
              <span><i style={{ background: '#fff', borderColor: '#9aa0a6' }} />ingredient</span>
            </>
          )}
        <span className="gexp-tip">
          {painted ? 'showing a query subgraph — reset to browse again' : 'click a node to expand · click again to collapse'}
        </span>
      </div>

      {painted?.legend && (
        <div className="gexp-legend">
          {Object.entries(painted.legend).map(([role, l]) => (
            <span key={role}><i style={{ background: l.color, opacity: l.opacity ?? 1 }} />{l.label}</span>
          ))}
        </div>
      )}

      {src === 'model' && (
        materialized
          ? <QueryBar model={model} api={api} compact
            onNeedMaterialize={onNeedMaterialize}
            onResult={(r) => { if (r?.subgraph) paintSub(r.subgraph); else if (r === null) roots() }}
            onSubgraph={paintSub}
            onFocusNodes={focusNodes} />
          : (
            <div className="kgr-nomat">
              This ontology has not been materialized yet — there is no instance graph to browse.
              <button className="kgb-btn primary" onClick={onNeedMaterialize}>Save &amp; materialize →</button>
            </div>
          )
      )}

      <div ref={boxRef} className="gexp-canvas" />
      <div className="gexp-perf">
        physics stops as soon as the layout settles · expansions are capped at 25 neighbours
      </div>
    </div>
  )
})

/* ────────────────────────────────────────────────────────────────────────────
   Shortcuts overlay
   ──────────────────────────────────────────────────────────────────────── */

const SHORTCUTS = [
  ['1 – 0', 'drop the Nth entity type on the canvas'],
  ['N', 'new custom entity type'],
  ['R', 'relate mode (sticky) — drag one type onto another'],
  ['E', 'rename the selection'],
  ['⌫ / Del', 'delete the selection'],
  ['⌘S', 'save & materialize'],
  ['⌘D', 'fork this model'],
  ['⌘Z / ⇧⌘Z', 'undo / redo'],
  ['F', 'fit'],
  ['A', 'auto-arrange'],
  ['⌘⏎', 'jump to the query bar'],
  ['Esc', 'exit relate → close popover → exit fullscreen'],
  ['?', 'this cheatsheet'],
]

function ShortcutsOverlay({ onClose }) {
  return (
    <div className="kg-keys" onClick={onClose}>
      <div className="kg-keys-box" onClick={(e) => e.stopPropagation()}>
        <h3>Keyboard</h3>
        <div className="kg-keys-grid">
          {SHORTCUTS.map(([k, d]) => <span key={k}><kbd>{k}</kbd>{d}</span>)}
        </div>
      </div>
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────────────
   The tab shell
   ──────────────────────────────────────────────────────────────────────── */

const SUBTABS = [
  ['builder', '✎', 'Build the ontology'],
  ['explorer', '🕸', 'Explore the graph'],
  ['query', '⌕', 'Query the graph'],
]

export default function KnowledgeGraph({ health = null, onRefreshHealth = () => { }, api = DEFAULT_API }) {
  const [sub, setSub] = useState(() => {
    try { return localStorage.getItem('ezc_kg_sub') || 'builder' } catch { return 'builder' }
  })
  const [models, setModels] = useState([])
  const [model, setModel] = useState(null)
  const [palette, setPalette] = useState(null)
  const [validation, setValidation] = useState(null)
  const [saving, setSaving] = useState(null)          // null | 'saving' | 'building'
  const [saveResult, setSaveResult] = useState(null)
  const [err, setErr] = useState('')
  const [full, setFull] = useState(false)
  const [keys, setKeys] = useState(false)
  const [draft, setDraft] = useState(null)            // a restorable localStorage draft
  const [histTick, setHistTick] = useState(0)         // the ring lives in a ref; this re-renders the buttons

  const modelRef = useRef(null)
  const savedRef = useRef('')
  const hist = useRef({ past: [], future: [] })
  const explorerRef = useRef(null)
  const seq = useRef(0)

  useEffect(() => { try { localStorage.setItem('ezc_kg_sub', sub) } catch { /* noop */ } }, [sub])

  /* ---- load ------------------------------------------------------------- */

  const loadModel = useCallback(async (id) => {
    const d = await getJSON(`${api}/api/kg/model/${encodeURIComponent(id)}`)
    const m = d.model
    modelRef.current = m
    savedRef.current = stripVolatile(m)
    hist.current = { past: [], future: [] }
    setModel(m); setSaveResult(null); setValidation(null)
    try { localStorage.setItem('ezc_kg_model', m.id) } catch { /* noop */ }
    // a draft from a previous session is offered, never silently applied — losing a save is bad,
    // but silently resurrecting a half-finished schema is worse
    try {
      const raw = localStorage.getItem(`ezc_kg_draft_${m.id}`)
      if (raw) {
        const d2 = JSON.parse(raw)
        setDraft(stripVolatile(d2) !== savedRef.current ? d2 : null)
      } else setDraft(null)
    } catch { setDraft(null) }
    return m
  }, [api])

  const loadModels = useCallback(async () => {
    const d = await getJSON(`${api}/api/kg/models`)
    setModels(d.models || [])
    return d
  }, [api])

  useEffect(() => {
    let dead = false
    ;(async () => {
      try {
        const [pal, ms] = await Promise.all([
          getJSON(`${api}/api/kg/palette`).catch(() => null),
          loadModels(),
        ])
        if (dead) return
        if (pal) setPalette(pal)
        let want = null
        try { want = localStorage.getItem('ezc_kg_model') } catch { /* noop */ }
        const ids = new Set((ms.models || []).map((m) => m.id))
        const id = want && ids.has(want) ? want : ms.active || (ms.models || [])[0]?.id || 'catering-core'
        await loadModel(id)
      } catch (e) {
        if (!dead) setErr(e.message || 'the Knowledge Graph sidecar is unreachable')
      }
    })()
    return () => { dead = true }
  }, [api, loadModel, loadModels])

  /* ---- mutation, undo, dirty, draft ------------------------------------- */

  const mutate = useCallback((fn, { snapshot = true } = {}) => {
    const prev = modelRef.current
    if (!prev) return
    const next = typeof fn === 'function' ? fn(prev) : fn
    if (!next || next === prev) return
    if (snapshot) {
      hist.current.past.push(JSON.stringify(prev))
      if (hist.current.past.length > 50) hist.current.past.shift()
      hist.current.future = []
    }
    modelRef.current = next
    setModel(next)
    if (snapshot) setHistTick((v) => v + 1)
  }, [])

  const undo = useCallback(() => {
    const s = hist.current.past.pop()
    if (!s) return
    hist.current.future.push(JSON.stringify(modelRef.current))
    const m = JSON.parse(s)
    modelRef.current = m; setModel(m); setHistTick((v) => v + 1)
  }, [])
  const redo = useCallback(() => {
    const s = hist.current.future.pop()
    if (!s) return
    hist.current.past.push(JSON.stringify(modelRef.current))
    const m = JSON.parse(s)
    modelRef.current = m; setModel(m); setHistTick((v) => v + 1)
  }, [])

  const dirty = useMemo(() => !!model && stripVolatile(model) !== savedRef.current, [model])

  // draft autosave — 400ms, matching the debounces already in App.jsx
  useEffect(() => {
    if (!model || !dirty) return
    const t = setTimeout(() => {
      try { localStorage.setItem(`ezc_kg_draft_${model.id}`, JSON.stringify(model)) } catch { /* quota — fine */ }
    }, 400)
    return () => clearTimeout(t)
  }, [model, dirty])

  // live validation gives the builder its "≈ 11 429 edges" readout and paints bad anchors red
  useEffect(() => {
    if (!model) return
    const my = ++seq.current
    const t = setTimeout(() => {
      postJSON(`${api}/api/kg/model/${encodeURIComponent(model.id)}/validate`, model)
        .then((d) => { if (my === seq.current) setValidation(d.validation || null) })
        .catch(() => { if (my === seq.current) setValidation(null) })
    }, 500)
    return () => clearTimeout(t)
  }, [api, model])

  /* ---- actions ---------------------------------------------------------- */

  const save = useCallback(async () => {
    const m = modelRef.current
    if (!m || saving) return
    setSaveResult(null); setErr('')
    try {
      setSaving('saving')
      // save and materialize are two calls on purpose: a build failure must never cost the canvas
      const saved = await postJSON(`${api}/api/kg/model`, m)
      const next = saved.model || m
      modelRef.current = next; setModel(next)
      setSaving('building')
      const built = await postJSON(`${api}/api/kg/model/${encodeURIComponent(next.id)}/materialize`, next)
      const done = built.model || { ...next, materialized: true, stats: built.stats }
      modelRef.current = done; setModel(done)
      savedRef.current = stripVolatile(done)
      setSaveResult({ stats: built.stats })
      hist.current.future = []
      try { localStorage.removeItem(`ezc_kg_draft_${done.id}`) } catch { /* noop */ }
      setDraft(null)
      loadModels().catch(() => { })
      onRefreshHealth()
    } catch (e) {
      const v = e.payload?.error?.detail
      if (v?.errors) setValidation(v)
      setSaveResult({ error: e.message || 'save failed' })
    } finally {
      setSaving(null)
    }
  }, [api, saving, loadModels, onRefreshHealth])

  const switchModel = useCallback(async (id) => {
    if (!id || id === model?.id) return
    try {
      await loadModel(id)
      postJSON(`${api}/api/kg/active`, { model_id: id }).catch(() => { })
    } catch (e) { setErr(e.message || 'could not open that ontology') }
  }, [api, model, loadModel])

  const duplicate = useCallback(async () => {
    const m = modelRef.current
    if (!m) return
    try {
      const d = await postJSON(`${api}/api/kg/model/${encodeURIComponent(m.id)}/duplicate`, {})
      await loadModels()
      if (d.model?.id) await loadModel(d.model.id)
    } catch (e) { setErr(e.message || 'could not fork the ontology') }
  }, [api, loadModel, loadModels])

  // "reset" restores the built-in schema onto the current model id — the built-in is the reference
  // ontology, so this is the escape hatch from any experiment that went sideways
  const reset = useCallback(async () => {
    const m = modelRef.current
    if (!m) return
    try {
      const d = await getJSON(`${api}/api/kg/model/default`)
      const def = d.model
      mutate((cur) => ({
        ...cur,
        entity_types: JSON.parse(JSON.stringify(def.entity_types || [])),
        relation_types: JSON.parse(JSON.stringify(def.relation_types || [])),
        layout: JSON.parse(JSON.stringify(def.layout || {})),
        description: def.description || cur.description,
      }))
    } catch (e) { setErr(e.message || 'could not read the built-in ontology') }
  }, [api, mutate])

  const rename = useCallback((name) => {
    const n = (name || '').trim()
    if (!n) return
    mutate((m) => ({ ...m, name: n.slice(0, 80) }))
  }, [mutate])

  const restoreDraft = useCallback(() => {
    if (!draft) return
    mutate(() => draft)
    setDraft(null)
  }, [draft, mutate])
  const discardDraft = useCallback(() => {
    const id = modelRef.current?.id
    if (id) { try { localStorage.removeItem(`ezc_kg_draft_${id}`) } catch { /* noop */ } }
    setDraft(null)
  }, [])

  /* ---- '?' cheatsheet, everywhere in the tab ----------------------------- */

  useEffect(() => {
    const onKey = (e) => {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return
      if (e.key === '?') { e.preventDefault(); setKeys((v) => !v) }
      else if (e.key === 'Escape' && keys) setKeys(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keys])

  /* ---- render ----------------------------------------------------------- */

  const mat = models.find((m) => m.id === model?.id)
  const canUndo = histTick >= 0 && hist.current.past.length > 0
  const canRedo = histTick >= 0 && hist.current.future.length > 0

  return (
    <div className="kg">
      <div className="kg-subtabs">
        {SUBTABS.map(([id, ic, label]) => (
          <button key={id} className={`kg-subtab${sub === id ? ' on' : ''}`} onClick={() => setSub(id)}>
            <span className="kg-subtab-ic">{ic}</span>{label}
          </button>
        ))}
        <span className="kg-note">
          {model
            ? <>
              <b>{model.name}</b> · {model.entity_types?.length || 0} types · {model.relation_types?.length || 0} relationships
              {model.materialized || mat?.materialized
                ? ` · materialized${model.stats?.nodes ? ` (${model.stats.nodes.toLocaleString()} nodes / ${model.stats.edges.toLocaleString()} edges)` : ''}`
                : ' · not materialized yet'}
              {mat?.stale ? ` · stale — ${mat.stale_reason}` : ''}
            </>
            : err ? <span>{err}</span> : 'loading the ontology…'}
        </span>
      </div>

      {err && !model && (
        <div className="kg-panel">
          <div className="kgr-err">
            The Knowledge Graph runs entirely in Python — check the sidecar on :8009. Vespa is not involved.
            <button className="kgb-btn ghost" onClick={() => window.location.reload()}>retry</button>
          </div>
        </div>
      )}

      {model && sub === 'builder' && (
        <Builder
          api={api} model={model} models={models} palette={palette} validation={validation}
          dirty={dirty} saving={saving} saveResult={saveResult}
          mutate={mutate} onSave={save} onSwitch={switchModel} onDuplicate={duplicate}
          onReset={reset} onRename={rename} onJumpToQuery={() => setSub('query')}
          undo={undo} redo={redo} canUndo={canUndo} canRedo={canRedo}
          full={full} setFull={setFull}
          draft={draft} onRestoreDraft={restoreDraft} onDiscardDraft={discardDraft}
        />
      )}

      {model && sub === 'explorer' && (
        <ExplorerPane ref={explorerRef} api={api} model={model} health={health}
          onNeedMaterialize={() => setSub('builder')} />
      )}

      {model && sub === 'query' && (
        <div className="kg-panel">
          <QueryBar
            model={model} api={api}
            onNeedMaterialize={() => setSub('builder')}
            onResult={() => { }}
          />
        </div>
      )}

      {keys && <ShortcutsOverlay onClose={() => setKeys(false)} />}
    </div>
  )
}
