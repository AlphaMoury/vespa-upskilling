# ezCater × Vespa — positioning notes (for the TTO presentation)

Research-backed talking points. Sources are ezCater's own job board, engineering blog, press,
and help center (cited below). Marked **VERIFIED** vs *inferred*.

## The headline (lead with this)
**ezCater's search platform already runs on Vespa.** **VERIFIED** from their *Staff Software
Engineer, Search Platform* posting:
> "Own the Search Engine architecture: **technology direction (Vespa-based)**, document schema,
> query serving, and operational model… Temporal-orchestrated indexing… Kafka event streaming."
> (job-boards.greenhouse.io/ezcaterinc/jobs/5160456007)

So the pitch is **not** "replace your keyword search with Vespa." It's **"I'm fluent in your engine,
and here's the intent-driven discovery your roadmap is heading toward — native in Vespa."**

## Their stack (so you sound informed) — VERIFIED
- **Search:** Vespa · Go (query layer) · Temporal (indexing) · Kafka (real-time indexing) · geospatial filtering
- **Backend:** Ruby on Rails (+ Go) · GraphQL · PostgreSQL · AWS · Kubernetes/Docker
- **Data/ML:** Snowflake · dbt · Fivetran · Airflow · **SageMaker + MLflow** · Monte Carlo · Hightouch ·
  a **dedicated ML team** · multi-armed-bandit experimentation
- **Scale:** **125,000+** restaurants · AOV ~**$420** · ~**25** people/order · 87% of orders ≤ $150 · ~75% booked < 12h before event
- **Org signal:** hiring a **Staff** owner for the Search Platform to "drive relevance and ranking strategy" →
  search is **mid-build-out, a priority**, not solved.

## What ezCater discovery looks like today — VERIFIED
Location-first entry (address + date + time + headcount) → a **keyword search bar** ("your team's favorite
Thai spot or a specific type of sandwich") → **facet filters** (cuisine; dietary: veg/vegan/GF/dairy-free/
halal/kosher; budget; packaging; min rating; delivery fee; "Reliability Rockstars") → **proprietary ranking**
that blends relevance + **paid placement** (Preferred Partner 2–20%, ezRewards) + reliability → caterer cards.
They shipped **Smart Ordering** (AI for *how much/what combos* to order, Sep 2024) and are rolling out
**Agentforce** (Salesforce natural-language discovery). *Discovery itself is still keyword + rigid filters —
that's the wedge.*

## The opportunity (what Vespa fluency adds)
| Gap in keyword+filter catering search | Vespa capability |
|---|---|
| Intent / NL ("impressive client dinner", "travels well") never matches catalog strings | **Hybrid retrieval** (BM25 + vectors, one query, fused) |
| Dish that's "plant-based" but not tagged `vegan`; naturally-GF dish untagged | **Embeddings** (meaning, not tags) |
| One-dimensional sort (rating *or* price *or* distance) | **ML / phased ranking**: relevance × rating × reliability × margin × delivery-fit |
| Dumb prefix typeahead ("med" → "medium") | **Vespa prefix + semantic suggest** |
| Stale price/availability | **Real-time partial updates** |
| Generic results for everyone | **Per-user ranking signals** (reorders, office dietary profile) |
| Caterer-level only | **Multi-level indexing** (dish + caterer) |

**The killer real-world case:** the **mixed-dietary order** (vegan AND gluten-free AND omnivore in one
order). Rigid filters break structurally (intersection vs. menu-span); hybrid + per-item relevance handles it.

## Demo script (validated on the live app)
Two columns (keyword vs. AI hybrid), same query. Reframe: *"this is the relevance engineering your Search
Platform team is doing in Vespa."*
1. **"impressive client dinner"** — keyword: nothing → hybrid: sushi platter, satay, paneer tikka. *Intent has no keyword.*
2. **"spicy food that is also gluten free"** — keyword: Mac & Cheese → hybrid: **Mapo Tofu**. *Meaning + nuance.*
3. **"healthy lunch vegans and meat-eaters will both like"** → hybrid: Vegan Buddha Bowl / Mediterranean. *Both/and.*
4. **"office breakfast that travels well"** → hybrid: cinnamon rolls, frittata, parfaits. *Logistics intent.*
5. **Quora tab "how do I become a better programmer"** → cleanest pure keyword-vs-meaning illustration.
6. **Scale:** the same engine has **522,931** Quora docs + **171,332** COVID papers indexed locally — real-time, 5–14 ms.

## Business value for ezCater
Higher search→order conversion (fewer zero-results / abandonments); larger, complete orders (the mixed-dietary
order succeeds); better dietary compliance & trust; long-tail supplier discovery on meaning (not just well-tagged
chains); **complements Smart Ordering** (which optimizes *how much* — Vespa fixes *finding what you mean* upstream).

**One-liner:** *"ezCater already knows how much you should order. Vespa fluency makes sure you can find what you
actually mean — and that's where the conversion lives."*

### Sources
- Search Platform role (Vespa): job-boards.greenhouse.io/ezcaterinc/jobs/5160456007
- Data stack: builtinboston.com/job/senior-data-engineer-remote/3366631 · stackshare.io/companies/ezcater · engineering.ezcater.com
- Smart Ordering: ezcater.com/company/press-release/ezcater-unveils-smart-ordering… · Agentforce: salesforce.com/customer-stories/ezcater
- Discovery UX: ezcater.com/lunchrush/office/how-to-order-catering-with-ezcater · /how-ezcater-works · catering.ezcater.com help center
