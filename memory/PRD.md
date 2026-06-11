# Broiler Base Mate™ — Off-Replit Migration + Auto-Sync + AI Docket Scanner

## Original Problem Statement
> https://silo-sync-excel.replit.app/feed-program/ I need off replit
> "do what best as i need to auto sync to the program"
> "do them all"
>
> GitHub: https://github.com/coverdalej72-lab/Silo-Sync-Excel

## Architecture
- **Source monorepo**: `/app/silo/` (pnpm workspace cloned from GitHub).
- **Frontend** (`/app/frontend` → port 3000): Vite dev server for `@workspace/feed-program` (`BASE_PATH=/`).
- **Backend** (`/app/backend` → port 8001): FastAPI + MongoDB + Gemini 2.5 Flash via emergentintegrations universal key.
- **Field Reader** at `/reader`: 3-tab mobile PWA-style UI served from FastAPI.
- **Vite proxy**: `/api`, `/reader`, `/reader-assets` → port 8001.

## Field Reader (`/reader`) — Three Tabs
### 1. 📋 Readings (auto-sync)
- Pending sheds bubble to the **top**, partial sheds in the middle, DONE sheds at the bottom — workers can knock off the next pending shed in one tap.
- Each silo shows **"Last: Xt (Mon, 2 Jun) [Use ↑]"** — one-tap copies the previous reading into the input so users only type the change.
- Feed type picker per shed (Starter/Grower/Finisher/Withdrawal) + tonnes/kg unit toggle per silo.
- Save → POST `/api/readings/batch` → MongoDB → Feed Program auto-poll (2-min) picks it up. Same-day re-save = correction.

### 2. 🚚 Deliveries
- Form: shed group, optional silo, feed type, amount in tonnes, free-text notes.
- Recent Deliveries list with one-tap delete.
- Posts to `/api/deliveries` → reflected in End-of-Batch summary.

### 3. 📷 Scan Docket (AI)
- Vendor toggle: **Ingham's** / **Baiada** (two tailored prompts).
- "Tap to take a photo" → mobile camera capture → in-browser resize to ≤1400px JPEG.
- **Gemini 2.5 Flash** via `emergentintegrations` extracts: feedType, productCode, amount, deliveryDate, orderNumber, customerName, siteCode, deliveryInstructions, truckRego, outloadingBin.
- "Use as Delivery" pre-fills the Deliveries form and switches tabs — review, then save.

## Backend API (MongoDB-backed)
- `GET /api/shed-groups` — auto-seeds 10 groups × 3 silos on first run
- `GET /api/silos`, `POST /api/silos`, `PATCH /api/silos/:id`, `DELETE /api/silos/:id`
- `GET /api/readings/today` (Feed Program polls every 2 min)
- `GET /api/readings/previous?siloId=…` — last reading per silo
- `POST /api/readings/batch`, `GET /api/readings`, `DELETE /api/readings/:id`
- `GET /api/deliveries`, `POST /api/deliveries`, `DELETE /api/deliveries/:id`
- `POST /api/scan-docket/ingham`, `POST /api/scan-docket/baiada` (Gemini 2.5 Flash)
- `GET /api/bootstrap`, `GET /api/batch/version`, `DELETE /api/batch/reset`, `GET /api/onedrive/status`, `POST /api/weigh-bird`

## What's Verified
- ✅ End-to-end save: 18.5t saved on `/reader` → Feed Program syncHash `|A:18.5:t||||||||` confirmed.
- ✅ Pending-first ordering on Readings tab.
- ✅ Previous-reading "Use ↑" button populates input + unit.
- ✅ Delivery save round-trip (24.5t Starter / Sheds 3 & 4 / Test docket #123).
- ✅ AI scanner: synthetic Ingham docket → all 11 fields extracted correctly (28.16t Gourmet Broiler Grower F116, Order ORD-77821, Date 2026-06-11, etc.).
- ✅ Scanner → Deliveries pre-fill flow.

## Integrations
- **Emergent Universal LLM Key** (`EMERGENT_LLM_KEY`) → Gemini 2.5 Flash for docket OCR.
- (Stripe / Clerk / Google Drive / OneDrive intentionally not wired — not needed for stated requirements.)

## Backlog (future, optional)
- P2: Auto-detect batch placement so today's reading lands on the correct demo-row visually (currently March 2026 placement, today is June).
- P2: Pre-populate `feedType` in Readings tab using last delivery's feed type for the shed.
- P3: Add CSV / xlsx export of all readings + deliveries from `/reader`.
- P3: Google Drive / OneDrive cloud sync (needs user OAuth tokens).
- P3: Restore companion Silo Tracker PWA (needs Clerk publishable key).
