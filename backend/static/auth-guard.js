/* Broiler Base Mate — page-level auth guard.
 *
 * Include this BEFORE any other JS that calls /api/* endpoints.
 *
 * Behaviour:
 *  - On load: process #session_id=... if present (exchange for cookie), then reload clean.
 *  - GET /api/auth/me with credentials. If 401: redirect to Emergent OAuth.
 *  - Once authed: stores user info on window.__BBM_AUTH__ for the page to consume.
 *  - Verifies the user has access to the current farm slug (if any) — bounces to /landing if not.
 *  - If user is operator-only and page is NOT /reader, bounces to their /reader URL.
 *  - Adds a small "👤 user · Logout" badge in the top-right corner.
 *
 * REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
 */
(function () {
  if (window.__BBM_AUTH_GUARD_STARTED__) return;
  window.__BBM_AUTH_GUARD_STARTED__ = true;

  // /morts-entry is a fully self-contained, no-login staff page (Staff QR
  // flow) — it makes its own auth decision by calling /api/farm-config with
  // its own ?t= token and shows a graceful "link expired" message on a bad
  // token. This guard must NOT run at all here: it was racing the SPA
  // shell's static-page fetch (index.html) and hard-redirecting anonymous
  // staff visitors to /landing/OAuth before morts-entry.html's own error
  // branch ever got a chance to render. Found via testing_agent iteration_19.
  if (/^\/morts-entry(\/|$|\?)/.test(window.location.pathname)) return;

  const PROTECTED_PAGE = (window.__BBM_PAGE__ || document.body && document.body.dataset && document.body.dataset.page) || "app";
  // Preview environment: the /reader route is bootstrapped by the SPA which sets
  // __BBM_PAGE__ = "app" — so also detect reader from the URL path so guards work.
  const IS_READER_URL = /^\/reader(\/|$|\?)/.test(window.location.pathname);
  // Sep 2026 — Jason: "installing the app taking long time [never finishes]".
  // This used to hardcode "default" when the URL had no ?farm= — exactly the
  // case on a re-opened installed PWA icon (manifest start_url has no query
  // params). For any farm slug other than literally "default", that made
  // SAVED_FARM_TOKEN below always fail its slug match, so HAS_FARM_TOKEN was
  // always false and every re-open bounced to /landing before the page's own
  // script even ran. Now falls back to the slug saved on the original scan,
  // same pattern as the Program's own getActiveFarmSlug().
  const FARM_SLUG = (function () {
    try { return new URLSearchParams(window.location.search).get("farm") || localStorage.getItem("bbm-farm-slug") || "default"; }
    catch { return "default"; }
  })();
  // SEC-006 scan-and-go: a valid ?t=<farmToken> in the URL (or saved from an
  // earlier visit to this same farm) means the grower doesn't need a login
  // session at all — the backend validates the token on every /api/* call.
  // Without this, every QR/link visitor with no session cookie was being
  // bounced to /landing (or OAuth) before the page ever got a chance to use
  // their token — silently breaking every farm's QR code. Fixed Mar 2026.
  const URL_FARM_TOKEN = (function () {
    try { return new URLSearchParams(window.location.search).get("t") || ""; }
    catch { return ""; }
  })();
  const SAVED_FARM_TOKEN = (function () {
    try {
      return (localStorage.getItem("bbm-farm-slug") === FARM_SLUG && localStorage.getItem("bbm-farm-token")) || "";
    } catch { return ""; }
  })();
  const HAS_FARM_TOKEN = !!(URL_FARM_TOKEN || SAVED_FARM_TOKEN);

  function emergentLoginUrl() {
    // REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
    const redirect = window.location.origin + window.location.pathname + window.location.search;
    return "https://auth.emergentagent.com/?redirect=" + encodeURIComponent(redirect);
  }

  // For anonymous visitors: homepage (PROTECTED_PAGE === "app") sends them to the marketing
  // landing page (better first-time UX than dropping them into a Google login). Other protected
  // pages (/reader, /ops-dashboard) bounce directly to OAuth because customers reach those via
  // email links and bookmarks — they came intending to log in.
  function anonymousBounceUrl() {
    return PROTECTED_PAGE === "app" ? "/landing" : emergentLoginUrl();
  }

  async function exchangeSessionId(sid) {
    const r = await fetch("/api/auth/exchange-session", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid }),
    });
    if (!r.ok) throw new Error("Auth exchange failed: " + r.status);
    return r.json();
  }

  async function fetchMe() {
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.status === 401) return null;
    if (!r.ok) throw new Error("/auth/me failed " + r.status);
    return r.json();
  }

  function renderBadge(info) {
    if (document.getElementById("bbm-auth-badge")) return;
    const u = info.user;
    const wrap = document.createElement("div");
    wrap.id = "bbm-auth-badge";
    // Compact pill — was 180+ px wide which overlapped the Save / Settings
    // buttons in the Feed Program's top header. Now ~80 px: avatar + "Logout"
    // text only. Full name shown on hover via the `title` attribute. Click the
    // avatar to expand the full name + admin badge.
    wrap.style.cssText = "position:fixed;top:8px;right:10px;z-index:9999;background:rgba(15,61,36,0.96);color:#fff;padding:5px 9px;border-radius:99px;display:flex;align-items:center;gap:7px;font:700 11px system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.25);border:1px solid rgba(201,162,39,.4);cursor:pointer;transition:padding 0.2s ease;";
    const isAdmin = info.role === "admin";
    const displayName = u.name || u.email || "User";
    const firstChar = (displayName[0] || "?").toUpperCase();
    wrap.title = displayName + (isAdmin ? " · ADMIN" : "");
    const pic = u.picture
      ? `<img src="${u.picture}" alt="" style="width:20px;height:20px;border-radius:50%;border:1px solid #C9A227;flex-shrink:0;">`
      : `<span style="width:20px;height:20px;border-radius:50%;background:#C9A227;color:#000;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:10px;flex-shrink:0;">${firstChar}</span>`;
    // Name span is hidden by default — only shows when the user hovers/taps
    // the badge, OR when the viewport is wide enough to comfortably fit it.
    wrap.innerHTML = `${pic}<span id="bbm-name" style="color:#bdd3c4;max-width:0;overflow:hidden;white-space:nowrap;transition:max-width 0.2s ease,margin 0.2s ease;">${displayName}${isAdmin ? ' <span style="color:#C9A227;font-weight:800;">· ADMIN</span>' : ''}</span><a href="#" data-testid="bbm-logout" id="bbm-logout" style="color:#C9A227;text-decoration:none;font-weight:800;padding-left:7px;border-left:1px solid rgba(255,255,255,.2);">Logout</a>`;
    document.body.appendChild(wrap);
    // Expand on hover (desktop) or tap (mobile)
    const nameSpan = wrap.querySelector("#bbm-name");
    const expand = () => { nameSpan.style.maxWidth = "180px"; nameSpan.style.marginLeft = "2px"; };
    const collapse = () => { nameSpan.style.maxWidth = "0"; nameSpan.style.marginLeft = "0"; };
    wrap.addEventListener("mouseenter", expand);
    wrap.addEventListener("mouseleave", collapse);
    wrap.addEventListener("click", (e) => {
      if (e.target.id === "bbm-logout") return;
      // Tap to toggle on mobile
      if (nameSpan.style.maxWidth === "0px" || !nameSpan.style.maxWidth) expand();
      else collapse();
    });
    document.getElementById("bbm-logout").addEventListener("click", async (e) => {
      e.preventDefault();
      try { await fetch("/api/auth/logout", { method: "POST", credentials: "include" }); } catch (_) {}
      window.location.href = "/landing";
    });
  }

  function bounceTo(url) { window.location.replace(url); }

  function enforceAccess(info) {
    const ownedSlugs = new Set((info.owned || []).map(f => f.slug));
    const invitedSlugs = new Set((info.invited || []).map(f => f.slug));
    const allSlugs = new Set([...ownedSlugs, ...invitedSlugs]);
    window.__BBM_AUTH__ = info;
    window.__BBM_ALL_SLUGS__ = Array.from(allSlugs);

    // Shared "does this page even care which farm we're on" check — used by
    // both the admin and owner branches below. None of these marketing/info
    // pages should ever trigger a farm-slug bounce.
    const INFO_PAGES = ["/landing", "/landing/success", "/terms", "/privacy", "/security", "/onboarding-guide", "/tools/fcr-calculator", "/tools/grower-payment-calculator", "/tools/silo-capacity-calculator", "/ross-308-growth-chart", "/cobb-500-growth-chart", "/vs/poultrylog"];
    const CURRENT_PATH = window.location.pathname.replace(/\/$/, "") || "/";
    const IS_INFO_PAGE = INFO_PAGES.indexOf(CURRENT_PATH) !== -1 || CURRENT_PATH.indexOf("/guides/") === 0;
    const FARM_SCOPED_PATH = !IS_INFO_PAGE;

    // Admin: full access to every farm (support/ops tooling) — BUT Jason's
    // own account is also a real farm owner via SUPERUSER_EMAILS, and this
    // used to `return` unconditionally with zero farm-slug awareness. That's
    // very likely the actual "staff QR not coming back to the Program" bug:
    // his own browser silently defaulting to "default" (or a stale cached
    // slug) with NO self-correction at all, while his Staff QR (generated
    // from whatever farm his browser happened to be on) pointed elsewhere.
    // Fix: only when there's no EXPLICIT ?farm= in the URL (i.e. relying on
    // the ambient fallback) and he owns at least one farm himself, snap to
    // his own farm instead of trusting the fallback. An explicit ?farm=X in
    // the URL (checking a customer's farm for support) is never touched.
    if (info.role === "admin") {
      const myOwnSlugs = new Set((info.myOwnFarms || []).map(f => f.slug));
      const hasExplicitFarmParam = new URLSearchParams(window.location.search).has("farm");
      if (FARM_SCOPED_PATH && !hasExplicitFarmParam && myOwnSlugs.size > 0 && !myOwnSlugs.has(FARM_SLUG)) {
        const slug = myOwnSlugs.values().next().value;
        const params = new URLSearchParams(window.location.search);
        params.set("farm", slug);
        try { localStorage.setItem("bbm-farm-slug", slug); } catch (_) {}
        return bounceTo(window.location.pathname + "?" + params.toString());
      }
      return;
    }

    // Operator-only (no owned farms): only allowed on /reader
    if (info.role === "operator") {
      if (PROTECTED_PAGE !== "reader") {
        const slug = invitedSlugs.values().next().value;
        return bounceTo("/reader?farm=" + encodeURIComponent(slug));
      }
      if (!invitedSlugs.has(FARM_SLUG)) {
        const slug = invitedSlugs.values().next().value;
        return bounceTo("/reader?farm=" + encodeURIComponent(slug));
      }
      return;
    }

    // Owner: can access pages, but only their own farms
    if (info.role === "owner") {
      // Sep 2026 — Jason: "staff qr code but nothing coming back to main
      // program." Root cause: this used to SKIP the mismatch check whenever
      // FARM_SLUG resolved to the literal fallback "default" (no ?farm= in
      // the URL and nothing cached in localStorage yet — e.g. a fresh
      // browser/device, or cache cleared). That let an owner silently sit
      // on a "default" farm they don't actually own/that may not exist,
      // while their real farm (and their staff's QR, which always carries
      // the correct farm=) was a totally different slug — so staff entries
      // landed in the right place, but the owner was quietly looking at the
      // wrong one and never knew it. Now "default" gets the exact same
      // ownership check as any other slug.
      //
      // Sep 2026 (part 2) — Jason: "log in and out its a bit glitchy".
      // __BBM_PAGE__ is hardcoded "app" for EVERY page this shell serves,
      // including /landing, /privacy, /terms, /security etc — none of which
      // care about farm context at all. The check above was firing there
      // too, so logging in FROM /landing (the normal flow) could immediately
      // bounce the freshly-logged-in owner to "/landing?farm=<realSlug>"
      // instead of into their actual Program. Fix: only SKIP this check on
      // known marketing/informational pages; everything else (the Program
      // at "/", the post-login redirect target "/feed-program", /reader,
      // and any future app route) stays farm-scoped by default — safer than
      // an allowlist of just "/", which would have missed "/feed-program".
      if (FARM_SCOPED_PATH && FARM_SLUG && !allSlugs.has(FARM_SLUG)) {
        // Default to their first owned farm
        const slug = ownedSlugs.values().next().value || "default";
        if (slug !== FARM_SLUG) {
          const params = new URLSearchParams(window.location.search);
          params.set("farm", slug);
          try { localStorage.setItem("bbm-farm-slug", slug); } catch (_) {}
          return bounceTo(window.location.pathname + "?" + params.toString());
        }
      }
      return;
    }

    // No farms at all → for the marketing front door (PROTECTED_PAGE === "app")
    // we send them to /landing (probably a Google user with no plan yet).
    // For ANY internal app page (reader, ops-dashboard, ops-outreach, admin)
    // we send them to Emergent OAuth login — paying customers should NEVER be
    // bounced to the marketing site mid-session.
    bounceTo(anonymousBounceUrl());
  }

  async function init() {
    // 1) Process session_id fragment if present (one-time, then strip and continue)
    if (window.location.hash && window.location.hash.includes("session_id=")) {
      try {
        const sid = new URLSearchParams(window.location.hash.slice(1)).get("session_id");
        if (sid) {
          await exchangeSessionId(sid);
          // Strip the fragment from URL (clean reload)
          const clean = window.location.pathname + window.location.search;
          window.history.replaceState(null, "", clean);
        }
      } catch (e) {
        console.error("[auth] session exchange failed:", e);
        return bounceTo(anonymousBounceUrl());
      }
    }

    // 2) Check existing session
    let info;
    try { info = await fetchMe(); } catch (e) { info = null; }
    if (!info) {
      // Anonymous but holding a valid farm QR/link token — let the page load
      // as a guest. The SPA's own fetch interceptor (index.html) attaches
      // ?t=<token> to every /api/* call, which the backend validates per-farm.
      if (HAS_FARM_TOKEN && (PROTECTED_PAGE === "app" || IS_READER_URL)) {
        document.dispatchEvent(new CustomEvent("bbm-auth-ready", { detail: null }));
        return;
      }
      return bounceTo(anonymousBounceUrl());
    }

    // 3) Enforce role-based access (may bounce)
    enforceAccess(info);

    // 4) Render the user badge — hidden on /reader (mobile). Logout now lives
    //    in Settings tab per Jason (Feb 2026) to keep the workspace clean.
    if (PROTECTED_PAGE !== "reader" && !IS_READER_URL) renderBadge(info);
    document.dispatchEvent(new CustomEvent("bbm-auth-ready", { detail: info }));
  }

  // Run as early as possible; the page should also wait for `bbm-auth-ready` event
  // before showing any sensitive data.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  // ── Production safety nets ───────────────────────────────────────
  // (a) Report unhandled JS errors to the backend so the owner sees them.
  // (b) Intercept fetch() to detect mid-session 401s on /api/* and gracefully re-auth.
  // Both are throttled client-side so a misbehaving page can't spam the server.

  const reported = new Set();
  function reportJsError(payload) {
    const sig = (payload.message || "") + "|" + (payload.url || "");
    if (reported.has(sig)) return;
    reported.add(sig);
    if (reported.size > 50) { reported.clear(); } // cap memory
    try {
      navigator.sendBeacon
        ? navigator.sendBeacon("/api/error-report", new Blob([JSON.stringify(payload)], { type: "application/json" }))
        : fetch("/api/error-report", {
            method: "POST", credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            keepalive: true,
          }).catch(() => {});
    } catch (_) {}
  }

  window.addEventListener("error", e => {
    if (!e || !e.message) return;
    reportJsError({
      message: String(e.message).slice(0, 500),
      stack:   e.error && e.error.stack ? String(e.error.stack).slice(0, 1500) : "",
      url:     (e.filename || "") + ":" + (e.lineno || ""),
      page:    PROTECTED_PAGE,
    });
  });

  window.addEventListener("unhandledrejection", e => {
    const r = e && e.reason;
    if (!r) return;
    reportJsError({
      message: String(r.message || r).slice(0, 500),
      stack:   r.stack ? String(r.stack).slice(0, 1500) : "",
      url:     window.location.href,
      page:    PROTECTED_PAGE,
    });
  });

  // Fetch interceptor: if any /api/* call returns 401 mid-session, prompt re-login.
  // Avoids confusing silent failures (e.g. user idle for hours, cookie expires).
  let _401HandledAt = 0;
  const _origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await _origFetch(input, init);
    try {
      const urlStr = typeof input === "string" ? input : (input && input.url) || "";
      if (res && res.status === 401 && urlStr.includes("/api/")
          && !urlStr.includes("/api/auth/")
          && !urlStr.includes("/api/error-report")) {
        const now = Date.now();
        if (now - _401HandledAt > 5000) { // debounce
          _401HandledAt = now;
          // Show a quick toast then bounce to login keeping the user on their current page
          try {
            const t = document.createElement("div");
            t.style.cssText = "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:99999;background:#0f3d24;color:#fff;padding:10px 18px;border-radius:99px;font:600 13px system-ui,sans-serif;box-shadow:0 6px 22px rgba(0,0,0,.3);";
            t.textContent = "Your session expired — redirecting to log in…";
            document.body.appendChild(t);
          } catch (_) {}
          setTimeout(() => { window.location.replace(emergentLoginUrl()); }, 1400);
        }
      }
    } catch (_) {}
    return res;
  };
})();
