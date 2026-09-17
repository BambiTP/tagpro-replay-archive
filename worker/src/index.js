/**
 * Holds the current address of each archive host.
 *
 * A host is exposed through a free cloudflared quick tunnel, which is handed
 * a fresh *.trycloudflare.com hostname every time it starts. A static site
 * cannot link to something that moves, so the tunnel supervisor registers the
 * hostname here each time it changes and the site links to /go instead, which
 * redirects. The redirect carries the path through, so a link or a script
 * written against /go/... keeps working across a rotation.
 *
 * Redirect, never proxy: the recordings are gigabytes and none of that should
 * pass through a Worker.
 *
 * The registry is a Durable Object rather than KV deliberately. KV's read
 * cache has a 60s floor, so for up to a minute after a rotation different
 * edges disagree about where the host is - /go would redirect to the new
 * hostname while /status still reported the host offline. A single DO is
 * strongly consistent, and one small object serving a handful of requests a
 * minute is nothing.
 *
 * More than one host registers here now, each in its own object, reached
 * through a /h/<host>/ prefix. See HOSTS.
 */
import { DurableObject } from "cloudflare:workers";

// The supervisor re-registers every 60s. Three missed heartbeats and we call
// the host down rather than hand out a hostname that no longer answers.
const STALE_S = 300;

// Every host keeps its own record, so two supervisors heartbeating a minute
// apart cannot overwrite each other. Addressed as /h/<host>/register,
// /h/<host>/go/..., /h/<host>/status, /h/<host>/stats.
//
// An unprefixed path stays the tagpro host, which was here first: its records
// live under "v1" and the links already published against /go and /status have
// to keep resolving exactly as they did before any of this was keyed.
const HOSTS = {
  tagpro: { key: "v1", site: "https://bambitp.github.io/tagpro-replay-archive/" },
  naltp: { key: "naltp", site: "https://bambitp.github.io/naltp-archive/" },
};
const DEFAULT_HOST = HOSTS.tagpro;

export class TunnelRegistry extends DurableObject {
  /**
   * Clicks on /go links, counted here rather than only on the host: this sees
   * a link followed while the host is down, and a link shared somewhere else
   * entirely. The host's own /stats.json is the authority on bytes actually
   * transferred - these two answer different questions and will not agree.
   */
  async hit(kind) {
    const day = new Date().toISOString().slice(0, 10);
    const keys = ["hits:total", `hits:kind:${kind}`, `hits:day:${day}`];
    const have = await this.ctx.storage.get(keys);
    const put = {};
    for (const k of keys) put[k] = (have.get(k) || 0) + 1;
    await this.ctx.storage.put(put);
  }

  async counts() {
    const all = await this.ctx.storage.list({ prefix: "hits:" });
    const out = { total: 0, by_kind: {}, by_day: {} };
    for (const [k, v] of all) {
      if (k === "hits:total") out.total = v;
      else if (k.startsWith("hits:kind:")) out.by_kind[k.slice(10)] = v;
      else if (k.startsWith("hits:day:")) out.by_day[k.slice(9)] = v;
    }
    return out;
  }

  async current() {
    return (await this.ctx.storage.get("current")) || null;
  }

  async set(url, totals) {
    const rec = { url, updated: new Date().toISOString() };
    await this.ctx.storage.put("current", rec);
    // Totals ride in on the heartbeat and are kept separately, so the site can
    // still show them while the host is down.
    if (totals) await this.ctx.storage.put("totals", totals);
    return rec;
  }

  async totals() {
    return (await this.ctx.storage.get("totals")) || null;
  }

  async clear() {
    await this.ctx.storage.delete("current");
  }
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
    },
  });

const registry = (env, host) => env.REGISTRY.get(env.REGISTRY.idFromName(host.key));

// Splits /h/<host>/rest into the host and the route it was asking for, and
// leaves an unprefixed path alone as the default host's. An unknown host name
// is a 404 rather than a fresh empty registry, so a typo or a probe cannot
// park a record here.
function route(pathname) {
  const m = pathname.match(/^\/h\/([a-z0-9-]{1,32})(\/.*)?$/);
  if (!m) return { host: DEFAULT_HOST, path: pathname };
  const host = HOSTS[m[1]];
  if (!host) return null;
  return { host, path: m[2] || "/" };
}

function withAge(rec) {
  if (!rec || !rec.url) return null;
  const age = Math.max(0, Math.round((Date.now() - Date.parse(rec.updated)) / 1000));
  return { ...rec, age, online: age < STALE_S };
}

function authorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  const enc = new TextEncoder();
  const a = enc.encode(token);
  const b = enc.encode(env.TUNNEL_SECRET || "");
  // Length check first - timingSafeEqual throws on a length mismatch.
  if (a.byteLength === 0 || a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

function validOrigin(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  // Only a quick tunnel, only over TLS. Anything else registered here would
  // turn /go into an open redirect.
  if (u.protocol !== "https:") return null;
  if (u.hostname !== "trycloudflare.com" && !u.hostname.endsWith(".trycloudflare.com")) return null;
  return u.origin;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
          "Access-Control-Allow-Headers": "Authorization,Content-Type",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    const hit = route(url.pathname);
    if (!hit) return json({ error: "not found" }, 404);
    const { host, path: pathname } = hit;
    const reg = registry(env, host);

    if (pathname === "/register") {
      if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);

      if (request.method === "DELETE") {
        await reg.clear();
        return json({ ok: true, online: false });
      }
      if (request.method !== "POST") return json({ error: "method not allowed" }, 405);

      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "bad json" }, 400);
      }
      const origin = validOrigin(body && body.url);
      if (!origin) return json({ error: "url must be an https *.trycloudflare.com origin" }, 400);

      const totals =
        Number.isFinite(body.downloads) && Number.isFinite(body.bytes)
          ? { downloads: body.downloads, bytes: body.bytes, at: new Date().toISOString() }
          : null;
      const rec = await reg.set(origin, totals);
      return json({ ok: true, ...rec });
    }

    if (pathname === "/go" || pathname.startsWith("/go/")) {
      const rec = withAge(await reg.current());
      if (!rec || !rec.online) {
        return new Response(
          "The archive host is not online right now.\n\n" +
            "It is a home machine behind a tunnel, so it is up when it is up. " +
            "Coverage data is always available at\n" +
            host.site + "\n",
          { status: 503, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } }
        );
      }
      const rest = pathname.slice("/go".length); // "" or "/something"
      // Counted off the critical path - a redirect should not wait on a write.
      const kind = (rest.split("/")[1] || "root").slice(0, 32);
      ctx.waitUntil(reg.hit(kind));
      return new Response(null, {
        status: 302,
        headers: {
          Location: rec.url + rest + url.search,
          "Cache-Control": "no-store",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    if (pathname === "/stats") {
      const [clicks, totals] = await Promise.all([reg.counts(), reg.totals()]);
      return json({ served: totals, clicks });
    }

    if (pathname === "/" || pathname === "/status") {
      const [rec, totals] = await Promise.all([reg.current().then(withAge), reg.totals()]);
      if (!rec) {
        return json({ online: false, url: null, updated: null, age: null, served: totals });
      }
      return json({
        online: rec.online,
        url: rec.online ? rec.url : null,
        updated: rec.updated,
        age: rec.age,
        // What the host has served, carried in on its heartbeat, so this is
        // here whether or not the host is up right now.
        served: totals,
      });
    }

    return json({ error: "not found" }, 404);
  },
};
