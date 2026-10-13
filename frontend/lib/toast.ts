'use client';
import { useSyncExternalStore } from 'react';

export type ToastKind = 'success' | 'error' | 'warning' | 'info';
export interface ToastItem {
  id: number;
  kind: ToastKind;
  title: string;
  description?: string;
  duration: number;
  action?: { label: string; onClick: () => void };
}
export type ToastInput = Omit<ToastItem, 'id' | 'kind' | 'duration'> & { duration?: number };

let items: ToastItem[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function push(kind: ToastKind, input: string | ToastInput, description?: string): number {
  const i: ToastInput = typeof input === 'string' ? { title: input, description } : input;
  const id = ++seq;
  items = [...items, { id, kind, title: i.title, description: i.description, action: i.action, duration: i.duration ?? (kind === 'error' ? 8000 : 4500) }].slice(-5);
  emit();
  return id;
}

/** Imperative API usable anywhere (event handlers, mutation callbacks). */
export const toast = {
  success: (t: string | ToastInput, d?: string) => push('success', t, d),
  error: (t: string | ToastInput, d?: string) => push('error', t, d),
  warning: (t: string | ToastInput, d?: string) => push('warning', t, d),
  info: (t: string | ToastInput, d?: string) => push('info', t, d),
  dismiss: (id: number) => { items = items.filter((x) => x.id !== id); emit(); },
};

const EMPTY: ToastItem[] = [];
export function useToasts() {
  return useSyncExternalStore((cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => items, () => EMPTY);
}
