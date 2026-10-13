import { notFound } from 'next/navigation';
import { DesignShowcase } from './showcase';

export const metadata = { title: 'Design system' };

/** Visual QA page for every primitive. Only served in development. */
export default function DesignPage() {
  if (process.env.NODE_ENV === 'production') notFound();
  return <DesignShowcase />;
}
