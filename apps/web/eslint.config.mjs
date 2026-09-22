// ESLint flat config.
//
// `eslint-config-next` is imported directly rather than wrapped in
// `FlatCompat`. The compat shim exists to load *eslintrc*-shaped configs into a
// flat config, and Next 16 stopped shipping one: `eslint-config-next/core-web-vitals`
// and `/typescript` both export a flat config array (lengths 4 and 5). Handing a
// flat config to FlatCompat re-normalises it through the legacy validator, which
// dies formatting its own error message with "property 'react' closes the circle"
// — the config has a cycle that only matters once it is stringified. The symptom
// names neither the config nor the cause, so this comment is the only thing that
// stops someone reaching for FlatCompat again.
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

const config = [
  ...nextCoreWebVitals,
  ...nextTypeScript,
  {
    rules: {
      // The reel payload is untrusted JSON decoded from a file the user supplied.
      // `any` at the boundary is honest; the parsers narrow it immediately.
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { prefer: "type-imports", fixStyle: "inline-type-imports" },
      ],
    },
  },
  {
    ignores: [".next/**", "node_modules/**", "out/**", "next-env.d.ts"],
  },
];

export default config;
