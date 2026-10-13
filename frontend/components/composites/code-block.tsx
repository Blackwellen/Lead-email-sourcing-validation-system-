import * as React from 'react';
import { CopyButton } from './copy-button';
import { cn } from '@/lib/utils';

export function CodeBlock({ code, language, className, wrap }: { code: string; language?: string; className?: string; wrap?: boolean }) {
  return (
    <div className={cn('group relative overflow-hidden rounded-md border bg-muted/60', className)}>
      {language && <div className="border-b px-3 py-1 font-mono text-2xs uppercase tracking-wide text-muted-foreground">{language}</div>}
      <pre tabIndex={0} className={cn('max-h-96 overflow-auto p-3 pr-12 font-mono text-xs leading-5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring', wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre')}><code>{code}</code></pre>
      <CopyButton value={code} label="Copy code" className="absolute right-1.5 top-1.5" />
    </div>
  );
}
