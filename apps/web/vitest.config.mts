import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest configuration for the frontend.
 *
 * `environment: "node"` is the default, and the reason is that almost everything
 * worth testing here is not a component: the route handlers, the reel parser and
 * the path-safety check are all plain TypeScript. A jsdom document costs a second
 * or two per file and buys those suites nothing. The component suites opt in with
 * a `@vitest-environment jsdom` docblock, which keeps the cost where the benefit is.
 *
 * The `@/` alias is repeated from `tsconfig.json` rather than derived from it.
 * Vitest resolves modules itself and does not read `compilerOptions.paths`, so
 * without this a test importing `@/lib/reel` fails to resolve while `tsc --noEmit`
 * stays perfectly happy — the two would disagree in the direction that hides the
 * problem, which is the worst way for a toolchain to disagree.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
