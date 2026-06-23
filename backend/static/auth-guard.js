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

  const PROTECTED_PAGE = (window.__BBM_PAGE__ || document.body && document.body.dataset && document.body.dataset.page) || "app";
  const FARM_SLUG = (function () {
    try { return new URLSearchParams(window.location.search).get("farm") || "default"; }
    catch { return "default"; }
  })();

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
    wrap.style.cssText = "position:fixed;top:8px;right:10px;z-index:9999;background:rgba(15,61,36,0.96);color:#fff;padding:6px 10px;border-radius:99px;display:flex;align-items:center;gap:8px;font:600 12px system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.25);border:1px solid rgba(201,162,39,.4);";
    const pic = u.picture
      ? `<img src="${u.picture}" alt="" style="width:22px;height:22px;border-radius:50%;border:1px solid #C9A227;">`
      : `<span style="width:22px;height:22px;border-radius:50%;background:#C9A227;color:#000;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:10px;">${(u.name||u.email||"?")[0].toUpperCase()}</span>`;
    const isAdmin = info.role === "admin";
    wrap.innerHTML = `${pic}<span style="color:#bdd3c4;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${u.name || u.email}${isAdmin ? ' <span style="color:#C9A227;font-weight:800;">· ADMIN</span>' : ''}</span><a href="#" data-testid="bbm-logout" id="bbm-logout" style="color:#C9A227;text-decoration:none;font-weight:800;padding-left:6px;border-left:1px solid rgba(255,255,255,.2);">Logout</a>`;
    document.body.appendChild(wrap);
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

    // Admin: full access
    if (info.role === "admin") return;

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
      if (FARM_SLUG && FARM_SLUG !== "default" && !allSlugs.has(FARM_SLUG)) {
        // Default to their first owned farm
        const slug = ownedSlugs.values().next().value || "default";
        const params = new URLSearchParams(window.location.search);
        params.set("farm", slug);
        return bounceTo(window.location.pathname + "?" + params.toString());
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
    if (!info) { return bounceTo(anonymousBounceUrl()); }

    // 3) Enforce role-based access (may bounce)
    enforceAccess(info);

    // 4) Render the user badge
    renderBadge(info);
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
