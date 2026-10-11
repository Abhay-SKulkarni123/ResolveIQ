/**
 * Design tokens for ResolveIQ.
 *
 * The palette is deliberately small and warm-neutral: paper-like canvas, white
 * surfaces, one restrained indigo accent, and a closed set of semantic status
 * colours. Colour is never the only signal — every status also carries a word —
 * which is why the semantic tokens are named for the meaning ("success"), not the
 * hue ("green").
 *
 * Elevation is intentionally quiet. Depth comes from hairline borders and one or
 * two low shadows, not from a shadow under every card.
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Surfaces — warm neutrals rather than cool blue-greys.
        canvas: '#f5f5f3',
        surface: {
          DEFAULT: '#ffffff',
          subtle: '#fafaf8',
          sunken: '#f7f7f5',
        },
        // Text.
        ink: {
          DEFAULT: '#23272f',
          muted: '#5c636e',
          faint: '#8b909a',
        },
        // Hairline structure.
        line: {
          DEFAULT: '#e7e6e2',
          strong: '#d7d6d1',
        },
        // The single brand accent: a muted indigo that reads as trustworthy
        // rather than decorative.
        accent: {
          DEFAULT: '#3d4db7',
          hover: '#33419c',
          active: '#2b3690',
          soft: '#eef0fb',
          'soft-hover': '#e5e8f8',
          border: '#c8cdf0',
          text: '#2b3690',
        },
        // Semantic states. Each is used with a text label, never alone.
        success: {
          bg: '#eef7f1',
          border: '#bfe3cd',
          text: '#1f6b43',
          solid: '#2f8a5b',
        },
        warning: {
          bg: '#fdf5e6',
          border: '#f0d9a8',
          text: '#8a5a12',
          solid: '#b8802a',
        },
        danger: {
          bg: '#fdeeee',
          border: '#f2c6c6',
          text: '#9b2c2c',
          solid: '#c04141',
        },
        info: {
          bg: '#eef3fb',
          border: '#c9d9f0',
          text: '#2b4a86',
          solid: '#3f6bb0',
        },
        neutral: {
          bg: '#f4f4f2',
          border: '#e2e1dc',
          text: '#5c636e',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'SF Mono',
          'Menlo',
          'Consolas',
          'Liberation Mono',
          'monospace',
        ],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      borderRadius: {
        md: '0.375rem',
        lg: '0.5rem',
        xl: '0.75rem',
      },
      boxShadow: {
        xs: '0 1px 2px 0 rgb(28 30 34 / 0.04)',
        sm: '0 1px 3px 0 rgb(28 30 34 / 0.06), 0 1px 2px -1px rgb(28 30 34 / 0.06)',
        md: '0 4px 12px -2px rgb(28 30 34 / 0.10), 0 2px 6px -2px rgb(28 30 34 / 0.06)',
        focus: '0 0 0 3px rgb(61 77 183 / 0.28)',
      },
      transitionDuration: {
        DEFAULT: '150ms',
      },
      keyframes: {
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
        'fade-in': {
          '0%': { opacity: '0', transform: 'translateY(2px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        shimmer: 'shimmer 1.4s infinite',
        'fade-in': 'fade-in 180ms ease-out',
      },
    },
  },
  plugins: [],
}
