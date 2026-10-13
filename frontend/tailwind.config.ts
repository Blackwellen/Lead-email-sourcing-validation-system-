import type { Config } from 'tailwindcss';
import animate from 'tailwindcss-animate';

const v = (name: string) => `hsl(var(--${name}) / <alpha-value>)`;
const status = (n: string) => ({ DEFAULT: v(n), soft: v(`${n}-soft`), border: v(`${n}-border`), foreground: v(`${n}-foreground`) });

const config: Config = {
  darkMode: ['class'],
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}', './hooks/**/*.{ts,tsx}'],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      colors: {
        background: v('background'),
        foreground: v('foreground'),
        card: { DEFAULT: v('card'), foreground: v('card-foreground') },
        popover: { DEFAULT: v('popover'), foreground: v('popover-foreground') },
        muted: { DEFAULT: v('muted'), foreground: v('muted-foreground') },
        border: v('border'),
        input: v('input'),
        ring: v('ring'),
        primary: { DEFAULT: v('primary'), foreground: v('primary-foreground') },
        secondary: { DEFAULT: v('secondary'), foreground: v('secondary-foreground') },
        accent: { DEFAULT: v('accent'), foreground: v('accent-foreground') },
        destructive: { DEFAULT: v('destructive'), foreground: v('destructive-foreground') },
        sidebar: { DEFAULT: v('sidebar'), foreground: v('sidebar-foreground'), border: v('sidebar-border'), accent: v('sidebar-accent') },
        success: status('success'),
        warning: status('warning'),
        danger: status('danger'),
        info: status('info'),
        neutral: status('neutral'),
        chart: {
          1: v('chart-1'), 2: v('chart-2'), 3: v('chart-3'), 4: v('chart-4'),
          5: v('chart-5'), 6: v('chart-6'), 7: v('chart-7'), 8: v('chart-8'),
        },
      },
      borderRadius: { lg: 'var(--radius)', md: 'calc(var(--radius) - 2px)', sm: 'calc(var(--radius) - 4px)' },
      fontFamily: {
        sans: ['var(--font-sans)'],
        mono: ['var(--font-mono)'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      boxShadow: {
        card: '0 1px 2px 0 hsl(var(--shadow) / 0.05)',
        pop: '0 8px 24px -6px hsl(var(--shadow) / 0.18), 0 2px 6px -2px hsl(var(--shadow) / 0.12)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        shimmer: { '100%': { transform: 'translateX(100%)' } },
        'slide-in-right': { from: { transform: 'translateX(100%)' }, to: { transform: 'translateX(0)' } },
        'slide-out-right': { from: { transform: 'translateX(0)' }, to: { transform: 'translateX(100%)' } },
        'slide-in-left': { from: { transform: 'translateX(-100%)' }, to: { transform: 'translateX(0)' } },
        'slide-out-left': { from: { transform: 'translateX(0)' }, to: { transform: 'translateX(-100%)' } },
      },
      animation: {
        'fade-in': 'fade-in 150ms ease-out',
        'slide-in-right': 'slide-in-right 220ms cubic-bezier(.32,.72,0,1)',
        'slide-out-right': 'slide-out-right 180ms ease-in',
        'slide-in-left': 'slide-in-left 220ms cubic-bezier(.32,.72,0,1)',
        'slide-out-left': 'slide-out-left 180ms ease-in',
      },
    },
  },
  plugins: [animate],
};
export default config;
