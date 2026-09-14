# DESIGN.md — grow-my-money dashboard

Design system for the read-only web dashboard (`src/dashboard_web/index.html`,
served by `src/dashboard.py`). Scope is the dashboard only — the bot itself
has no other UI. This document is the source of truth for the visual
language; the CSS tokens it describes live in exactly one place
(`index.html`'s `<style>` block) and the showcase page renders them live so
this document and the code cannot silently drift apart.

## Direction: "Terminal Ledger"

**The dashboard is the one surface where a human looks a live-money
autonomous trading bot in the eye.** The audience is a single technical
operator — the project's own owner — checking bot health, P&L vs. a
buy-and-hold benchmark, open positions, and the safety-cap/kill-switch state,
often while real money is on the line (`mode: live`). This is not a product
with a funnel or a brand to charm anyone with; it's an instrument panel that
has to be legible at a glance, dense enough to hold everything relevant on
one screen, and calm under a red "ENGAGED"/"live" state rather than
decorative about it. The project's own `LESSONS_LEARNED.md` is unusually
blunt about strategy performance ("no demonstrated edge", "-20.2% vs -2.0%
buy-hold") — the dashboard should read with the same directness.

"Terminal Ledger" answers that brief: a dark, monospace-forward, terse
financial-terminal register (Bloomberg/trading-desk lineage), high
information density, a single confident blue accent for the bot's own data
series, and unambiguous green/red/amber semantics for money and risk. It is
the mood-board direction that most closely resembled a **real trading
terminal** rather than a dashboard-shaped app — which is exactly the register
this tool needs.

### Mood-board rationale (autonomous run — no user review)

This was a batch/autonomous pass; three directions were generated via
Ideogram (`mcp__ideogram__generate_image`) and evaluated against the brief
above rather than shown to the user for a live pick:

1. **"Terminal Ledger" (chosen)** — near-black charcoal, monospace-precision
   grid, blue/green/red on dark, chunky data cards, terse Bloomberg-terminal
   mood. **Why it won:** the register matches the actual use case exactly —
   a technical operator reading dense tabular/numeric data under real
   financial stakes. Monospace numerals and a dark ground are native to
   that context (every real trading terminal looks like this for a reason:
   tabular alignment, low eye strain during long monitoring sessions, and a
   register that reads as "instrument" rather than "app"). Nothing about it
   fights the page's actual content, which is almost entirely numbers and
   short status tags.
2. **"Quiet Instrument" (rejected)** — warm off-white paper, a
   serif-meets-grotesk display face, hairline financial-audit-report
   styling. Elegant and calm, but the serif display face and paper warmth
   read as *editorial* (a report you read once) rather than *operational*
   (a panel you glance at repeatedly, including at the moment a kill switch
   might need pulling). Too soft for a live-money control surface — a
   warning pill or a "mode: live" banner would fight the gentleness of the
   rest of the page instead of reading as urgent.
3. **"Vault Signal" (rejected)** — graphite/navy control-room aesthetic,
   riveted metal panel edges, an analog gauge, industrial stencil display
   type. Closest in spirit to "safety-critical" but the skeuomorphism
   (rivets, bevels, a physical gauge) adds visual weight that would compete
   with dense real tables and KPI grids rather than get out of their way.
   A reactor-control-panel look is memorable for a hero image; it becomes
   noisy once tiled across five KPI cards and two data tables on one
   screen.

Generated mood-board images (not committed to the repo — Ideogram-hosted,
referenced here for provenance):
- Terminal Ledger: `https://ideogram.ai/g/3KUMq9C-Tx-ae2gG8crkDA/0`
- Quiet Instrument: `https://ideogram.ai/g/aB5bOTiFTkmkg9HaKzkXCA/0`
- Vault Signal: `https://ideogram.ai/g/2MysmlM-QB-uoeBGOUmxtA/0`

### Key moments this was designed around

1. **The first glance** — operator opens the dashboard to check on the bot.
   The hero KPI ("bot vs. buy & hold") and the mode/kill/halt pills must be
   readable in under a second, before reading anything else.
2. **`mode: live` with real money at risk** — the mode pill inverts to a
   solid red chip; nothing else on the page should need to change register
   to communicate "this matters now" (see Accessibility notes on this — it
   is deliberately not the only signal).
3. **A tripped kill switch or halted portfolio** — `pill.bad` states must
   out-rank everything else on the page visually without needing motion,
   since the dashboard polls on a fixed interval rather than pushing
   updates.
4. **Reading the activity ledger after a strategy review** — dense
   trade/intent rows (buy/sell/reject/pending) need to be scannable in bulk;
   this is the "ledger" half of the name.
5. **Manual refresh** — the one interactive control on the page (added as
   part of this pass; see Components) is the dashboard's single "signature"
   action and had to feel immediate (fast easing, disables mid-fetch).

## Color

All colors are CSS custom properties on `:root`, defined for the dark
identity (default/primary) and overridden for light under
`@media (prefers-color-scheme: light)` and `:root[data-theme="light"]` (a
manual override hook the JS already exposed before this pass — no toggle UI
exists yet, but the hook works if one is added later). Dark is the primary
identity: a terminal is a dark surface by nature, and the light variant
stays in the same cooler-neutral instrument-panel family rather than
becoming a softer, different-feeling app.

| Token | Dark value | Light value | Role |
|---|---|---|---|
| `--page` | `#0a0c0f` | `#f4f5f3` | Page background |
| `--surface-1` | `#12151a` | `#ffffff` | Card / panel surface |
| `--surface-2` | `#171b21` | `#fbfbfa` | Raised surface, row hover, tooltip bg |
| `--border` | `rgba(255,255,255,0.09)` | `rgba(10,12,15,0.11)` | Hairline borders |
| `--chip-bg` | `rgba(255,255,255,0.06)` | `rgba(10,12,15,0.05)` | Pill / tag fill |
| `--text-1` | `#eef1f4` | `#0d1116` | Primary text |
| `--text-2` | `#a7b0ba` | `#454d56` | Secondary text (footers, legends) |
| `--muted` | `#707881` | `#7b828b` | Labels, muted captions |
| `--grid` | `#1f242b` | `#e4e6e3` | Equity chart gridlines |
| `--axis` | `#2a313a` | `#cdd1cb` | Chart axis / bankroll reference line |
| `--series-1` | `#3d8bef` | `#1f66c4` | Bot equity line, primary button, links |
| `--benchmark` | `#707881` | `#8b929b` | Buy & hold reference line (deliberately muted — it's a reference, not the hero) |
| `--good` | `#22b866` | `#16874a` | Buy tag fill, positive semantics |
| `--good-text` | `#3ecb79` | `#0f6b3a` | Positive numeric text (higher-contrast than `--good` fill) |
| `--critical` | `#e5484d` | `#c8353a` | Sell / reject / danger / kill-engaged |
| `--warning` | `#d98f2b` | `#a8660a` | Caution (near trade cap, stale price source) |

**Contrast**: `--text-1` on `--page`/`--surface-1` and `--text-2` on the same
exceed WCAG AA (4.5:1) in both themes — verified visually via the showcase
page's rendered swatches. `--good-text`/`--critical` on `--surface-1` are the
two color pairs carrying meaning (P&L sign, tag state) and were chosen with
extra contrast margin over `--good`/plain red specifically because they
carry information, not just decoration — see Accessibility notes below for
why sign is never color-only.

## Type

No custom `@font-face` is loaded. This is a deliberate deviation from the
skill's usual "self-host a distinctive display face" guidance: the
dashboard's own file header states its architecture is "fully
self-contained: inline CSS/JS/SVG, no external `<script src>`/`<link>`" —
adding a webfont would mean embedding font bytes as a `data:` URI inside a
page that's re-served whole on every visit, which fights that constraint for
a monitoring tool that should stay light. Instead, **the monospace system
stack becomes the display face** — genuinely distinctive in this context
(most dashboards use a humanist sans; a terminal-style dashboard in
monospace is the point, not a compromise) while staying zero-byte.

- **`--font-mono`** — `ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", "Roboto Mono", Consolas, monospace`. Used for: the page title, all KPI values, all section headers (`h2`), pills, tags, table headers, numeric table cells (`.num`), the equity chart's axis labels, the model panel's values, and the manual-refresh button. This is the identity — numbers and status are always monospace.
- **`--font-sans`** — `-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, Roboto, sans-serif`. Used only for prose that needs maximum legibility at small size and isn't tabular data: table body text in the "Reason" column, KPI footnotes, the equity-chart caption, model-panel labels (`.model-row .k`).

### Type scale

| Step | Spec | Where |
|---|---|---|
| Display | `700 32px/1.1 var(--font-mono)`, `letter-spacing:-0.01em` | Hero KPI value (`.kpi.hero .value`) |
| H1 | `700 19px/1.2 var(--font-mono)`, `letter-spacing:-0.01em` | Page title ("grow-my-money") |
| KPI value | `700 26px/1.15 var(--font-mono)`, tabular numerals | Standard KPI card values |
| KPI value / small | `700 21px/1.2 var(--font-mono)` | Secondary KPI values (`.value.small`) |
| H2 | `700 12px/1.4 var(--font-mono)`, uppercase, `letter-spacing:0.08em` | Section labels ("Equity curve", etc.) |
| Body | `400 13px/1.5 var(--font-sans)` | Table prose, reason text |
| Label | `600 11px/1.4 var(--font-mono)`, uppercase, `letter-spacing:0.04em` | Table column headers |
| Pill/tag | `600 12px/1.6 var(--font-mono)` (pills), `600 11px/1.6` (tags) | Status pills, activity tags |
| Caption | `400 12.5px/1.4 var(--font-sans)` | Footnotes, "updated Xs ago" |

## Spacing, radius, shadow, motion

All literal values; see the showcase page's Spacing/Radius/Shadow/Motion
sections for a live render of each.

**Spacing** (`--sp-1` … `--sp-8`): `4px, 8px, 12px, 16px, 20px, 24px, 32px, 48px`. Used for card padding, grid gaps, and section margins — never a bespoke pixel value outside this scale in new CSS.

**Radius**: `--r-sm: 6px` (buttons, tags, tooltips), `--r-md: 10px` (reserved for future smaller panels), `--r-lg: 14px` (cards), `--r-pill: 999px` (status pills).

**Shadow**: `--shadow` (cards: `0 1px 2px rgba(0,0,0,.5), 0 4px 16px rgba(0,0,0,.35)` dark / softer black-on-white in light) and `--shadow-sm` (tighter, currently unused by a component but defined for a future smaller floating element).

**Motion**: `--ease-standard: cubic-bezier(0.2, 0.8, 0.2, 1)`; `--dur-fast: 120ms` (button/tag hover, pill state change), `--dur-base: 200ms` (crosshair tooltip fade, row hover), `--dur-slow: 320ms` (reserved — no component uses it yet, kept for a future larger reveal so the scale doesn't need inventing later). All transitions are killed under `prefers-reduced-motion: reduce` via a single global rule at the bottom of the stylesheet.

## Components

Every component below is real markup/CSS in `index.html`, not a mockup — the
showcase page renders each one from the live stylesheet.

- **Button** (`.btn`, `.btn-primary`) — new in this pass. The dashboard had
  zero interactive controls before (pure auto-polling); a manual "Refresh"
  button was added as the system's one signature control, wired to
  immediately re-run the same `tick()` fetch/redraw the poll timer uses and
  reset the interval. States: default (`.btn`, neutral surface), primary
  (`.btn-primary`, solid `--series-1` fill — used for the one action that
  matters, refresh), hover (border/color shifts to accent), active (1px
  press), disabled (0.5 opacity, no pointer feedback) — disabled while a
  fetch is in flight so double-clicks can't race the poll timer.
- **Pill** (`.pill` + `.good`/`.bad`/`.warn`/`.mode-live`) — status chips for
  mode/kill/halt/trade-cap. `.mode-live` is the only pill with a filled
  (not outlined) treatment — solid red — because "real money is trading
  right now" is the one state that should look different in kind, not just
  in color, from every other pill on the page.
- **Tag** (`.tag` + `.buy`/`.sell`/`.filled`/`.pending`/`.reject`/`.blocked`) —
  inline activity-row status. Buy/sell use `--good`/`--critical` at 16% fill
  (readable as a tint, not a solid block, since these appear many-per-row in
  a dense table).
- **Card / KPI card** (`.card`, `.kpi`, `.kpi.hero`) — the base surface unit.
  `.kpi.hero` spans 2 grid columns and uses the Display type step — reserved
  for exactly one metric (bot vs. buy-and-hold), since that's the number the
  operator actually opens the dashboard to see.
- **Table** (`th`/`td`, `.num`, `.src-badge`) — right-aligned numeric columns
  in monospace with `font-variant-numeric: tabular-nums` for column
  alignment; row hover (`--surface-2`) added in this pass for scannability
  in the activity ledger, which can run long.
- **Model panel** (`.model-row`) — label/value pairs, label in sans (`.k`),
  value in mono (`.v`).
- **Equity chart** (`svg.equity`, `.legend`, `.crosshair-tip`) — unchanged
  structurally; axis labels now render in `--font-mono` for consistency with
  the rest of the numeric system.
- **Error / empty states** (`#err`, `.empty`) — `#err` uses `--critical` in
  monospace (it's effectively a system message); `.empty` uses `--muted` in
  the default body face, kept deliberately quiet since "no data yet" isn't
  an error.

## Backgrounds & texture

None generated. "Terminal Ledger" earns its identity from type, color, and
density rather than a decorative background layer — a scanline/grid texture
was in the original mood-board prompt but was deliberately not carried into
the real component (the equity chart's own gridlines already supply that
texture where it's functional; a decorative overlay on top of dense tables
would reduce legibility, which is the one thing this page cannot trade
away).

## Accessibility notes

- **Never color-only for sign/state**: every positive/negative number pairs
  a `+`/`-` prefix or explicit word (`pos`/`neg` classes) with color; every
  pill pairs its color with a text label (`kill: ENGAGED`, not just a red
  dot); tags spell out the state (`reject`, `pending`) rather than relying
  on color alone. This was already true before this pass and was preserved.
- **Contrast**: `--good-text`/`--critical` were chosen distinct from
  `--good`/`--critical` fills specifically for higher contrast against
  `--surface-1` where they carry text, not just a chip background.
- **Focus states**: `.btn:focus-visible` gets a visible 2px accent outline
  (new in this pass, since it's the first focusable interactive element the
  page has had).
- **`prefers-reduced-motion`**: a single global rule collapses all
  transition/animation durations to near-zero when set; no component
  relies on motion to convey information (the crosshair tooltip's fade is
  cosmetic, not load-bearing).
- **Theme**: dark is default; a light variant is fully defined (not just
  inverted) for `prefers-color-scheme: light`, and a manual `data-theme`
  override on `<html>` is supported (hook only — no UI toggle exists yet).

## Asset inventory

| Path | Role |
|---|---|
| `src/dashboard_web/index.html` | Dashboard page — source of truth for every design token (inline `<style>`) |
| `src/dashboard_web/showcase.html` | Design-system showcase — fetches `/` at runtime and renders live tokens/components; served at `GET /design-system` |
| `src/dashboard_web/favicon.ico` | Existing favicon (blue upward chart-line mark, 16/32/48/64px) — **not regenerated**; already fits the product (a "grow" chart-line icon for a money-growing bot) and needed no uplift |
| `src/dashboard.py` | Added `GET /design-system` route serving the showcase page |
| `DESIGN.md` | This document |

No new images/fonts/audio were generated for this pass — see Type and
Backgrounds & texture above for why (architectural constraint: fully
self-contained, no external resources, minimal page weight for a polling
monitoring tool).

## Viewing the showcase

Run the dashboard (`.venv/bin/python -m src.dashboard`, or via the app
container) and open `http://<host>:8420/design-system`. It fetches `/`
client-side to pull the live `<style>` block, so it must be served by the
same app — opening the file directly (`file://`) shows an inline error
explaining why.

## Changelog

- **2026-09-13** — Initial design system: "Terminal Ledger" direction chosen
  autonomously from 3 Ideogram-generated mood boards (batch run, no user
  review — see rationale above). Retokenized `index.html`'s existing
  light/dark CSS custom properties in place (same variable names/behavior,
  new values + several new tokens for spacing/radius/shadow/motion scales);
  switched numeric/data type to a monospace system stack; added the
  dashboard's first interactive control (`.btn`/`.btn-primary` manual
  refresh button); added row-hover on tables; added a global
  `prefers-reduced-motion` rule. Added `src/dashboard_web/showcase.html` and
  a `GET /design-system` route in `src/dashboard.py`. No test changes
  required — the existing suite (`tests/test_dashboard.py` and the rest of
  the 150-test suite) asserts only the data layer, not DOM/CSS, and all 150
  tests pass unchanged after this pass.
