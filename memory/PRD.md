# Broiler Base Mate — Product Requirements Document
Last updated: Feb 25, 2026

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
