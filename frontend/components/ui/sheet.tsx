'use client';
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';
import { DialogOverlay, DialogHeader, DialogFooter, DialogTitle, DialogDescription } from './dialog';

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

const sheetVariants = cva(
  'fixed z-50 flex flex-col gap-4 bg-popover p-6 text-popover-foreground shadow-pop focus-visible:outline-none',
  {
    variants: {
      side: {
        right: 'inset-y-0 right-0 h-full w-[calc(100vw-2rem)] border-l data-[state=open]:animate-slide-in-right data-[state=closed]:animate-slide-out-right',
        left: 'inset-y-0 left-0 h-full w-[calc(100vw-3rem)] border-r data-[state=open]:animate-slide-in-left data-[state=closed]:animate-slide-out-left',
      },
      size: { sm: 'sm:max-w-sm', md: 'sm:max-w-md', lg: 'sm:max-w-2xl', xl: 'sm:max-w-4xl' },
    },
    defaultVariants: { side: 'right', size: 'md' },
  },
);

/** Right-side drawer for detail views (contact/business detail, filters). `side="left"` powers the mobile nav. */
export const SheetContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & VariantProps<typeof sheetVariants>
>(({ side, size, className, children, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogOverlay />
    <DialogPrimitive.Content ref={ref} className={cn(sheetVariants({ side, size }), className)} {...props}>
      {children}
      <DialogPrimitive.Close className="absolute right-3 top-3 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <X className="size-4" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
));
SheetContent.displayName = 'SheetContent';

export const SheetHeader = DialogHeader;
export const SheetFooter = ({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) => <DialogFooter className={cn('mt-auto', className)} {...p} />;
export const SheetTitle = DialogTitle;
export const SheetDescription = DialogDescription;
