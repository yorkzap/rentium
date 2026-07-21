# Prompt: make `<slug>.rentium.ca` work via a Cloudflare Worker

Hand the section below to an AI that has access to the Cloudflare dashboard (or
`wrangler`). It fully solves the landlord vanity-subdomain problem without moving
nameservers off Cloudflare.

---

## PROMPT (copy from here)

You are setting up a **Cloudflare Worker** so that `https://<slug>.rentium.ca`
serves a landlord's public showcase page. Implement and deploy it.

### Context / constraints (do not change these)
- DNS is on **Cloudflare**. `rentium.ca` and `www` point to **Vercel** (the
  Next.js frontend). `api.rentium.ca` is a **Cloudflare Tunnel** to a Docker
  backend. Nameservers CANNOT move to Vercel (the tunnel needs Cloudflare), so
  Vercel wildcard domains are not an option — that's why `*.rentium.ca` on Vercel
  showed "Invalid Configuration" and the browser got **SSL error 525**.
- Cloudflare Universal SSL already covers the first label `*.rentium.ca`, so
  TLS terminates fine at Cloudflare's edge — we just need the edge to serve the
  right content instead of failing to reach an origin.
- The Next.js app already renders each showcase at the path **`/l/<slug>`** on
  the apex (`https://rentium.ca/l/<slug>`), with `rel=canonical` pointing there.
  So the Worker's job is: for a vanity host, **serve the apex `/l/<slug>` page and
  proxy its assets**, and send every other path to the apex.

### Behaviour to implement
For a request to `https://<slug>.rentium.ca<path>`:
1. Compute `slug` = the first DNS label of the Host.
2. If `slug` is in the RESERVED set below, or the host isn't `*.rentium.ca`,
   pass the request through unchanged (`fetch(request)`).
3. Otherwise proxy to the apex origin `https://rentium.ca`:
   - If `path === "/"` → fetch `https://rentium.ca/l/<slug>` and return it
     (this is the showcase page).
   - For **any other path** (assets like `/_next/*`, `/favicon.ico`, images,
     etc.) → fetch the **same path** from `https://rentium.ca<path><query>` and
     return it, so the page's absolute-path assets load correctly.
   - Preserve method/headers/body; use `redirect: "manual"`.
4. (Optional nicety) If you want listing/pricing/app links to live on the apex,
   you may 301 non-root, non-asset *navigation* requests to
   `https://rentium.ca<path>`; but the simple "proxy everything to apex" above is
   correct and lower-risk — start with that.

RESERVED (never treat as a landlord slug) — keep in sync with the frontend's
`middleware.ts` `RESERVED_SUBDOMAINS` and the backend `showcase.models.RESERVED_SLUGS`:
`www, api, app, about, auth, blog, contact, dashboard, help, invite, l, legal,
login, logout, mail, media, pricing, privacy, public, rentium, settings, signup,
sitemap, static, support, terms, viewing` and every province code
`ab, bc, mb, nb, nl, ns, nt, nu, on, pe, qc, sk, yt`.

### Reference implementation (Worker, module syntax)
```js
const APEX = "https://rentium.ca";
const RESERVED = new Set([
  "www","api","app","about","auth","blog","contact","dashboard","help","invite",
  "l","legal","login","logout","mail","media","pricing","privacy","public",
  "rentium","settings","signup","sitemap","static","support","terms","viewing",
  "ab","bc","mb","nb","nl","ns","nt","nu","on","pe","qc","sk","yt",
]);

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const host = (request.headers.get("host") || url.hostname).toLowerCase();

    if (!host.endsWith(".rentium.ca")) return fetch(request);
    const slug = host.slice(0, -".rentium.ca".length);
    if (!slug || slug.includes(".") || RESERVED.has(slug)) return fetch(request);

    const path = url.pathname === "/" ? `/l/${slug}` : url.pathname;
    const target = APEX + path + url.search;

    const resp = await fetch(target, {
      method: request.method,
      headers: request.headers,
      body: ["GET","HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    });
    // Pass the origin response straight back (status, headers, body).
    return new Response(resp.body, resp);
  },
};
```

### Deploy
Either the dashboard or wrangler:

**Dashboard:** Workers & Pages → Create Worker → paste the code → Deploy. Then
Workers Routes (on the `rentium.ca` zone) → add route **`*.rentium.ca/*`** → this
Worker. Ensure the `*.rentium.ca` DNS record exists and is **Proxied** (orange
cloud) so the route runs. Set SSL/TLS mode to **Full**.

**wrangler:** create `wrangler.toml` with `name`, `main`, `compatibility_date`,
and:
```toml
routes = [{ pattern = "*.rentium.ca/*", zone_name = "rentium.ca" }]
```
then `npx wrangler deploy`.

### Verify
- `https://raj.rentium.ca/` → the raj showcase (no 525, no Vercel error).
- `https://raj.rentium.ca/_next/...` assets return 200 (page fully styled).
- A reserved host like `www.rentium.ca` is unaffected.
- `api.rentium.ca` (the tunnel) is unaffected.

### Cleanup
In Vercel → Domains, **remove `*.rentium.ca`** (it will keep showing "Invalid
Configuration" and is no longer used — the Worker handles the wildcard now).

## (end prompt)
