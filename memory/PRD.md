# Broiler Base Mate™ — Off-Replit Migration + Auto-Sync

## Original Problem Statement
> https://silo-sync-excel.replit.app/feed-program/ I need off replit
> GitHub: https://github.com/coverdalej72-lab/Silo-Sync-Excel
> Follow-up: "do what best as i need to auto sync to the program"

## Architecture
- **Source monorepo**: `/app/silo/` (full pnpm workspace cloned from GitHub).
- **Frontend** (`/app/frontend` → port 3000): wrapper that runs `pnpm --filter @workspace/feed-program run dev` (Vite dev server) with `BASE_PATH=/`.
- **Backend** (`/app/backend` → port 8001): FastAPI + MongoDB. Implements the real OpenAPI surface (shed-groups, silos, readings, deliveries, onedrive/status, bootstrap, batch). Auto-seeds 10 shed groups × 3 silos on first run.
- **Field Reader** (`/reader`): mobile-friendly HTML/JS UI served by FastAPI for entering silo readings in the field.
- **Vite proxy**: `/api`, `/reader`, `/reader-assets` → `localhost:8001`.

## Auto-Sync Flow (working end-to-end)
1. User opens `https://harvest-hub-634.preview.emergentagent.com/reader` on phone in the field.
2. Picks shed group, picks feed type (Starter/Grower/Finisher/Withdrawal), enters silo A/B/C tonnes, taps **Save Readings**.
3. POST `/api/readings/batch` → stored in MongoDB `readings` collection.
4. Feed Program (open on PC at `/`) auto-polls `/api/readings/today` **every 2 minutes** (existing behaviour in `App.tsx`).
5. New readings are detected via sync-hash diff and applied to the spreadsheet's `SILO A/B/C` columns at the correct day row.
6. Re-saving the same shed today overwrites (acts as a "correction" — same behaviour as the original Replit app).

## Verified
- 18.5 t reading saved to Sheds 3 & 4 Silo A → Feed Program syncHash localStorage = `|A:18.5:t||||||||` confirmed pulled.
- Silo Reader UI saves A/B/C in one tap, badge turns DONE, "Saved" counter updates.
- API: `/api/shed-groups`, `/api/readings/today`, `/api/readings/batch`, `/api/deliveries`, `/api/silos*`, `/api/bootstrap`, `/api/batch/version`, `/api/batch/reset`, `/api/onedrive/status`, `/api/weigh-bird` all responding.

## Backlog
- **P2**: Restore demo spreadsheet placement date or let user reset it from Settings so today's reading visually lands on a row (today the demo's batch ends 18 May 2026 — pre-built data).
- **P2**: Wire deliveries to the End-of-Batch summary (`/api/deliveries` is read but only stub-saved from the reader UI).
- **P3**: Build a QR-scan flow into `/reader` for Ingham/Baiada delivery dockets (silo-tracker had `Html5Qrcode` integration).
- **P3**: Cloud sync to Google Drive / OneDrive (requires user-provided OAuth tokens).
- **P3**: Restore the full `/silo-tracker/` companion app (requires Clerk publishable key from user).

## Test Data
None (MongoDB auto-seeds 10 shed groups × 3 silos on first startup; readings collection starts empty).
