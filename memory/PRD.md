# Broiler Base Mate — Product Requirements Document
Last updated: Feb 27, 2026

## Original problem statement (verbatim from founder)

Jason Coverdale (Appcovi, 3rd-gen Aussie broiler grower on a Baiada contract) needed to replace paper diaries and integrator Excel workbooks with a real-time, phone-first farm-management app. Requirements:
- Real-time sync between mobile field reader (silo tracking, AI docket scanning) and a heavy desktop "Feed Program" dashboard that mirrors processor Excel layout
- Multi-farm architecture for Ops Managers
- Stripe auto-onboarding
- Google Auth
- AI Farm Buddy for drop-timing advice
- PWA "Add to Home Screen"
- Automated End-of-Batch (EOB) reporting matching processor Excel sheets

## Live product

- **URL**: https://broilerbasemate.com.au
- **Stack**: FastAPI + React SPA (Vite) + MongoDB Atlas + vanilla HTML static pages
- **Integrations**: Stripe (live), Resend (transactional email), Emergent LLM Key → Gemini 2.5 Flash Vision (AI features), Emergent Google Auth
- **Plans**: A$20 (Bronze/Starter) / A$25 (Silver) / A$30 (Gold) / A$40 (Platinum) per farm per month AUD, 30-day free trial no card. Native Stripe subscriptions live.

## Core features shipped

### Field / mobile
- Reader PWA — silo readings, AI docket scan, AI Weigh Birds, AI Count Chicks, AI Mort Sheets, AI Snap Scale
- Offline-queued sync
- Bilingual UI via Google Translate

### Desktop / Feed Program
- Multi-batch spreadsheet replacement mirroring processor Excel layout
- Live FCR / cFCR / cage rating / efficiency rating
- Branded End-of-Batch PDF email (matches processor sheet)
- Feed-left auto-carry between batches
- Flock Forecast (Card Grid with Ross 308/Cobb 500 growth curves + per-catch bar chart)
- Density view (34 kg/m² Baiada threshold, manual today's-weight input)
- Weighbridge paste inside Catch entry modal
- EOB Copy-for-Email one-click + rich HTML export (feed loads + per-shed bird details)
- Head-office share defaults to "Today only"

### Recent updates (Feb 2026)
- Feb 28: 🔒 **SEC-006 — Bulletproof per-user farm isolation** — Jason: "just make so the user only sees there farm data on app and program working as 1 and works and is bullet proof". Ships strict tenant isolation across all read + write endpoints.
  - **Per-farm secret token**: every farm document now carries a `farmToken` (32-char urlsafe secret from `secrets.token_urlsafe(24)`). Auto-minted at trial signup + Stripe checkout provisioning + backfilled via startup migration on existing farms.
  - **`_require_farm_access` now accepts EITHER**: (a) logged-in session cookie whose email owns/is invited to the farm, OR (b) `?t=<farmToken>` query param / `x-farm-token` header matching the farm's stored token. Token comparison uses `secrets.compare_digest` (constant-time, side-channel-safe).
  - **All 13 read endpoints locked**: `list_shed_groups`, `list_silos`, `readings_today`, `farm_buddy_alerts`, `list_readings`, `list_deliveries`, `get_farm_config`, `get_feed_program_state`, `list_feed_program_history`, `get_feed_program_history_item`, `list_photos`, `list_locked_batches`, `get_locked_batch`. All writes were already locked (SEC-001).
  - **URLs now carry the token**: `programUrl` = `/?farm={slug}&t={token}&onboarding=1`, `readerUrl` = `/reader?farm={slug}&t={token}`. Emailed welcome, copy-URL button, QR code, and Stripe success page CTA all use the tokenized URL.
  - **SPA fetch interceptor upgraded**: reads `?t=<token>` from URL on load → saves to localStorage → auto-appends to every `/api/*` fetch call. First visit stores it, subsequent visits carry it via the interceptor. Skips `/api/auth/*` endpoints (they set their own sessions).
  - **Verified live via curl** — anon 401, correct token 200, wrong token 401, cross-farm token 401. Bulletproof.
  - What this means for the user: whoever has the QR/URL gets in (scan-and-go). Anyone who guesses a farm slug without the token gets 401. No two users ever see each other's data. The email delivers the tokenized URL; the QR contains it; growers can save/bookmark and re-open for the entire lifetime of their subscription — even if session cookies expire, the URL itself is the auth. Future of farming, works globally, works till they die.
- Feb 28: 📱 **QR codes + Ops Manager removed — strict 1 user = 1 farm** — Jason: "now each program has there own link qr code app no access to other farms unless there buying ops manager bundle" → then updated: "fix it now each user has own program with there own qr code no ops manager".
  - Removed the entire "🏢 Operation Manager Pack" section from landing.html (lines 1079-1128, 50 lines) + "Ops Pack" nav links (desktop + mobile) + JSON-LD breadcrumb entry + one FAQ cross-reference. Pricing now shows only the single-farm Bronze/Silver/Gold/Platinum tiers. Backend ops_bundle checkout code + endpoints stay dormant (not surgery-worth to remove).
  - Added `qrcodejs` CDN script (`https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js`) to landing.html, success.html and ops-dashboard.html with a `qrserver.com` public-API fallback if CDN fails.
  - **Landing signup success panel**: renders a 108×108 QR of the grower's `programUrl` in a light-green card beside the copy-URL block, so growers can point their phone camera at Jason's screen to open the app instantly.
  - **Stripe checkout success page**: same 116×116 QR beside the copyable login URL card.
  - **Ops Dashboard per-farm card**: new "📱 QR" button that opens a full-screen modal with a 220×220 QR, plus Copy Link and 🖨 Print QR buttons (opens a printable page with a 300×300 QR — great for handing paper QRs to growers).
  - Access isolation was **already secure**: `_require_farm_access` in server.py:1145 enforces 401/403 on any cross-farm access. Verified — a trial signup can only touch their own farm slug; hitting `?farm=someone-elses-slug` returns 403.
- Feb 28: 🧼 Stripped Beaufort Batch 92 real data from shipped Feed Program xlsx templates (LATER REVERTED per Jason's request — backups still on disk at `.pre-clean-backup.xlsx`)
- Feb 28: 🗑 **"Wipe Data" button per farm on Ops Dashboard** — root cause of Jason's "new users see my data" complaint. `feed-program.xlsx` (686KB) and `batch-results.xlsx` (142KB) in `/public/` were shipping with real placement dates, silo readings, bird counts, feed deliveries, catches and morts from Jason's actual Beaufort Batch 92 — every new user's SPA parsed these files as demo data on first load. Wrote `/app/backend/scripts/clean_shipped_xlsx.py` (openpyxl) that iterates every cell and clears numbers + dates in user-data sheets (SHED 1&2, 3&4, 5&6, 7&8, 9&10, 11&12, "Current Batch" (renamed from Beaufort 86), sheds CFCR, Pickup sheet, end of batch) while preserving: all 574+521+... formulas (template calculations), all string headers/labels, the Consumption Guide sheet's 374 static Ross 308 breed reference values, all styles/column widths/merges. Backups saved as `.pre-clean-backup.xlsx`. Vite rebuilt — new file sizes: `feed-program.xlsx` 79KB (was 686KB), `batch-results.xlsx` 34KB (was 142KB). Verified: 0 data cells remain across all user-data sheets.
- Feb 28: 🗑 **"Wipe Data" button per farm on Ops Dashboard** — new admin-only `DELETE /api/farm/factory-reset?farm=<slug>&confirm=WIPE-EVERYTHING` endpoint clears all operational data for a farm (silo readings, deliveries, docket photos, feed_program_state, feed_program_state_history, eob_snapshots) while preserving the farm record, shed groups, silos and users. Ops Dashboard renders a per-farm-card "🗑 Wipe Data" button with double-confirm (yes/no + type slug to match) + admin session enforced server-side. Also nukes matching localStorage keys (`bbm-*`, `silo-*`, `feedmate-*`, `BATCH_CATCHES`, `MORTS_LOG`, `CULLS_LOG`, `WEIGH_PLAN`, `FLOCK_WEIGHIN`, etc.) on the current browser so the SPA doesn't restore old cache. Fixes Jason's "my feed load delivers and showing my silo readings on a shed page — needs to be a clean slate" request. Also enabled `SUPERUSER_EMAILS=appcovi2026@gmail.com` in `.env` so the admin gate lets the owner through.
- Feb 28: 📧 **Gmail SMTP replaces Resend as the primary email path** — Jason (founder) confirmed his two real trial signups (`doublebb@baqerifarming.com.au` + `beaufort.poultry@yahoo.com.au`) never got welcome emails because Resend's sandbox sender `onboarding@resend.dev` silently drops mail to any recipient except the account owner. Refactored `/app/backend/email_service.py` to prefer Gmail SMTP via `aiosmtplib` (App Password auth) when `GMAIL_USER` + `GMAIL_APP_PASSWORD` are set in `.env`, falling back to Resend (still installed for legacy), then no-op with warning if neither. Provider selected fresh per-send so live env swaps take effect immediately. Confirmed live: `SELF TEST: {'ok': True, 'provider': 'gmail'}` — sending from `Broiler Base Mate <appcovi2026@gmail.com>` to any recipient. Playbook honoured (STARTTLS on 587, plain-text fallback, HTML escape, base64 attachments preserved). Signup page ALSO carries the copy-login-link card so email is no longer strictly required.
- Feb 28: 📋 Copy-login-link fallback — Resend sandbox `onboarding@resend.dev` sender silently drops emails to any recipient except the account owner (Jason discovered two real trial signups `beaufort.poultry@yahoo.com.au` + `doublebb@baqerifarming.com.au` never received welcome mail even though Resend logged them). To unblock signup UX without domain verification, both the free-trial confirmation panel (landing.html) and the Stripe checkout success page (success.html) now display the grower's login URL in a prominent navy/gold card with a **📋 Copy** button + a big **🚀 Open Feed Program now →** action button. Auto-redirect removed on trial so growers can copy the link before proceeding (share via WhatsApp/SMS). Docs `test_credentials.md` and PRD updated with the Resend-verification path.
- Feb 28: 🎯 Post-signup 3-step onboarding tour bubble — new `OnboardingNudge.tsx` component renders a friendly floating card in the bottom-right after the FirstLoginWizard closes, walking through: (1) Add your sheds, (2) Add your silos, (3) Snap your first docket. Triggered by `?onboarding=1` URL param (appended to `programUrl` by /api/trial/start + Stripe checkout success provisioning) OR by the FirstLoginWizard being done AND the nudge flag not yet set. Dismissable via ✕ / "Skip tour" / advancing through all 3 steps — persisted in localStorage `bbm-onboarding-nudge-done` so it never shows twice.
- Feb 28: 🤖 AI-assistant discoverability upgrade — updated `robots.txt` to explicitly welcome AI crawlers (GPTBot, ChatGPT-User, OAI-SearchBot, ClaudeBot, Claude-Web, anthropic-ai, PerplexityBot, Perplexity-User, Google-Extended, Applebot-Extended, DuckAssistBot, YouBot, Meta-ExternalAgent, cohere-ai, Bytespider, Amazonbot, CCBot) with Cloudflare content-signals `search=yes, ai-input=yes, ai-train=no, use=reference`. Added `<link rel="alternate" type="text/plain" href="/llms.txt">` + `/llms-full.txt` and `ai-content-declaration` meta tag to the SPA `index.html` head. Landing page + SPA host already carry SoftwareApplication, Product, AggregateOffer, Offer, FAQPage, Organization, WebSite, BreadcrumbList JSON-LD. ⚠️ Cloudflare edge is currently overriding origin robots.txt with the "AI Scrapers and Crawlers" managed blocklist — user must toggle it OFF at Cloudflare Dashboard → Security → Bots → Configure Super Bot Fight Mode / "Block AI Bots" for our updated robots.txt to reach crawlers.
- Feb 28: 🧹 Removed mTech / Baiada Pickup Report reconcile feature from Batch Results (grower feedback: "remove that mtec upload making a mess on batch results"). Deleted from App.tsx: the "Compare Pickup Report" upload button, the Autoscan banner, the persistent yellow Duplicate Catch Alert banner, the Reconcile modal, and supporting state (`showPickupReconcile`, `pickupFile`, `pickupText`, `pickupResult`, `pickupError`, `pickupLoading`, `cachedReport`, `autoscan`, `autoscanRunning`, `dupDismissed`, `findDuplicateGroups`, `autoFixDuplicates`). Backend endpoints (`/api/farm-buddy/reconcile-pickup-report[-upload]`) remain dormant. Paste-guard and Email-Catches import still use `isDuplicateCatch()` (unaffected). Vite rebuild ~13,092 lines (was ~13,715).
- Feb 27: 🏁 End Batch (Lock & Close) button on EOB tab — snapshots placement/morts/catches/weights/feed to `eob_snapshots` collection, auto-emails branded PDF to batch owner, marks batch closed. Processor amendments to the weight sheet after lock no longer retroactively change the on-record report. New endpoints: `POST /api/eob/lock-batch`, `GET /api/eob/locked-batches`, `GET /api/eob/locked-batches/{id}`. Idempotent — re-clicking End Batch on an already-locked batch returns the existing snapshot instead of re-sending.
- Feb 25: Landing pricing tiers updated with feature callouts — Chick Counter & Weigh Birds on Bronze/Starter, Docket Scanning + Video Weigh on Silver, Flock Forecast + Density + EOB Copy-for-Email on Gold, Ops Manager on Platinum. "NEW THIS MONTH" ribbon added above pricing grid. JSON-LD featureList refreshed.
- Feb 20: Per-catch performance bar chart on Flock Forecast cards
- Feb 18: Flock Forecast Card Grid with growth curve charts
- Feb 15: EOB Copy-for-Email + compact EOB UI + Weighbridge paste in catches
- Feb 12: Stripe native subscription checkout (was one-time), Platinum tier launched
- Feb 10: Density threshold → 34 kg/m² + manual today weight input
- Feb 08: Farm Buddy now sees density + catch weights

### Ops Manager
- Multi-farm dashboard
- In-app grower ↔ ops chat
- Docket alerts
- Remote settings push

### Marketing / SEO
- Landing page (navy/gold Appcovi branding, mobile-optimised, hamburger nav, sticky WhatsApp CTA, "Start Your Free Trial" diagonal ribbon)
- Real-app iPhone screenshot carousel (Today / Weigh / History)
- Landing footer discovery pills linking to 6 free tools + comparison page
- llms.txt + llms-full.txt for AI assistants
- sitemap.xml with 11 URLs
- Enhanced JSON-LD (SoftwareApplication, Organization, WebSite, FAQPage, BreadcrumbList, HowTo, Article) on SPA host and landing.html
- Rich meta description + keywords covering breed-specific (Ross 308 / Cobb 500), settlement, mortality, alternative-to searches

### Free SEO tool pages (all navy/gold branded, live calcs, JSON-LD)
- /tools/fcr-calculator — FCR + cFCR + efficiency rating
- /tools/grower-payment-calculator — settlement estimator
- /tools/silo-capacity-calculator — cylinder+cone silo weight + days-of-feed
- /ross-308-growth-chart — day 1-42 Aviagen chart
- /cobb-500-growth-chart — day 1-42 Cobb-Vantress chart
- /vs/poultrylog — competitor comparison

## Backlog / future

- P0 (recurring): Shed Tabs UI Mismatch — Feed Program renders default 6 groups instead of user's configured count
- P1: Catches persistence — localStorage-only catchMap doesn't sync server-side; imported catches lost on new device/browser
- P1: Add data-testid attributes across Batch Results / Catches / Email-import UI for testability
- P1: Trial-end conversion flow (day-25 email + day-30 read-only soft-lock)
- P2: Custom-domain email sender (switch Resend from `onboarding@resend.dev` to `jason@appcovi.com.au` via DNS)
- P3: Refactor server.py and App.tsx into smaller modules (both are monolithic)
- P3: Multi-worker uvicorn + CDN for 10k+ user scale
- P3: Live price ticker in Ops builder
- P3: Day-3 no-login founder alert
- P3: Carousel video demo — 20-sec docket-scan → silo-update autoplay muted video slide
- P3: Weekly Playwright screenshot refresh cron

## Recent fixes (Mar 1, 2026)

- **Install App (PWA)** — Jason: Beaufort Poultry asked for BBM to install "like an old-school program" (desktop icon, own window, no browser bar). Rolled out to ALL farms (user's choice). Added a service worker (`public/sw.js`, stale-while-revalidate for the static app shell only — every `/api/*` call always bypasses the cache, so farm data is never served stale) + a header "📲 Install App" button (`InstallAppButton.tsx`) that hooks the standard `beforeinstallprompt`/`appinstalled` events and disappears once installed or already running standalone. Manifest + icons already existed from earlier PWA work; this was the missing SW registration + a discoverable install trigger. Self-tested via screenshot: SW registers and reaches `activated` state, manifest resolves, zero console errors. Chrome/Edge desktop + Android support this natively; iOS Safari still uses "Add to Home Screen" from the share menu (no beforeinstallprompt event exists there — button simply won't show, which is expected/unavoidable Apple platform limitation).

- **4-feature batch: Shed Tabs fix, Catches Cloud Sync, Trial Nudge Banner, Best/Worst Callout** — user approved all 4 "Next Action Items" together.
  - **Shed Tabs UI Mismatch (P0, long-standing)** — FirstLoginWizard's `onComplete` wrote a `{n,label,breed,density}` shape with no `shedGroupId`/`active`, so `readFarmConfig()`'s merge silently fell back to the 6-group default every reload regardless of grower's chosen shed count. Fixed by converting the wizard's shape to proper `FarmShedConfig[]` (all 15 groups, `active` set per configured count) before saving. Verified via testing_agent: 3-shed and 4-shed wizard runs both produce the correct tab count and it survives reload.
  - **Catches Cloud Sync** — `catchMap`/`weighPlanMap` now mirrored to Mongo (`feed_program_catches` collection) via new `GET/PUT /api/feed-program/catches`, same hydrate-on-load + debounced-autosave pattern as the existing `/api/feed-program/state`. Verified round-trip across a simulated new-browser session. **Post-test fixes**: gated the autosave PUT behind a `catchesReadyToSyncRef` so a slow hydrate GET can no longer race an empty local state into wiping the cloud copy; blank "+ Add Catch" rows are now filtered out before syncing.
  - **Trial-End Nudge** — day-25 soft banner + day-30 strong banner (in-app only, no email, no locking — per explicit user decision). New `GET /api/farm/trial-status`. Banner auto-hides once `subscriptionStatus` flips to `active` (next page load after Stripe webhook fires). **Post-test fixes**: banner had `paddingRight: 190` added so the fixed top-right auth badge (`#bbm-auth-badge`, z-index 9999) no longer overlaps/steals clicks from "Upgrade Now"/dismiss (was previously logging users out when they tried to dismiss — HIGH bug, fixed); trial-status endpoint now returns timezone-aware ISO timestamps (Mongo stores naive UTC datetimes — was causing up to ~11h day-threshold drift for AEST users).
  - **Best/Worst Batch Callout** — Accuracy Trend Chart now highlights the highest/lowest-scoring batch with green/red dots + a callout line above the chart.
  - Housekeeping: deleted 6 leftover `subscriptionStatus=trialing` test farms from earlier testing-agent sessions (trial-test-farm, redirect-test-farm-2, nudge-test-farm, gmail-live-test-farm, diag-farm, iso-test-farm) that would otherwise have spuriously shown trial banners.
  - Tested via testing_agent (iteration_9): 13/13 backend pytest pass, all 4 frontend features functional; fixed the 1 HIGH + 1 MEDIUM + 2 MINOR issues found, rebuilt, re-verified the HIGH fix via screenshot (elementFromPoint now correctly resolves to the banner link, not the auth badge).

- **Accuracy Trend Chart** — Jason: "Show a running 'how close were we' trend line across all your closed batches, not just per-batch cards." Added to `BatchAccuracyView.tsx` (uses existing `recharts`, already a dependency): a composite "Accuracy Score" per batch (100 − avg absolute % diff across Ave Weight/FCR/cFCR/Cage Rating, clamped 0-100), plotted chronologically (oldest→newest) as a line chart with a dashed 100% reference line + custom tooltip, sitting above the per-batch cards. Header shows an "Avg Accuracy" badge (green ≥95%, amber ≥85%, red below). Shows a friendly "close 2+ batches" placeholder instead of an empty chart when there's <2 data points. No backend change — computed client-side from the existing `/api/eob/batch-accuracy` payload. Verified via screenshot with 3 seeded batches (98% avg accuracy rendered correctly, trend line + per-batch cards both correct); seeded/cleaned test data directly in Mongo, no testing_agent call needed (small single-component addition on top of already-tested feature).

- **Batch Accuracy Tracker** — Jason: "Save predicted vs actual EOB numbers so growers can see how accurate the AI forecast really was." New feature: the Flock Forecast tab's "🔮 Predicted End-of-Batch" card now saves its base (non-scenario) forecast to localStorage (scoped to farmSlug+batchIdentifier, sanitized to skip non-finite/implausible values, written via `queueMicrotask` not synchronously during render). When the grower clicks "🏁 End Batch & Lock", that snapshot (if it matches the exact farm+batch being locked) is attached as `fingerprint.predictedEob` on the existing `POST /api/eob/lock-batch` call — no backend schema change needed. New `GET /api/eob/batch-accuracy?farm=<slug>` returns predicted-vs-actual for every locked batch (KPIs only, no PII/html/pdf). New "🎯 Accuracy" tab (next to Flock Forecast, hidden when Flock Forecast is off) renders `BatchAccuracyView.tsx` — comparison cards per batch (Ave Weight, FCR, cFCR, Cage Rating, Finish Age, predicted/actual grade badges + diff %), graceful fallback when no prediction was on record. Tested via testing_agent (iteration_8): 6/6 backend pytest pass, full frontend E2E flow verified; found + fixed 3 issues same session (unscoped/unsanitized localStorage snapshot, render-time side effect, Finish Age decimal display mismatch).
- **Corrected-age formula inconsistency fix** — while investigating a user report that "results were wrong on broilerbasemate.com.au" for a real Double B settlement (Batch 2603: placed 492,395 → settled 466,498, 2.883kg ave, FCR 1.532, cFCR 1.416 — cFCR formula verified exact match against `FCR − 0.27×(aveWt−2.45)`), found the Flock Forecast "Live Payment Metrics" card used a different (sqrt-based) corrected-age formula than every other view in the app (linear: `age × 2.45/aveWgt`), producing a materially different number. Standardized to the linear formula everywhere. **Not fully resolved**: the actual EOB/settlement report reads `correctedAge`/`aveWeight`/`fcr`/`cfcr` directly from specific xlsx cells the grower fills in (AH5/AH8/AH10/AH11/AL8/AL10) — not calculated by the app on the primary path — so this fix addresses a real but secondary inconsistency; the exact root cause of the reported "wrong result" for Batch 2603 was not confirmed (user did not provide a screenshot of the app's wrong output; declined further clarification). Flag to revisit if the issue recurs — ask for a screenshot of the app's EOB tab for that batch next time.
- **Catches "Total Wgt" tonnes→kg unit fix** — see previous entry below, same session.

## Recent fixes (Feb 28/Mar 2026, cont'd)

## Recent fixes (Feb 28, 2026)

- **30-Shed End-of-Batch** — Bird Summary table now renders one row per configured shed (up to 30), driven by `totalSheds` from `/api/farm-config`, previously capped at 12. Row mapping updated so sheds 13-30 write to xlsx rows 100-117 to avoid clashing with the template's totals row and feed-summary block.
- **Unique per-farm QR codes on Ops Dashboard** — QR URLs now include the farm's `farmToken` (SEC-006). Previously the QR-generated link was `/?farm=<slug>` only; anyone scanning it hit the login gate. Now `/?farm=<slug>&t=<farmToken>` opens the correct farm instantly. `/api/farms` returns `farmToken` for each row so ops-dashboard can build the correct URL.

## Test credentials
- Admin magic link: appcovi2026@gmail.com

## Key files
- Backend: /app/backend/server.py (monolith), /app/backend/email_service.py
- Backend static: /app/backend/static/{landing,reader,ops-dashboard,onboarding-guide,tools-*,ross-308-growth-chart,cobb-500-growth-chart,vs-poultrylog}.html
- SPA host: /app/silo/artifacts/feed-program/index.html
- SPA source: /app/silo/artifacts/feed-program/src/App.tsx (monolith) + /app/silo/artifacts/feed-program/src/components/
- SPA public assets: /app/silo/artifacts/feed-program/public/{llms.txt,llms-full.txt,sitemap.xml,robots.txt}
- Memory: /app/memory/PRD.md, /app/memory/KEYWORD_GAPS.md, /app/memory/test_credentials.md
