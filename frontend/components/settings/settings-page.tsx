'use client';
import * as React from 'react';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { RequireRole, useMe } from '@/lib/auth';
import { PageHeader } from '@/components/composites/page-header';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { DirtyProvider, useDirtyRegistry } from './dirty';
import { AccountSection } from './account-section';
import { UsersSection } from './users-section';
import { AuditSection } from './audit-section';
import { PointersSection } from './pointers-section';
import { BackupSection, ConcurrencySection, ExportDefaultsSection, NotificationsSection, RetentionSection, SourcePolicySection, WorkerSection } from './sections';
import { ValidationSection } from './validation-section';

interface Tab { id: string; label: string; admin?: boolean; render: () => React.ReactNode }
const TABS: Tab[] = [
  { id: 'account', label: 'Account & security', render: () => <AccountSection /> },
  { id: 'users', label: 'Users', admin: true, render: () => <UsersSection /> },
  { id: 'worker', label: 'Worker', admin: true, render: () => <WorkerSection /> },
  { id: 'source-policies', label: 'Source policies', admin: true, render: () => <SourcePolicySection /> },
  { id: 'validation', label: 'Validation', render: () => <ValidationSection /> },
  { id: 'concurrency', label: 'Concurrency', admin: true, render: () => <ConcurrencySection /> },
  { id: 'retention', label: 'Data retention', admin: true, render: () => <RetentionSection /> },
  { id: 'exports', label: 'Export defaults', render: () => <ExportDefaultsSection /> },
  { id: 'countries', label: 'Countries & categories', render: () => <PointersSection /> },
  { id: 'notifications', label: 'Notifications', render: () => <NotificationsSection /> },
  { id: 'audit', label: 'Audit log', admin: true, render: () => <AuditSection /> },
  { id: 'backups', label: 'Backups', admin: true, render: () => <BackupSection /> },
];

export function SettingsPage() {
  return (
    <RequireRole role="viewer">
      <DirtyProvider><Inner /></DirtyProvider>
    </RequireRole>
  );
}

function Inner() {
  const me = useMe();
  const { anyDirty } = useDirtyRegistry();
  const { filters, set } = useUrlFilters({ tab: 'account' });
  const visible = TABS.filter((t) => !t.admin || me.can('admin'));
  const active = visible.find((t) => t.id === filters.tab)?.id ?? 'account';
  return (
    <>
      <PageHeader title="Settings" description={me.can('admin') ? 'Account security, users, operational limits, policies and the audit log.' : 'Your account and security. Other settings are managed by administrators.'} />
      <Tabs value={active} onValueChange={(v) => {
        if (v === active) return;
        if (anyDirty() && !window.confirm('You have unsaved changes in this section. Discard them and switch?')) return;
        set({ tab: v === 'account' ? undefined : v });
      }}>
        <TabsList aria-label="Settings sections">
          {visible.map((t) => <TabsTrigger key={t.id} value={t.id}>{t.label}</TabsTrigger>)}
        </TabsList>
        {visible.map((t) => <TabsContent key={t.id} value={t.id}>{t.id === active ? t.render() : null}</TabsContent>)}
      </Tabs>
    </>
  );
}
