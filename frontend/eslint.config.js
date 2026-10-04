import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks, 'react-refresh': reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-explicit-any': 'error',
      // Money handling is the reason these are here. parseFloat or Number on an amount
      // turns "5021.86" into a float that can render as 5021.859999999999, which a
      // reviewer reads as a different number than the one that was charged. Amounts
      // arrive as strings and are formatted by src/components/money.ts.
      'no-restricted-globals': [
        'error',
        { name: 'parseFloat', message: 'Amounts must stay strings; format them instead.' },
        { name: 'parseInt', message: 'Amounts must stay strings; format them instead.' },
      ],
      'no-restricted-properties': [
        'error',
        {
          object: 'Number',
          property: 'parseFloat',
          message: 'Amounts must stay strings; format them instead.',
        },
        {
          object: 'Number',
          property: 'parseInt',
          message: 'Amounts must stay strings; format them instead.',
        },
      ],
    },
  },
)
