import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: ['lib/api-types.ts', '.next/**', 'node_modules/**', 'playwright-report/**', 'test-results/**'] },
  {
    rules: {
      // Reading localStorage / syncing props in effects is a deliberate pattern in this codebase.
      'react-hooks/set-state-in-effect': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
];
export default config;
