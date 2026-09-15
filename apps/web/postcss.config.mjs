/**
 * PostCSS configuration.
 *
 * Tailwind 4 is a PostCSS plugin and nothing else — there is no `tailwind.config.ts`.
 * The design tokens live in `src/app/globals.css` under `@theme`, which means the
 * place you look for a colour is the place you look for everything else about it.
 */

const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
