'use client';
import * as React from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tooltip } from '@/components/ui/tooltip';
import { toast } from '@/lib/toast';

export function CopyButton({ value, label = 'Copy', size = 'icon-sm', className }: { value: string; label?: string; size?: 'icon' | 'icon-sm' | 'sm'; className?: string }) {
  const [done, setDone] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setDone(true);
      setTimeout(() => setDone(false), 1800);
    } catch {
      toast.error('Could not copy to clipboard');
    }
  };
  return (
    <Tooltip content={done ? 'Copied' : label}>
      <Button type="button" variant="ghost" size={size} className={className} onClick={copy} aria-label={done ? 'Copied' : label}>
        {done ? <Check className="text-success" /> : <Copy />}
        <span className="sr-only" aria-live="polite">{done ? 'Copied to clipboard' : ''}</span>
      </Button>
    </Tooltip>
  );
}
