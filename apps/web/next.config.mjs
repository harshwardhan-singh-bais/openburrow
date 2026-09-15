/**
 * Next.js configuration.
 *
 * The one non-default decision is `output: "export"` being *available* but not
 * enabled by default, because the app has two deployment shapes:
 *
 *   - **Static** (`OPENBURROW_WEB_STATIC=1`): a folder of HTML you can serve from
 *     anywhere, including a file:// URL. The reel player and the session board
 *     read from the daemon over HTTP, so they work fine without a server.
 *   - **Server** (default): route handlers proxy the daemon and the relay, which
 *     keeps the daemon's token out of the browser.
 *
 * Making the static build a flag rather than a fork of the codebase is what stops
 * the two from drifting.
 */

const isStatic = process.env.OPENBURROW_WEB_STATIC === "1";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  ...(isStatic ? { output: "export", images: { unoptimized: true } } : {}),

  // The daemon and the relay are separate origins in every real deployment, and
  // a browser will not talk to a WebSocket on a different origin without this
  // being reflected in the Content-Security-Policy. Setting it here rather than
  // in a proxy config keeps it next to the code that needs it.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          // A reel is untrusted input rendered in the browser. Framing the app
          // is never something we want, and a permissive CSP for a viewer that
          // renders arbitrary transcripts is how a transcript becomes a script.
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
