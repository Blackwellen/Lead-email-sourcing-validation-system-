import type { Metadata, Viewport } from 'next';
import './globals.css';
import { Providers } from './providers';
import { themeInitScript } from '@/hooks/use-theme';

export const metadata: Metadata = {
  title: { default: 'Lead Intelligence', template: '%s · Lead Intelligence' },
  description: 'Lead intelligence and email verification operations console.',
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { width: 'device-width', initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
