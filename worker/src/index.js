/**
 * Holds the current address of the archive host.
 *
 * The host is exposed through a free cloudflared quick tunnel, which is handed
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
 */
import { DurableObject } from "cloudflare:workers";

// The supervisor re-registers every 60s. Three missed heartbeats and we call
// the host down rather than hand out a hostname that no longer answers.
const STALE_S = 300;

export class TunnelRegistry extends DurableObject {
  async current() {
    return (await this.ctx.storage.get("current")) || null;
  }

  async set(url) {
    const rec = { url, updated: new Date().toISOString() };
    await this.ctx.storage.put("current", rec);
    return rec;
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

const registry = (env) => env.REGISTRY.get(env.REGISTRY.idFromName("v1"));

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
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

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

    if (pathname === "/register") {
      if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);

      if (request.method === "DELETE") {
        await registry(env).clear();
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

      const rec = await registry(env).set(origin);
      return json({ ok: true, ...rec });
    }

    if (pathname === "/go" || pathname.startsWith("/go/")) {
      const rec = withAge(await registry(env).current());
      if (!rec || !rec.online) {
        return new Response(
          "The archive host is not online right now.\n\n" +
            "It is a home machine behind a tunnel, so it is up when it is up. " +
            "Coverage data is always available at\n" +
            "https://bambitp.github.io/tagpro-replay-archive/\n",
          { status: 503, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } }
        );
      }
      const rest = pathname.slice("/go".length); // "" or "/something"
      return new Response(null, {
        status: 302,
        headers: {
          Location: rec.url + rest + url.search,
          "Cache-Control": "no-store",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    if (pathname === "/" || pathname === "/status") {
      const rec = withAge(await registry(env).current());
      if (!rec) return json({ online: false, url: null, updated: null, age: null });
      return json({
        online: rec.online,
        url: rec.online ? rec.url : null,
        updated: rec.updated,
        age: rec.age,
      });
    }

    return json({ error: "not found" }, 404);
  },
};
