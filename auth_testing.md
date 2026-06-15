# Auth Testing Playbook — Broiler Base Mate

## Auth Flow Summary
1. User visits protected page (/, /reader, /ops-dashboard) → page checks `/api/auth/me` → if 401, redirect to `https://auth.emergentagent.com/?redirect=<current-url>`
2. User signs in with Google → returns to original URL with `#session_id=<token>` in fragment
3. Frontend JS posts session_id to `POST /api/auth/exchange-session` (backend) → backend exchanges with Emergent for `session_token` → sets httpOnly cookie → returns user info
4. All subsequent /api/* requests include cookie automatically; backend validates via `get_current_user()` dependency
5. Access scoping rules:
   - `ADMIN_EMAIL=appcovi2026@gmail.com` → super-admin (sees all farms, can access all pages)
   - `farms.ownerEmail` match → can access /, /ops-dashboard (their own farms), /reader?farm=<their-slug>
   - `farm_invites.operatorEmail` match → can ONLY access /reader?farm=<invited-slug>
   - Otherwise: redirected to /landing

## Public (unauthenticated) routes
- `/landing` (sales page)
- `/landing/success` (post-Stripe redirect)
- `POST /api/checkout`, `GET /api/checkout/status/*`, `POST /api/webhook/stripe`
- `POST /api/demo-request`
- `POST /api/auth/exchange-session`, `GET /api/auth/me`, `POST /api/auth/logout`

## Manual Test Setup (skip if testing via real Google login)
```bash
mongosh --eval "
use('test_database');
var userId = 'test-user-' + Date.now();
var sessionToken = 'test_session_' + Date.now();
db.users.insertOne({
  user_id: userId,
  email: 'appcovi2026@gmail.com',  // admin email
  name: 'Test Admin',
  created_at: new Date()
});
db.user_sessions.insertOne({
  user_id: userId,
  session_token: sessionToken,
  expires_at: new Date(Date.now() + 7*24*60*60*1000),
  created_at: new Date()
});
print('Session token: ' + sessionToken);
"
```

## Test Cases
1. **Anonymous landing** — Visit `/landing` → loads without redirect. Verify "Login" link opens recovery modal.
2. **Anonymous /** — Visit `/` → JS detects no session → redirects to `https://auth.emergentagent.com/?redirect=...`
3. **Admin login** — After Google login with `appcovi2026@gmail.com` → can access `/`, `/ops-dashboard`, all `/reader?farm=*` URLs
4. **Operator-only** — User whose email exists ONLY in farm_invites.operatorEmail → can access `/reader?farm=<their-slug>` ONLY; redirected to /landing on `/` or `/ops-dashboard`
5. **Farm owner** — User whose email is in farms.ownerEmail → can access `/`, `/ops-dashboard`, and `/reader?farm=<any-owned-slug>`
6. **Logout** — Click logout → cookie cleared → next request 401 → redirected back to auth
7. **Cross-farm isolation** — Owner of farm A cannot access /reader?farm=B (where they have no access)
