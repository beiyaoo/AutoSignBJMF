(() => {
  const originalFetch = window.fetch.bind(window);
  const loginUrl = "/login?next=" + encodeURIComponent(location.pathname);
  window.siteAuthReady = originalFetch("/api/admin/me", { credentials: "same-origin", cache: "no-store" })
    .then(async response => {
      if (!response.ok) {
        location.assign(loginUrl);
        throw new Error("请先登录网站");
      }
      const session = await response.json();
      window.siteAuthData = session;
      return session;
    });
  window.siteAuthReady.catch(() => {});
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return originalFetch(input, options);
    const session = await window.siteAuthReady;
    const method = (options.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) headers.set("X-CSRF-Token", session.csrf_token);
    const response = await originalFetch(input, { ...options, headers, credentials: "same-origin" });
    if (response.status === 401) location.assign(loginUrl);
    return response;
  };
  document.addEventListener("DOMContentLoaded", () => {
    const logout = document.getElementById("siteLogout");
    if (logout) logout.addEventListener("click", async () => {
      logout.disabled = true;
      try { await fetch("/api/admin/logout", { method: "POST" }); }
      finally { location.assign("/login"); }
    });
  });
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
})();
