'use client';
import * as React from 'react';
import * as M from '@radix-ui/react-dropdown-menu';
import { Check, ChevronRight, Circle } from 'lucide-react';
import { cn } from '@/lib/utils';

export const DropdownMenu = M.Root;
export const DropdownMenuTrigger = M.Trigger;
export const DropdownMenuGroup = M.Group;
export const DropdownMenuSub = M.Sub;
export const DropdownMenuRadioGroup = M.RadioGroup;

const itemBase = 'relative flex cursor-default select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none transition-colors focus:bg-muted focus:text-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted-foreground';

export const DropdownMenuContent = React.forwardRef<React.ComponentRef<typeof M.Content>, React.ComponentPropsWithoutRef<typeof M.Content>>(
  ({ className, sideOffset = 6, ...props }, ref) => (
    <M.Portal>
      <M.Content ref={ref} sideOffset={sideOffset} collisionPadding={8}
        className={cn('z-50 min-w-[10rem] max-w-[calc(100vw-1rem)] overflow-hidden rounded-md border bg-popover p-1 text-popover-foreground shadow-pop data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95', className)} {...props} />
    </M.Portal>
  ));
DropdownMenuContent.displayName = 'DropdownMenuContent';

export const DropdownMenuItem = React.forwardRef<React.ComponentRef<typeof M.Item>, React.ComponentPropsWithoutRef<typeof M.Item> & { destructive?: boolean; inset?: boolean }>(
  ({ className, destructive, inset, ...props }, ref) => (
    <M.Item ref={ref} className={cn(itemBase, inset && 'pl-8', destructive && 'text-destructive focus:bg-danger-soft focus:text-destructive [&_svg]:text-destructive', className)} {...props} />
  ));
DropdownMenuItem.displayName = 'DropdownMenuItem';

export const DropdownMenuCheckboxItem = React.forwardRef<React.ComponentRef<typeof M.CheckboxItem>, React.ComponentPropsWithoutRef<typeof M.CheckboxItem>>(
  ({ className, children, checked, ...props }, ref) => (
    <M.CheckboxItem ref={ref} checked={checked} className={cn(itemBase, 'pl-8', className)} {...props}>
      <span className="absolute left-2 flex size-4 items-center justify-center"><M.ItemIndicator><Check className="size-4 text-foreground" /></M.ItemIndicator></span>
      {children}
    </M.CheckboxItem>
  ));
DropdownMenuCheckboxItem.displayName = 'DropdownMenuCheckboxItem';

export const DropdownMenuRadioItem = React.forwardRef<React.ComponentRef<typeof M.RadioItem>, React.ComponentPropsWithoutRef<typeof M.RadioItem>>(
  ({ className, children, ...props }, ref) => (
    <M.RadioItem ref={ref} className={cn(itemBase, 'pl-8', className)} {...props}>
      <span className="absolute left-2 flex size-4 items-center justify-center"><M.ItemIndicator><Circle className="size-2 fill-current text-foreground" /></M.ItemIndicator></span>
      {children}
    </M.RadioItem>
  ));
DropdownMenuRadioItem.displayName = 'DropdownMenuRadioItem';

export const DropdownMenuLabel = React.forwardRef<React.ComponentRef<typeof M.Label>, React.ComponentPropsWithoutRef<typeof M.Label>>(
  ({ className, ...props }, ref) => <M.Label ref={ref} className={cn('px-2 py-1.5 text-xs font-semibold text-muted-foreground', className)} {...props} />);
DropdownMenuLabel.displayName = 'DropdownMenuLabel';

export const DropdownMenuSeparator = React.forwardRef<React.ComponentRef<typeof M.Separator>, React.ComponentPropsWithoutRef<typeof M.Separator>>(
  ({ className, ...props }, ref) => <M.Separator ref={ref} className={cn('-mx-1 my-1 h-px bg-border', className)} {...props} />);
DropdownMenuSeparator.displayName = 'DropdownMenuSeparator';

export const DropdownMenuSubTrigger = React.forwardRef<React.ComponentRef<typeof M.SubTrigger>, React.ComponentPropsWithoutRef<typeof M.SubTrigger>>(
  ({ className, children, ...props }, ref) => (
    <M.SubTrigger ref={ref} className={cn(itemBase, 'data-[state=open]:bg-muted', className)} {...props}>{children}<ChevronRight className="ml-auto" /></M.SubTrigger>
  ));
DropdownMenuSubTrigger.displayName = 'DropdownMenuSubTrigger';

export const DropdownMenuSubContent = React.forwardRef<React.ComponentRef<typeof M.SubContent>, React.ComponentPropsWithoutRef<typeof M.SubContent>>(
  ({ className, ...props }, ref) => (
    <M.SubContent ref={ref} className={cn('z-50 min-w-[8rem] overflow-hidden rounded-md border bg-popover p-1 shadow-pop data-[state=open]:animate-in data-[state=open]:fade-in-0', className)} {...props} />
  ));
DropdownMenuSubContent.displayName = 'DropdownMenuSubContent';
