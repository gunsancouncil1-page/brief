interface Env {
  ORIGIN_URL: string;
  ORIGIN_SECRET: string;
}

const REMOVE_REQUEST_HEADERS = [
  "host",
  "cf-connecting-ip",
  "cf-ipcountry",
  "cf-ray",
  "x-forwarded-for",
  "x-forwarded-proto",
  "x-gunsan-origin-key",
];

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const incoming = new URL(request.url);
    const origin = new URL(env.ORIGIN_URL);
    const target = new URL(`${incoming.pathname}${incoming.search}`, origin);
    const headers = new Headers(request.headers);
    for (const name of REMOVE_REQUEST_HEADERS) headers.delete(name);
    headers.set("X-Gunsan-Origin-Key", env.ORIGIN_SECRET);
    headers.set("X-Forwarded-Proto", "https");

    const init: RequestInit = {
      method: request.method,
      headers,
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") init.body = request.body;

    try {
      const response = await fetch(target, init);
      const responseHeaders = new Headers(response.headers);
      // This dashboard contains private, changing reports; prevent edge/browser caching.
      responseHeaders.set("Cache-Control", "no-store");
      return new Response(response.body, { status: response.status, headers: responseHeaders });
    } catch {
      return new Response("The local briefing server is unavailable.", { status: 503 });
    }
  },
} satisfies ExportedHandler<Env>;

