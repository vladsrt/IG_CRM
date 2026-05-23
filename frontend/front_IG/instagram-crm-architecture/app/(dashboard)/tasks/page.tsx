'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { CheckCircle2, XCircle, Loader2, Clock, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { api, type Task, type Account } from '@/lib/api'

const TABS = ['all', 'pending', 'running', 'completed', 'failed'] as const

function StatusBadge({ s }: { s: Task['status'] }) {
  const map: Record<string, { icon: React.ReactNode; cls: string }> = {
    completed: { icon: <CheckCircle2 className="w-3.5 h-3.5" />, cls: 'bg-success/15 text-success' },
    failed: { icon: <XCircle className="w-3.5 h-3.5" />, cls: 'bg-destructive/15 text-destructive' },
    running: { icon: <Loader2 className="w-3.5 h-3.5 animate-spin" />, cls: 'bg-amber-500/15 text-amber-500' },
    pending: { icon: <Clock className="w-3.5 h-3.5" />, cls: 'bg-secondary text-muted-foreground' },
    draft: { icon: <Clock className="w-3.5 h-3.5" />, cls: 'bg-secondary text-muted-foreground' },
  }
  const v = map[s] ?? map.draft
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${v.cls}`}>{v.icon}{s}</span>
}

export default function TasksPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('all')
  const { data: tasks, mutate, isLoading } = useSWR<Task[]>(
    ['tasks', tab],
    () => api.getTasks(tab === 'all' ? undefined : tab),
    { refreshInterval: 5000 },
  )
  const { data: accounts } = useSWR<Account[]>('accounts', () => api.getAccounts().catch(() => []))
  const [selected, setSelected] = useState<Task | null>(null)

  const nameById = new Map((accounts || []).map((a) => [a.id, a.ig_username]))

  const retry = async (id: string) => {
    try {
      await api.retryTask(id); toast.success('Re-queued'); mutate()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Retry failed')
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Tasks</h1>
        <p className="text-sm text-muted-foreground mt-1">Execution history across your fleet (auto-refreshes)</p>
      </div>

      <div className="flex gap-1.5">
        {TABS.map((t) => (
          <Button key={t} size="sm" variant={tab === t ? 'default' : 'outline'} className="capitalize h-8" onClick={() => setTab(t)}>{t}</Button>
        ))}
      </div>

      <div className="rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Action</TableHead>
              <TableHead>Account</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="w-[80px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow><TableCell colSpan={5} className="h-20 text-center text-muted-foreground">Loading...</TableCell></TableRow>
            ) : tasks && tasks.length > 0 ? (
              tasks.map((t) => (
                <TableRow key={t.id} className="cursor-pointer" onClick={() => setSelected(t)}>
                  <TableCell className="font-medium">{t.payload?.commands?.[0]?.action?.replace(/_/g, ' ') ?? 'task'}</TableCell>
                  <TableCell>@{nameById.get(t.account_id) ?? '—'}</TableCell>
                  <TableCell><StatusBadge s={t.status} /></TableCell>
                  <TableCell className="text-muted-foreground text-sm">{new Date(t.created_at).toLocaleString()}</TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    {(t.status === 'failed' || t.status === 'draft') && (
                      <Button variant="ghost" size="icon-sm" onClick={() => retry(t.id)}><RotateCcw className="w-4 h-4" /></Button>
                    )}
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow><TableCell colSpan={5} className="h-32 text-center text-muted-foreground">No tasks</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
          <DialogHeader><DialogTitle>Task details</DialogTitle></DialogHeader>
          {selected && (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-2"><StatusBadge s={selected.status} /><span className="text-muted-foreground">@{nameById.get(selected.account_id) ?? '—'}</span></div>
              <div><span className="text-muted-foreground">Created:</span> {new Date(selected.created_at).toLocaleString()}</div>
              {selected.completed_at && <div><span className="text-muted-foreground">Completed:</span> {new Date(selected.completed_at).toLocaleString()}</div>}
              <div>
                <p className="text-muted-foreground mb-1">Plan:</p>
                <pre className="bg-secondary/40 rounded p-3 text-xs overflow-x-auto">{JSON.stringify(selected.payload, null, 2)}</pre>
              </div>
              {selected.error_log && (
                <div>
                  <p className="text-destructive mb-1">Error:</p>
                  <pre className="bg-destructive/10 text-destructive rounded p-3 text-xs overflow-x-auto whitespace-pre-wrap">{selected.error_log}</pre>
                </div>
              )}
              {(selected.status === 'failed' || selected.status === 'draft') && (
                <Button onClick={() => { retry(selected.id); setSelected(null) }}><RotateCcw className="w-4 h-4 mr-2" /> Retry task</Button>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
