# Broiler Base Mate™ — Off-Replit Migration

## Original Problem Statement
> https://silo-sync-excel.replit.app/feed-program/ I need off replit
> GitHub: https://github.com/coverdalej72-lab/Silo-Sync-Excel

The user wants the "Broiler Base Mate™ — Feed Program" app (originally hosted on Replit) running off Replit so it works without dependence on Replit's hosting.

## Architecture (off-Replit)
- **Monorepo source**: `/app/silo/` (the full pnpm workspace cloned from GitHub)
- **Frontend** (`/app/frontend` → port 3000): thin wrapper. `yarn start` shells into `/app/silo` and runs `pnpm --filter @workspace/feed-program run dev` (Vite dev server) with `BASE_PATH=/`.
- **Backend** (`/app/backend` → port 8001): FastAPI stub providing the few `/api/*` endpoints the feed-program calls (`/api/readings/today`, `/api/deliveries`, `/api/weigh-bird`, `/api/batch/reset`, `/api/bootstrap`, `/api/health`). All return safe empty/no-op responses so the largely-client-side spreadsheet works without PostgreSQL/Clerk/Stripe/AI services.
- **Vite proxy**: `/api` requests inside the Vite dev server are proxied to `localhost:8001`.
- **Data persistence**: localStorage (matches the original feed-program design — farm config, theme, batch data are all client-side).

## What's Implemented (2026-01)
- Cloned full monorepo from GitHub.
- Installed pnpm + all workspace dependencies (no frozen lockfile).
- Successful production build of `@workspace/feed-program` (verified).
- Vite dev server live on port 3000; FastAPI stubs live on port 8001.
- Full Broiler Base Mate Feed Program UI verified working via external preview URL (`https://harvest-hub-634.preview.emergentagent.com/`): toolbar, tabs (1&2 / 3&4 / … / end of batch), spreadsheet rendering, formulas, Settings, Save & Download all visible and reactive.
- Vite proxy configured to talk to local FastAPI on 8001 instead of Replit's Express api-server on 8080.

## NOT Migrated (intentional — they require external services the user did not provide)
The full Replit monorepo has 6 other artifacts that depend on services the user does not have keys for. These were NOT brought up:
- `artifacts/api-server` (Express + Drizzle ORM + PostgreSQL + Clerk auth + Stripe + OpenAI/Gemini) — replaced by FastAPI stub.
- `artifacts/silo-tracker` (companion mobile app — needs Clerk + Postgres for cloud sync).
- `artifacts/farm-buddy-home`, `artifacts/silo-mate-plans`, `artifacts/how-to-video`, `artifacts/mockup-sandbox` — marketing/landing pages, not part of the URL the user referenced.

## Backlog / Next Action Items
- **P1**: If user wants silo-tracker (the field reading app at `/silo-tracker/`) running too, we need Clerk + PostgreSQL keys, or we can run it in offline-only mode (localStorage).
- **P1**: If user wants real cloud sync to Google Drive / OneDrive, provide `GDRIVE_ACCESS_TOKEN` / `ONEDRIVE_ACCESS_TOKEN`.
- **P2**: Wire `/api/readings/today` to a real DB (MongoDB) so silo readings from the tracker actually flow into the feed program.
- **P2**: Run the original Express api-server alongside the FastAPI stub (would need PostgreSQL + Clerk).
- **P3**: Restore the `/feed-program/` URL prefix for parity with the original Replit URL (currently served from root `/`).
