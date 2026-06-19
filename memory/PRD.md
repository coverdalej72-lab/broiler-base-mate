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

## Future / Backlog
- 🟡 P1: Stripe LIVE mode — swap `sk_test_` for user's `sk_live_…` + real Price IDs (next session when user is home).
- 🟡 P1: Paste real Resend API key + verify live email delivery to `appcovi2026@gmail.com`.
- P2: Auth — Emergent Google Auth or JWT to lock down farm data.
- P2: Sora 2 social-marketing video clips (awaiting credit top-up).
- P2: Auto-allocation — parse `deliveryInstructions` "5 B 10, 6 B 5, 7 B 13" → create one delivery row per shed-silo automatically.
- P2: Feed Program UI farm-picker (currently always uses `default`; can be added once Ops have farms).
- P2: PDF / CSV export of full history.
- P2: Search / filter in History (by supplier, date range, feed type, docket #).
- P3: Google Drive / OneDrive cloud sync (needs user OAuth tokens).
- P3: Restore companion Silo Tracker PWA (needs Clerk publishable key).
