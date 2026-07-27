# Broiler Base Mate™ — Off-Replit Migration + Auto-Sync + AI Docket Scanner + History

## Original Problem Statement
> https://silo-sync-excel.replit.app/feed-program/ I need off replit
> "do what best as i need to auto sync to the program"
> "do them all"
> "[uploaded BPL Adelaide + Ingham's dockets] this should fill the end of batch and should be in history on the app"
>
> GitHub: https://github.com/coverdalej72-lab/Silo-Sync-Excel

## Architecture
- **Source monorepo**: `/app/silo/` (pnpm workspace cloned from GitHub).
- **Frontend** (`/app/frontend` → port 3000): wrapper script that auto-installs pnpm if missing, then runs the feed-program Vite dev server with `BASE_PATH=/`.
- **Backend** (`/app/backend` → port 8001): FastAPI + MongoDB + Gemini 2.5 Flash via emergentintegrations.
- **Field Reader** at `/reader`: 4-tab mobile UI served by FastAPI.
- **Vite proxy**: `/api`, `/reader`, `/reader-assets` → port 8001.

## Field Reader — 4 Tabs
### 📋 Readings (auto-sync)
- Pending sheds at the top, partial in the middle, DONE at the bottom.
- Per-silo "Last: 22.3t (Wed, 3 Jun) [Use ↑]" — one-tap previous-reading recall.
- Posts to `/api/readings/batch` → Feed Program polls `/api/readings/today` every 2 min.

### 📷 Scan (AI auto-detect)
- Single "Tap to photograph" button — no vendor toggle.
- Auto-detects supplier from docket header (Ingham's / Baiada / BPL Adelaide / etc).
- **Gemini 2.5 Flash** extracts: supplier, feedType, productCode, amount (auto-converts Kg→tonnes), deliveryDate (DD/MM/YY → YYYY-MM-DD), orderNumber/ticketNo, customerName, siteCode, deliveryInstructions, truckRego, outloadingBin.
- One-tap **"Save to History & End-of-Batch"** — creates a delivery with a base64 thumbnail.

### 🚚 Add (manual)
- Form with shed group, silo, feed type, amount, docket #, truck rego, notes.

### 📜 History
- Chronological list of every delivery (manual + scanned).
- Each item shows: supplier pill, feed type (original product name) + code, date, docket #, truck rego, customer, delivery instructions, outloading bin, amount.
- Thumbnail click → lightbox.
- Delete also removes from End-of-Batch.

## End-of-Batch Auto-Fill
- On Feed Program load, EndOfBatchContent polls `/api/deliveries`.
- Backend **normalises feed type** (e.g. "Broiler Grower" → "Grower", "Gourmet Broiler Grower" → "Grower") so the deliveries land in the correct column (Starter/Grower/Finisher/Withdrawal) — `feedTypeOriginal` is preserved separately for display.
- Each delivery becomes a row: DATE (DD/MM/YYYY) · DOCKET # (from notes/`docketNumber`) · KG (auto t→kg conversion).
- Idempotent via `eob-synced-delivery-ids` localStorage set.

## Verified Real Dockets ✅
- **BPL Adelaide** Ticket 55104, S110 Broiler Grower, 43,740 Kg → 43.74t, 02/04/2026, Double B Farm, Truck XS07IQ.
- **Ingham's** Order 125865, F116 Gourmet Broiler Grower, 28.16t, 18/05/2026, GP Farms, Truck SB84EF, Instructions "5 B 10, 6 B 5, 7 B 13", Bin BN8103.
- End-of-Batch shows DELIVERED 71,900 kg = 28.16t + 43.74t, both rows in Grower section with correct dates + docket #s.

## Backend API
`/api/shed-groups`, `/api/silos*`, `/api/readings/today`, `/api/readings/previous?siloId=`, `/api/readings/batch`, `/api/readings`, `/api/deliveries` (with rich docket fields + imageThumb + feedTypeOriginal), `/api/scan-docket/auto` (recommended), `/api/scan-docket/ingham`, `/api/scan-docket/baiada`, `/api/bootstrap`, `/api/batch/version`, `/api/batch/reset`, `/api/onedrive/status`.

## Integrations
- **Emergent Universal LLM Key** → Gemini 2.5 Flash for OCR + structured extraction.
- **Stripe** — 14 pricing/sponsorship tiers wired via `/api/checkout` (currently `sk_test_` keys; awaiting user live keys + Price IDs).

## Landing Page (`/landing`)
- Hero with AI aerial broiler farm image, charities strip, Ops Manager bundle builder, stats counter, **testimonials (3)**, **FAQ (8 accordions)**, **Book-a-Demo form → POST `/api/demo-request`**, trust badges, Back-the-Build sponsorships, How-It-Works modal walk-through, **mobile sticky CTA bar** (≤768px).
- Demo requests stored in MongoDB `demo_requests` collection; readable via GET `/api/demo-request`.

## Changelog
- **2026-02-16 (Whole-Farm Flock Forecast)**:
  - New `/app/silo/artifacts/feed-program/src/lib/breedStandards.ts` — daily-resolution Ross 308 FF + Cobb 500 performance objectives (0–56 days) with `stdAt()` linear interpolation helper.
  - Added "🌾 Whole-Farm Forecast" section at the top of the Flock Forecast page in `App.tsx`. Aggregates weigh-ins + catch averages across all sheds (weighted by birds), picks dominant breed standard, projects forward to user-selected pickup age (35 / 42 / 49 d).
  - 8 rollup KPI cards: birds live, current age, projected pickup weight, total live weight, projected FCR, mortality %, feed-to-pickup, days-to-pickup.
  - Two Recharts graphs: Whole-Farm Growth Curve (actual + breed std + forecast tail) and FCR Trajectory (breed std + current actual reference line).
  - Plain-english insight strip explains tracking status (above/below/on standard) with predicted pickup weight and remaining feed.
- **2026-06-12 (fork-resume)**: Fixed broken FAQ section (raw JS template-literal was leaking into HTML), wired Book-a-Demo form to `/api/demo-request` with toast feedback, added mobile-only sticky "Start free trial" CTA bar.
- **2026-06-12 (multi-farm + ops + email)**:
  - Added `farmId` scoping to readings/deliveries/photos/shed_groups/silos/farm_config — backward-compatible (untagged data treated as `farmId="default"`).
  - New `farms` collection + `/api/farms` CRUD endpoints + `/api/farms/{slug}/invite`.
  - New `/ops-dashboard` page (multi-farm overview, create farm, send invite).
  - Reader (`/reader?farm=<slug>`) monkey-patches `fetch()` to auto-append `?farm=` to all `/api/*` calls.
  - Resend email integration (`/app/backend/email_service.py`) with graceful-degrade when `RESEND_API_KEY` is missing.
  - Demo-request form now emails admin (`appcovi2026@gmail.com`) when key is set; logs+skips when not.
  - Vite proxy updated to forward `/ops-dashboard` to FastAPI.
- **2026-06-13 (Stripe auto-onboarding + Feed Program farm switcher)**:
  - Landing page now opens a "Almost there" modal collecting buyer name + email before any plan/ops checkout.
  - Ops bundle checkout sends the full configured farms list + per-tier pricing (Σ tier_prices, not hardcoded).
  - On Stripe `paid` event (status poll or webhook), `_provision_purchase` auto-creates farms, seeds each with 10 shed-groups × 3 silos, and emails the buyer their reader URLs + ops-dashboard link. Idempotent — second call returns None.
  - Admin (`appcovi2026@gmail.com`) gets a sale-notification email on every paid checkout (when Resend key set).
  - Success page (`/landing/success`) renders the buyer's auto-provisioned farm cards with reader links and adapts primary CTA: single farm → reader, multi-farm → ops-dashboard.
  - Feed Program (React desktop) now mounts a floating farm-switcher header widget with a dropdown of all farms + "Ops →" shortcut; auto-hides when only the default farm exists. Same fetch monkey-patch trick auto-scopes every `/api/*` call by the selected farm (stored in localStorage).
- **2026-02-19 (Ops Dashboard Chat)**:
  - Floating chat FAB widget added to `/ops-dashboard` with unread-count badge polled every 30s via `/api/chat-unread`.
  - Two-tab chat panel: **Group** (broadcast across all farms) + **Per-farm** (dropdown selector → scoped to a single farm slug).
  - Messages persisted in MongoDB `chat_messages` collection; endpoints `GET/POST /api/chat/{scope}`, `GET /api/chat-unread` (all auth-gated via Emergent Google session).
  - Auto-poll every 10s while panel is open; mine vs. theirs styled bubbles; Ops role tag; light/dark theme aware.
- **2026-06-20 (Free dev provisioning + fast static frontend + Production Hardening pass)**:
  - Seeded 3 extra demo farms (`north-creek`, `southridge`, `eaglehawk`) for owner `appcovi2026@gmail.com` so he can fully test multi-farm flow without paying — `POST /api/farms` requires no Stripe.
  - `/app/frontend/start.js` now serves the prebuilt Vite bundle (`/app/silo/artifacts/feed-program/dist/public/`) by default; `DEV_MODE=1` env flag opts back into Vite dev. First-load time on a fresh browser dropped from ~60s → ~2s.
  - **New module `/app/backend/hardening.py`** wires:
    - Global `Exception` handler → returns clean JSON 500 + logs to `error_log` collection + emails admin (1h cooldown per signature).
    - `POST /api/error-report` for frontend JS errors (sendBeacon-friendly, throttled).
    - Nightly backup task: gzipped JSON dump of every collection at 02:00 UTC → `/app/backups/backup_YYYYMMDD_HHMMSS.json.gz`, 7-day retention, emails admin on success.
    - In-memory rate limiter middleware: per-IP token bucket on hot endpoints — `/api/error-report` (60/min), `/api/auth/exchange-session` (10/min), `/api/demo-request` (5/10min), `/api/partner-request` (5/10min), `/api/outreach/send` (30/min), `/api/scan-docket/auto` (30/min — LLM cost guard).
    - Admin-only `GET /api/admin/error-log`, `GET /api/admin/last-backup`, `POST /api/admin/backup-now`, `GET /api/admin/health` (single-call business pulse: error counts, last backup, farms, readings/chat 24h, paid orders, outreach pipeline).
  - **New page `/admin` (and `/admin/health`)** — branded health dashboard pulling `/api/admin/health` & `/api/admin/error-log`. Shows error counts, last backup age, farm count, today's readings/chat volume, gross revenue, outreach pipeline. One-click "Backup now" button.
  - **Stripe webhook + status-poll** `_provision_purchase` calls now catch all exceptions, log via `log_error`, and still return 200 to Stripe so retries don't pile up — owner gets alerted instead of losing the sale silently.
  - **Auth-guard.js** extended with: (a) `window.onerror`/`unhandledrejection` → POSTs JS errors to `/api/error-report` (de-duped, max 50 per page); (b) global `fetch()` wrapper that detects mid-session 401 on `/api/*` calls, shows a toast, and re-routes to the OAuth login (debounced 5s).
  - **Offline-safe mobile reader** (`/reader`): every failed POST/PUT/PATCH/DELETE to `/api/*` now persists to `localStorage` queue and replays automatically on `online` event / every 30s. Floating banner shows "📡 Offline — N queued" or "🔄 Syncing N…". Drain bypass via `x-bbm-skip-queue: 1` header avoids recursion.
  - `.gitignore` updated to exclude `backups/` from git.
- **2026-06-20 (Service Worker kill switch + Owner Magic Link)**:
  - **Cause of "worked yesterday, broken today" production bug**: VitePWA shipped a Workbox service worker that aggressively cached the React bundle + API responses. On any deploy, browsers stubbornly served the cached old bundle → app appeared broken. Fix:
    1. `vite.config.ts` — removed `VitePWA` plugin entirely. No more SW generation on future builds.
    2. `src/App.tsx` — stubbed `useRegisterSW` / `PwaUpdateBanner` to no-op so existing UI refs still work.
    3. `silo/artifacts/feed-program/dist/public/sw.js` — replaced workbox SW with a self-destruct script that unregisters itself and dumps every CacheStorage on `activate`. Any browser still polling the old SW URL gets nuked on the next update check.
    4. `feed-program/index.html` — added a page-load kill switch that calls `navigator.serviceWorker.getRegistrations().then(r => r.unregister())` + `caches.keys().then(k => caches.delete(k))` for any browser that bypasses the SW update check (e.g., first-visit-after-deploy).
    5. `server.py` — added FastAPI `GET /sw.js` route as fallback (same self-destruct payload) in case static server misses.
  - **`/api/auth/owner-magic?key=...&to=/...` endpoint** — one-click owner login that skips Google OAuth entirely. Requires env vars `OWNER_EMAIL` and `OWNER_MAGIC_KEY` (32-byte URL-safe token). Sets a 30-day session cookie + redirects to the requested page. Owner can bookmark a single URL and log in from any device/browser without OAuth.
  - **Session TTL bumped 7d → 30d** (`auth.py: SESSION_TTL_DAYS = 30`).
  - Production rollout requires: (a) Deploy, (b) set `OWNER_EMAIL` + `OWNER_MAGIC_KEY` env vars in production via Emergent platform.
- **2026-06-23 (Results tab math alignment with `result 121 (1).xlsx`)**:
  - **cFCR slope fixed `0.40` → `0.27`** in `App.tsx` (`loadBatchResultsXlsx`, both farm-summary and per-shed fallbacks). User's spreadsheet uses formula `cFCR = FCR − (AveWt − 2.45) × 0.27`. Verified: spreadsheet cached cFCR = 1.297; new code computes 1.297; old code with 0.40 would have computed 1.234.
  - **cFCR source cell corrected** — was reading `AN4` (= 1.78/cFCR efficiency ratio, value ~1.37) and displaying it as cFCR. Now reads `AL10` (the actual cFCR formula `=AL8-((AH8-2.45)*0.27)`).
  - **Added `aveWeight` fallback** `= totalWeight / totalOut` for cases where `AH8` formula cell reads as 0 in SheetJS.
  - **"Cage Age … days" tile relabeled to "Cage Rating"** — `AN5` (`=39.5/AH11`) is a unitless efficiency ratio, not a day count. Now displays as `1.309` (decimal) instead of `1.31 days`.
- **2026-06-23 (SEO Quick Wins)**:
  - Created `/sitemap.xml` (5 URLs on broilerbasemate.com.au) at `silo/artifacts/feed-program/public/sitemap.xml`.
  - Fixed `/robots.txt` — sitemap reference now correctly points to broilerbasemate.com.au (was farmbuddy.com.au).
  - Rewrote `/llms.txt` in proper markdown spec format (was being intercepted by SPA fallback → returning HTML).
  - Added homepage meta description, OG tags, Twitter cards to `landing.html` using correct domain.
  - Added `alt="Broiler Base Mate logo"` to landing header logo.
  - Replaced all `farmbuddy.com.au` references with `broilerbasemate.com.au` in feed-program `index.html` (canonical, OG, JSON-LD).
  - **DNS fix (user-side)**: user updated `www.broilerbasemate.com.au` CNAME from self-referential loop → apex domain.
- **2026-06-23 (Landing page content expansion + Results page tiles)**:
  - Added **"Why Australian broiler growers switched"** section to `landing.html` with 6 feature paragraphs (the exact maths, AI docket scanning, offline reader, Farm Buddy, end-of-batch, multi-farm Ops).
  - Added **8-question FAQ** with collapsible `<details>` cards (tech-savvy, processor contracts, offline, security, pricing, PWA install, Excel migration, who built it).
  - Added bottom CTA card. Text-to-HTML ratio raised from 0.09 → **0.38** (parsed from raw HTML, well above 10% threshold).
  - Added **Efficiency Rating (`AN6`)** and **Payment / bird (`AN8`)** tiles to Feed-Program Results page. New fields on `BatchSummary` interface with fallback math: `ER = (1.78/cFCR)×0.7 + (39.5/correctedAge)×0.3`, `Payment = ER × 0.005`. Live in screenshot at 1.162 / $0.0058.
- **2026-06-23 (Feed-Program state persists to MongoDB — placement-date data-loss bug fix)**:
  - **Root cause**: every spreadsheet edit (placement dates, bird counts, mortality, feed orders, silo readings, etc) was stored only in browser `localStorage`. When the computer shut down or the user opened a different browser, the cache was cleared and the app fell back to whatever defaults were baked in when the spreadsheet was first imported — so dates reverted to month-old values.
  - **Backend**: added `GET /api/feed-program/state` and `PUT /api/feed-program/state` on collection `feed_program_state`, keyed by `farmId`, storing the serialized `edits` blob + `sheetNames` + `updatedAt` ISO timestamp.
  - **Frontend (`App.tsx`)**:
    - Autosave (debounced 2 s) now writes to **both** localStorage and the backend.
    - Stamps `EDITS_SAVED_AT_KEY` localStorage timestamp on every save.
    - On load, hydration `useEffect` fetches backend state once and merges in if `backend.updatedAt > localStorage.savedAt` — i.e. backend wins on a fresh browser / cleared cache, local wins if the user has edited since the last successful backend round-trip (offline-safe).
  - **Verified live**: ~55 KB of edits (9 sheets) saved to MongoDB on first page-load smoke test.
- **2026-06-23 (Live sync — Ops Dashboard auto-refresh + Feed Program poll speed-up)**:
  - **Feed Program** silo-reading auto-sync interval reduced from **3 minutes → 30 seconds** (still skips when tab hidden).
  - **Ops Dashboard** previously never auto-refreshed silo readings. Now polls `loadAll()` every 30 s when tab is visible, fires immediately on visibility-change (tab-back), pauses when hidden.
  - Live indicator pill `[data-testid="live-status"]` in header: pulsing green dot + ticking "Live · 9s ago" label, switches to grey "Paused (tab hidden)" in background.
  - End-to-end latency field-manager Save → Ops Dashboard visible: **~30 s worst case**, ~2 s best case.
- **2026-06-23 (Reader: persistent sync indicator + proactive Farm Buddy feed alerts)**:
  - **Backend**: new `GET /api/farm-buddy/alerts?farm={slug}` endpoint. Lightweight (no LLM) — aggregates the most recent silo readings per shed group and flags any group whose total stored feed is `< 5 t` (`critical`) or `< 10 t` (`watch`). Returns `{riskLevel, alerts: [{shedGroupId, shedGroupName, totalT, level, message}], checkedAt}`.
  - **Reader UI** (`reader.html`):
    - **Persistent sync-status pill** in the header — pulsing dot + label that flips between `☁️ Synced to Feed Program` (green) / `🔄 Syncing N items…` (amber) / `📡 Offline` (red). Sub-text shows `last reading: 4m ago` so the field manager always sees how fresh the cloud copy is. Hooks `window.fetch` to detect successful `/api/readings/batch` POSTs and update the timestamp on the fly. Polls `/api/readings/today` every 30 s as a health probe.
    - **Farm Buddy alerts banner** between header and tabs. Hidden by default. Shows up the moment a shed group drops below 10 t with a clear `🚨 ORDER FEED NOW — Sheds 1 & 2 only has 3.0 t left` message. Auto-refreshes every 60 s and also fires immediately after any successful reading save (so the message updates the instant the manager finishes their walk-around).
  - **Verified**: inserted a `3.0 t` reading via curl → the orange Farm Buddy banner appeared with the correct message; cleaned up the test data afterward.
- **2026-06-24 (Summary tab: breed picker per shed)**:
  - Added a **Ross 308 / Cobb 500** dropdown next to each shed's bird-count field on the Summary page (12 dropdowns: 2 per shed group × 6 groups).
  - Backed by the same `FLOCK_BREEDS_KEY` localStorage entry the Flock Forecast tab uses, so the choice flows through to projected weight, target FCR comparisons and `getRoss308Standard()` / `BREED_STANDARDS.cobb500` curves automatically.
  - New helper component `BreedPickerRow` reused for both sheds in each card. `data-testid="breed-picker-<label>"` on each dropdown for QA.
- **2026-06-24 (EOB feed-delivery doubling bug fix)**:
  - **Root cause**: `EndOfBatchContent.syncDeliveries()` deduplicated only by the `eob-synced-delivery-ids` list in localStorage. If localStorage was cleared (cache wipe, switching browsers, restored backup, fresh device), every delivery looked "unsynced" and got re-appended to the next free row in the EOB sheet — doubling everything.
  - **Fix**: added a **content-fingerprint** dedupe layer. Before writing a delivery, build `${dateCol}|${dateStr}|${docket}|${kgRounded}` for every existing row in the sheet, then skip any incoming delivery whose fingerprint already matches. The localStorage `syncedIds` is still the fast first-pass; the fingerprint check is the safety net that survives any cache reset.
  - **One-time cleanup sweep** on mount (`cleanupDuplicateDeliveries`, gated by `eob-dedupe-cleanup-v1` localStorage flag) — scans every delivery row, clears any row whose `date + docket + kg` already exists in the same feed-type column. Skips rows without a docket number so legit same-day deliveries are never touched. Runs exactly once per browser, posts a banner with the number cleared.
  - Existing customers will see their doubled rows tidy up automatically the next time they open the EOB tab; new sync writes will never double again.
- **2026-06-24 (Weight-sheet upload: morts + caught now flow to shed cards & EOB)**:
  - **Root cause**: `loadBatchResultsXlsx()` already parsed `morts` and `catches` per shed, but only `catches` were pushed to the EOB sheet via `onEobCatch`. The `morts` value was dropped on the floor, and neither value appeared on the Summary tab's per-shed cards. So growers uploading a weight sheet saw the global Bird-Summary row update on EOB but the individual shed cards (where they plan feed) stayed stale.
  - **Fix #1** — added `onEobMorts(shedNum, morts)` prop to `BatchResultsView`. Called in the same xlsx-seed `useEffect` that already seeds catchMap, so every weight-sheet upload now writes morts into EOB cell `(shedRow, 24)` as well as catches into `(shedRow, 23)`.
  - **Fix #2** — added a new `ShedLiveCountsRow` strip under each shed's bird-count + breed row on the Summary tab. Renders red `−234 morts` and green `✓ 12,345 caught` pills the moment data is available. Hidden when both are zero. `data-testid="shed-morts"` and `shed-caught` for QA.
  - **Fix #3** — passed the App-level `catchMap` state through `SummaryView` → `ShedSummaryCard` so the caught counts update live as the user pastes a Baiada/Adelaide Weighbridge email, uploads a new weight-sheet xlsx, or edits the Catches tab manually.
  - Build clean, smoke-screenshotted. With no batch loaded the pills stay hidden (correct behaviour); once the user uploads their weight sheet they'll appear instantly per shed.
- **2026-06-24 (Farm Buddy + days-of-feed projection now use LIVE bird count)**:
  - **Root cause #2 of "feed planning ignores my pickups"**: `farmBuddySheds` was emitting `birdsPlaced = original placement` and computing `daysOfFeedLeft = siloTotal / dailyUsageT` — both assumed a constant flock size for the whole batch. After a pickup of 4,500 of 13,500 birds Farm Buddy was still projecting feed demand for 13,500.
  - **Fix (frontend)** — `farmBuddySheds` now computes:
    - `LIVE birdsPlaced = original placement − morts (from EOB col 24) − birds caught in the past (from catchMap)`.
    - `upcomingCatches: [{date, birds}, ...]` for any planning entries in the next 14 days.
    - `daysOfFeedLeft` is now a **day-by-day projection** that subtracts upcoming catches from the live flock on the scheduled catch dates before computing each day's feed demand — so after a big pickup the projection automatically lengthens.
  - **Fix (backend)** — `farm_buddy.py` `ShedSnapshot` gained `birdsOriginalPlaced`, `mortsToDate`, `birdsCaught`, `upcomingCatches`. The prompt builder now feeds the LLM lines like *"18,000 live birds (5,500 already caught of 24,000 placed), upcoming catches: 6,000 on 26/06/2026, 12,000 on 01/07/2026, day 42, silos 15.0t"* so advice scales with the shrinking flock instead of over-ordering.
  - Verified end-to-end: `POST /api/farm-buddy/recommend` returns a sensible response with the new payload shape; React build clean; page renders with no console errors.
- **2026-06-24 (Weight-sheet upload now drops BIRDS LEFT column per-day + trims trailing ghost rows)**:
  - **Root cause #3 — bird-count display wasn't dropping**: each shed sheet's "BIRDS LEFT" column (col 14) is computed by `birdsLeftByRow` = `placement − sum(col 13 entries on or before this row)`. Previous fix wrote catches only to the EOB sheet and the catchMap — but never to the per-day **col 13** on the individual shed tab. So the grower saw the EOB updated and the Summary card pills updated but the spreadsheet's BIRDS LEFT column stayed at the full placement number on every row.
  - **Fix** — added `onShedSheetCatch(shedNum, dateSerial, birds)` prop on `BatchResultsView`. Called once per catch row during xlsx seeding. The parent finds the matching SHED tab (via `SHED_SHEET_ORDER`), locates the row by date (col B/C), and adds the catch's bird count into col 13 of that row. **Tracked with localStorage key `xlsx-shed-catches-synced-v1`** so re-uploading the same weight sheet never double-counts.
  - **Trailing "ghost rows" trim**: the shed template auto-fills cols D/G/H/I/J with formulas for the entire 60-day range, so `lastNonEmptyRow` always returned row 72 — the user saw rows past the batch with negative feed alloc and constant 80,000 birds. Added a stricter `lastInputRow` that only counts user-input columns (ORDERED, silo readings, catch/morts). `shedDisplayEndRow` now:
    - Batch in progress → today + 14 rows
    - Batch ended (today way past last input) → lastInputRow + 3 rows (kills the ghost rows)
    - Empty sheet → first 14 rows minimum so a fresh batch doesn't look broken
- **2026-06-24 (Hard cap shed rendering at "Total Morts" / "Total Birds Caught" labels)**:
  - User screenshot showed 3 ghost rows still appearing *below* the Total Morts and Total Birds Caught labels with values like `-543,300`, `-225,621`, `0` — formula scaffolding rolling over template rows 75-77.
  - Added a `totalsLabelRow` lookup that scans the first 5 columns of each shed sheet for any cell matching `/total\s+(morts|birds|feed)/i`. The last such row becomes the absolute hard cap for `shedDisplayEndRow`. Nothing renders past it. If no totals labels are found we fall back to `shedDataStartRow + 62`.
- **2026-06-24 (Weight-sheet upload auto-populates Planning Catches)**:
  - User asked: "can we make the weight sheet update the plan catches on the date they're going to pick".
  - **Fix**: added a new `useEffect` inside `BatchResultsView` that, on every xlsx parse, mirrors each catch row into `weighPlanMap` (the Planning Catches map). Uses `xlsx-weigh-plan-synced-v1` localStorage set for fingerprint-based idempotency so re-uploads never double up. Skips dates the user has already entered manually (manual entries win).
  - Calls `saveWeighPlanMap` → fires `weighPlanUpdated` custom event → the App-level `weighPlanMap` listener picks it up → `planningCatchMap` (catchMap merged on top of weighPlanMap) refreshes → `farmBuddySheds` recomputes with the new upcoming catches → Farm Buddy's next-7-days projection shrinks the flock on the right dates.
- **2026-06-24 (Critical fix — parseWeighSheetBuffer was returning 0 rows for Double-B format)**:
  - **Root cause** of "weight sheet upload not changing any bird numbers across all sheds": the parser at line 2918 only accepted date cells stored as Excel serial numbers (`typeof v === "number" && v > 40000`). The user's actual weight sheet (`Double B Weigh Sheet`) stores dates as **datetime objects**, which xlsx returns as JS `Date` instances → the parser found zero day-columns → returned `[]` → nothing imported, no shed numbers updated.
  - **Fix**:
    1. Switched `XLSX.read` to use `cellDates: true` so all date cells normalise to JS `Date`.
    2. Added a `toDDMMYYYY()` helper that accepts JS Date, Excel-serial number, AND ISO/DD-MM-YYYY strings.
    3. The parser now finds EVERY MON/TUES-style header row in the file (the Double-B layout has 2 weeks stacked vertically — old parser only found the first one), parses each block until the next header, and merges all pickup events.
    4. Added a fallback that synthesises dates from "MON/TUES/WED" labels anchored to the upcoming Monday when no real date cells exist.
    5. Shed-label detection now accepts plain numbers like `1` as well as `1(CB)`, `3 (CB)`, etc.
  - **Verified end-to-end against the user's actual `Double B Weigh Sheet - 2026-06-24` file**: 22 pickup events extracted across 2 weeks (Shed 3 → 7000 birds on 29/06, Shed 12 → 11500 on 29/06, Shed 1 → 13533 on 03/07 ... all the way through Week 2). Python test reproduces the JS logic exactly.
- **2026-06-24 (Re-upload now AUTO-CORRECTS broken data from prior parser)**:
  - User asked: "the system already has the weight sheet in it but has not done what it needed before — when uploaded again it needs to auto correct it some how".
  - **Problem**: previous logic deduped by `xlsx-weigh-plan-synced-v1` fingerprints and `existing + birds` accumulation on shed-sheet col 13. If a prior (broken-parser) upload had left wrong/stale values behind, re-uploading would either skip the entries or ADD on top of the bad data.
  - **Fix #1 — Planning Catches map**: dropped the localStorage dedupe tracker. Every fresh xlsx parse now **fully replaces** each shed's `weighPlanMap` entries with what the new xlsx says (sheds not in the xlsx keep their existing data). Manual entries live in `catchMap` so they're untouched.
  - **Fix #2 — Shed-sheet col 13 (CATCH/MORTS)**: dropped the `existing + birds` accumulation. Each catch from a fresh xlsx now **overwrites** the matching `(row, 13)` cell on the shed tab. If the catcher amends a count and the grower re-uploads, the cell shows the new number cleanly — no stale-data leftovers.
  - End result: any re-upload is now the source of truth. Wipes and rebuilds, no manual cleanup needed.
- **2026-06-25 (System health sweep — backend stability + DB indexes + unified branding)**:
  - User asked: "can you check the whole system find fixes how to make better" + "app needs a matching logo on it" + "all same logo program and app and user wants there own logo same deal do what needs to be done".
  - **Backend fix #1 — `KeyError: 'name'` in `/api/readings/today`**: silos created without a `name` field were crashing the endpoint with `KeyError: 'name'` at `server.py:509`. Changed to `s.get("name", s.get("letter", "Silo"))` so legacy/letter-only silos render cleanly.
  - **Backend fix #2 — `RuntimeError: Response content longer than Content-Length`**: the rate-limiter was a `BaseHTTPMiddleware` (`@app.middleware("http")`) which wraps every response in a Starlette TaskGroup and breaks FastAPI's auto-Content-Length on streaming/file responses. Rewrote it as a pure ASGI middleware (`_RateLimitASGI` in `hardening.py`) that only intercepts when the path is in `RATE_LIMITS` — every other request passes straight through unwrapped. Zero Content-Length errors after restart.
  - **Backend fix #3 — Mongo indexes on hot collections**: added a startup hook `_ensure_indexes()` in `server.py` that creates compound indexes on `readings (farmId, readingDate desc)`, `deliveries (farmId, deliveryDate desc)`, `silos`, `shed_groups`, `feed_program_state (farmId unique)`, `farms (slug unique, ownerEmail)`, `farm_config (id unique)`, `photos (farmId, createdAt desc)`, `chat_messages (farm_id, created_at desc)`, `payments (session_id unique)`. Verified all 9 collections now have the new indexes.
  - **Branding fix — unified logo across Reader, Feed Program, Landing**:
    - Synced `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`, `logo.png`, `logo-small.png` between `/app/backend/static/` (Reader) and `/app/silo/artifacts/feed-program/public/` + `dist/public/` (Feed Program). All MD5s now match.
    - Updated `theme-color` to `#0f3d24` (brand green) in `reader.html`, `silo/artifacts/feed-program/index.html` + `dist/public/index.html`, `backend/static/manifest.json`, and `silo/.../public/manifest.json`.
    - Repointed the Reader's top "status-bar" strip from orange → brand green so the page chrome matches the silo+rooster logo.
    - Custom logo data flow (already in place): user uploads via Reader Settings → stored in `farm_config.logoData` (base64) → Feed Program's `App.tsx` polls `/api/farm-config` and renders `customLogo` in the SPA header. So a user's uploaded logo now appears in BOTH the Reader and the Feed Program automatically.
  - **Verified**: `curl /api/readings/today`, `curl /api/farm-buddy/alerts`, `curl /api/error-report` × 5 (rate-limited) all return 200. Backend logs are clean — 0 KeyError / 0 Content-Length errors after the fix.
- **2026-07-02 (Farm Buddy phantom-shed suppression + www→apex 301 redirect)**:
  - **Bug fix (P0) — Farm Buddy false alerts on unconfigured sheds 13/14/15/16**: `/api/farm-buddy/alerts` used to pin a "🐔 EMPTY" info alert for every shed_group not present in `farm_config.enabledGroupIds`. Because the default seed always creates 20 sheds (10 groups), growers running fewer sheds saw 4-8 spurious warnings for phantom sheds they never placed. Fix at `server.py:1175-1240`: computed `phantom_group_ids = empty_group_ids ∩ groups_without_history` (any group with zero historical silo readings + currently disabled). Phantom sheds are now skipped from EMPTY-pin generation entirely; real depopulated sheds (readings on file + disabled) still get the info pin. Same filter applies to stale-reading and missing-today watchdog loops (already were, via `empty_group_ids`).
  - **SEO fix (P2) — www→apex 301 middleware**: added `@app.middleware("http")` `www_to_apex_redirect` at `server.py:94-107`. Any request whose `Host` header starts with `www.` gets a 301 to `https://<apex><path>?<query>`. Backup to DNS-level redirect flagged by Semrush audit; preserves link equity.
  - **Testing**: 13/13 backend pytest tests pass (`/app/backend/tests/test_phantom_sheds_and_www_redirect.py`) via `testing_agent_v3_fork` (iteration_3). Phantom sheds now excluded from `syncHealth.emptyGroups`. Local curl verified 301 redirect (`location: https://broilerbasemate.com.au/`).
- **2026-07-27 (Trust-first hero + Founding Grower offer + Sales Playbook + Live app screenshots + Kill Buyer Details Modal)**:
  - **P0 conversion fix — killed Buyer Details Modal**: `.start-btn` and `#ops-checkout` handlers now skip the "name + email" modal and hit `/api/checkout` directly. Stripe already collects email on its own hosted page, so the extra step was pure drop-off (audit estimate: 20-40% loss). Optional `email`/`buyerName` prefill still flows through when the buyer has one saved in `localStorage` (e.g. from a personalised `/g/<slug>` outreach link). The modal HTML + `openBuyerModal()` helper are retained but unused, so nothing else regresses. Verified end-to-end: clicking "Start Free Trial" now redirects straight to `checkout.stripe.com/c/pay/cs_live_...` with zero intermediate screens.
  - **Landing rewrite + real app screenshots** (earlier this session): tighter hero, Founding Grower deal band, "See it in action" section with real Feed Program + Silo Reader screenshots captured via Playwright.
  - **Sales Playbook** at `/app/memory/sales_playbook.md`.
- **2026-07-22 (Reader History layout + Excel export upgrade)**:
  - **UI fix (P1) — "on app when i read silo its has nice lay out … why can we do the same for history"**: Rebuilt `loadHistory()` in `/app/backend/static/reader.html` so each day now renders one card per shed group (matching the "Today" tab), with silos A/B/C in a single horizontal row + a per-shed **TOTAL** and an orange **DAY TOTAL** on the date header. Added `.hist-shed-card` / `.hist-silo-cell` styles.
  - **Excel export upgrade (P1) — "needs a total of feed for the day when i share excel"**: Rewrote the "Export Excel" click handler to pivot readings into columns: **Date · Time · Shed Group · Silo A (t) · Silo B (t) · Silo C (t) · Shed Total (t)**, with a **DAY TOTAL** row after each date section and a blank separator. All amounts normalised to tonnes so head-office can sum any column instantly.
  - **Verified visually**: reader.html History tab shows the new card layout with orange DAY TOTAL badges. Excel export sample confirmed via JS-in-page evaluation.
- **2026-07-22 (Flock Forecast — per-shed weigh-ins + "2 Shed 7" label bug + AI camera unhidden on desktop)**:
  - **UI/data fix (P1) — "putting in weights but its sheds in groups, no weights for each shed"**: Weigh-ins were stored as one group-level number per age → weighing Shed 3 then Shed 4 silently averaged them together. Rebuilt storage to per-slot: `WeighInEntry = number | { s1?: number; s2?: number }` with `readWeighInSlot` / `readWeighInGroup` / `writeWeighInSlot` helpers that transparently migrate legacy `number` entries. Updated `logManualWeight`, `logToForecast` (⚖️ Weigh Birds tab) and the FlockForecastView tracker table to save/render per slot. Weigh-in Tracker now shows **two columns** — one per individual shed — with per-slot editing and correct Δ% aggregation. Whole-farm aggregation reads via `readWeighInGroup` so cross-shed averages still work.
  - **Label fix (P1) — "there was 2 shed 7"**: `Shed 9 & 10` card was showing "SHED 7 / SHED 8" in its slot chips because the XLSX template left stale labels in rows 3/4 col B. Added a regex guard: if the raw XLSX label doesn't contain the group's expected shed number (`\bN\b`), fall back to the canonical `Shed N`.
  - **UI fix (P0) — "AI camera missing, only manual weights"**: The ⚖️ Weigh Birds tab (with the AI camera) was gated behind `isTouchDevice = navigator.maxTouchPoints > 0` — so it was invisible on desktop browsers. Removed the gate: the tab now shows on all devices. Modern desktop browsers support `getUserMedia` too, so the AI camera flow works everywhere. Verified visually — AI Camera / Manual Entry toggle both visible on desktop.
  - **Verified visually**: Shed 9 & 10 card now shows "Shed 9 / Shed 10" chips + "SHED 9 / SHED 10" tracker columns. Backend regression suite still 64/64 green.
- **2026-07-15 (Backend stability + Sheds 1 & 2 header label fix + Farm Buddy smarter data)**:
  - **Test fix (P3) — Flaky `test_today_filters_to_today_only`**: rewrote the test in `/app/backend/tests/test_sync_and_alerts.py` to pre-clean any stale readings for the target silo, use the LAST silo of the group (not `gsilos[1]`), and post-clean after asserting. Now runs deterministically. Full backend suite: **64/64 passing**.
  - **UI fix (P1) — Sheds 1 & 2 missing "FEED ALLOC" gold header**: the XLSX template had empty cells at COL_G on the Shed 1&2 tab (Sheds 3+ carry the label). Added an override in `App.tsx` render loop (`isMissingFeedAllocHeader`) that injects "FEED"/"ALLOC" on header rows 7/8 and forces gold text color even when `info.bold` is false. Rebuilt via `BASE_PATH=/ yarn build` in `/app/silo/artifacts/feed-program/`. Verified visually via magic-link screenshot — Shed 1 & 2 now identical to Shed 3 & 4.
  - **AI fix (P0) — Farm Buddy "way off track with silo data"**: The `getShedSnapshots` builder only read COL_J/K/L/M (physical silo dips) so if the user pasted feed orders without doing a manual silo reading, Farm Buddy saw `siloTotal=0` and cried "critical". Three-part fix in `App.tsx`:
    1. When silo A/B/C are empty, **fall back to FEED ON HAND (COL_I)** — this cascades automatically from deliveries + usage.
    2. **Sum future FEED ORDERED (COL_E) entries** as `incomingT`, add to the days-of-feed projection, and annotate the shed name (e.g. "Shed 1&2 (30.0t incoming)") so the LLM sees the pipeline.
    3. **Skip completed batches** — if `dayAge >= batchEndAge` OR `liveBirds <= 0`, the shed is excluded from Farm Buddy's context so stale/finished batches don't trigger false "critical" alarms.
  - **Removed from roadmap** (per user 2026-07-15): "Wingman" voice AI (permanently dropped) and all P1 marketing/email-domain tasks.



## Future / Backlog
- 🟡 P1: Stripe LIVE mode — swap `sk_test_` for user's `sk_live_…` + real Price IDs (next session when user is home).
- 🟡 P1: Paste real Resend API key + verify live email delivery to `appcovi2026@gmail.com`.
- P2: Auth — Emergent Google Auth or JWT to lock down farm data.
- P2: Sora 2 social-marketing video clips (awaiting credit top-up).
- P2: Auto-allocation — parse `deliveryInstructions` "5 B 10, 6 B 5, 7 B 13" → create one delivery row per shed-silo automatically.
- P2: Feed Program UI farm-picker (currently always uses `default`; can be added once Ops have farms).
- P2: PDF / CSV export of full history.
- P2: Search / filter in History (by supplier, date range, feed type, docket #).
- P3: Custom-domain email via Resend (DNS for `broilerbasemate.com.au`).
- P3: Refactor monolithic `server.py` (>1800 lines) and `App.tsx` (>10000 lines).
- P3: Multi-worker Uvicorn + CDN for 10k+ user scale.
- P3: Google Drive / OneDrive cloud sync (needs user OAuth tokens).
- P3: Restore companion Silo Tracker PWA (needs Clerk publishable key).
