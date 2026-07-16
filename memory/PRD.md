# Coop Overwatch — Product Requirements

## Original problem statement
Web-based poultry shed monitoring & control dashboard with a self-learning "Mother Hen AI"
that overwatches every shed 24/7 across multiple vendor systems, tracks full batch history
per breed (Ross 308 / Cobb 500), and takes intelligent corrective action.

## Tech stack
- Frontend: React 19 + Tailwind + Recharts + Phosphor icons + framer-motion + sonner
- Backend: FastAPI + Motor (MongoDB)
- AI: Claude Sonnet 4.5 via Emergent Universal Key (deep reasoning) + local rules engine (offline)

## Architecture snapshot (Phase 1 shipped 2026-02-16)
- MongoDB collections: farms, sheds, batches, readings, alerts, decisions, policies
- Backend: `/api` routes in `server.py`, breed curves in `breed_profiles.py`,
  rules + LLM in `mother_hen.py`, demo seeder in `seed.py`, models in `models.py`
- Frontend: Dashboard, ShedDetail, BatchList, BatchDetail, MotherHenPanel, AlertsPanel, Settings

## Implemented
- [x] Farms / Sheds CRUD (2026-02-16)
- [x] Batches CRUD + close (2026-02-16)
- [x] Reading ingestion API `POST /api/readings` (2026-02-16)
- [x] Dashboard aggregator with traffic-light shed tiles (2026-02-16)
- [x] Breed standards seeded (Ross 308, Cobb 500) + interpolated curves (2026-02-16)
- [x] Mother Hen rules engine — temp/humidity/ammonia/water/mortality/fan (2026-02-16)
- [x] Mother Hen LLM deep analysis (Claude Sonnet 4.5) (2026-02-16)
- [x] Per-action policy engine (auto / recommend / manual) (2026-02-16)
- [x] Decisions with approve/reject + audit log (2026-02-16)
- [x] Alerts with acknowledgement (2026-02-16)
- [x] Batch history charts with breed benchmark overlay (2026-02-16)
- [x] Manual reading form + simulate-reading button (2026-02-16)
- [x] Demo seeder (2 farms, 5 sheds, 5 batches with anomalies injected)

## Backlog / Next
- [ ] P1: CSV import for vendor systems without APIs
- [ ] P1: Side-by-side batch comparison view
- [ ] P1: Post-batch learning loop (adjust Mother Hen thresholds from outcomes)
- [ ] P2: Email / SMS / WhatsApp alerts (Twilio + Resend)
- [ ] P2: Mobile companion app
- [ ] P2: Multi-user auth for hub SaaS launch
- [ ] P3: Predictive end-of-batch forecasting
