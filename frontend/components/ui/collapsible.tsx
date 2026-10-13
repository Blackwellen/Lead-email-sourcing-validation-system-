'use client';
import * as React from 'react';
import * as CollapsiblePrimitive from '@radix-ui/react-collapsible';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export const Collapsible = CollapsiblePrimitive.Root;
export const CollapsibleTrigger = CollapsiblePrimitive.Trigger;
export const CollapsibleContent = React.forwardRef<
  React.ComponentRef<typeof CollapsiblePrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof CollapsiblePrimitive.Content>
>(({ className, ...p }, ref) => <CollapsiblePrimitive.Content ref={ref} className={cn('overflow-hidden data-[state=open]:animate-in data-[state=open]:fade-in-0', className)} {...p} />);
CollapsibleContent.displayName = 'CollapsibleContent';

/** "Advanced settings" style panel: `<AdvancedPanel title="Advanced settings">…</AdvancedPanel>` */
export function AdvancedPanel({ title = 'Advanced settings', description, defaultOpen = false, children, className }: {
  title?: React.ReactNode; description?: React.ReactNode; defaultOpen?: boolean; children: React.ReactNode; className?: string;
}) {
  return (
    <Collapsible defaultOpen={defaultOpen} className={cn('rounded-lg border bg-card', className)}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 rounded-lg px-4 py-3 text-left text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span>{title}</span>
        {description && <span className="ml-1 hidden text-xs font-normal text-muted-foreground sm:inline">{description}</span>}
      </CollapsibleTrigger>
      <CollapsibleContent><div className="border-t p-4">{children}</div></CollapsibleContent>
    </Collapsible>
  );
}

/* Accordion: a set of independently-collapsible sections (single-open optional) built on Collapsible. */
export interface AccordionItemDef { id: string; title: React.ReactNode; content: React.ReactNode }
export function Accordion({ items, type = 'multiple', defaultOpen = [], className }: { items: AccordionItemDef[]; type?: 'single' | 'multiple'; defaultOpen?: string[]; className?: string }) {
  const [open, setOpen] = React.useState<string[]>(defaultOpen);
  const toggle = (id: string, v: boolean) => setOpen((o) => (v ? (type === 'single' ? [id] : [...o, id]) : o.filter((x) => x !== id)));
  return (
    <div className={cn('divide-y rounded-lg border bg-card', className)}>
      {items.map((it) => (
        <Collapsible key={it.id} open={open.includes(it.id)} onOpenChange={(v) => toggle(it.id, v)}>
          <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
            <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            {it.title}
          </CollapsibleTrigger>
          <CollapsibleContent><div className="px-4 pb-4 pl-10 text-sm">{it.content}</div></CollapsibleContent>
        </Collapsible>
      ))}
    </div>
  );
}
