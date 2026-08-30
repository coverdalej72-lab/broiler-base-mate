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

- P2: Custom-domain email sender (switch Resend from `onboarding@resend.dev` to `jason@appcovi.com.au` via DNS)
- P3: Refactor server.py and App.tsx into smaller modules (both are monolithic)
- P3: Multi-worker uvicorn + CDN for 10k+ user scale
- P3: Live price ticker in Ops builder
- P3: Day-3 no-login founder alert
- P3: Carousel video demo — 20-sec docket-scan → silo-update autoplay muted video slide
- P3: Weekly Playwright screenshot refresh cron

## Test credentials
- Admin magic link: appcovi2026@gmail.com

## Key files
- Backend: /app/backend/server.py (monolith), /app/backend/email_service.py
- Backend static: /app/backend/static/{landing,reader,ops-dashboard,onboarding-guide,tools-*,ross-308-growth-chart,cobb-500-growth-chart,vs-poultrylog}.html
- SPA host: /app/silo/artifacts/feed-program/index.html
- SPA source: /app/silo/artifacts/feed-program/src/App.tsx (monolith) + /app/silo/artifacts/feed-program/src/components/
- SPA public assets: /app/silo/artifacts/feed-program/public/{llms.txt,llms-full.txt,sitemap.xml,robots.txt}
- Memory: /app/memory/PRD.md, /app/memory/KEYWORD_GAPS.md, /app/memory/test_credentials.md
