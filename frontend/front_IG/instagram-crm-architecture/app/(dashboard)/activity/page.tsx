'use client'

import useSWR from 'swr'
import { Activity, Loader2, CheckCircle2, XCircle, Clock, Rocket, Zap } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { api, type Task, type Account } from '@/lib/api'

function actionIcon(a: string) {
  if (a === 'warmup') return <Clock className="w-4 h-4" />
  if (a.startsWith('upload')) return <Rocket className="w-4 h-4" />
  return <Zap className="w-4 h-4" />
}

export default function ActivityPage() {
  // Live: poll all tasks every 2s so the operator sees agents move in real time.
  const { data: tasks } = useSWR<Task[]>('activity-tasks', () => api.getTasks(), { refreshInterval: 2000 })
  const { data: accounts } = useSWR<Account[]>('accounts', () => api.getAccounts().catch(() => []))
  const nameById = new Map((accounts || []).map((a) => [a.id, a.ig_username]))

  const all = tasks || []
  const running = all.filter((t) => t.status === 'running')
  const pending = all.filter((t) => t.status === 'pending')
  const recent = [...all]
    .filter((t) => t.status === 'completed' || t.status === 'failed')
    .sort((a, b) => +new Date(b.completed_at || b.created_at) - +new Date(a.completed_at || a.created_at))
    .slice(0, 12)

  return (
    <div className="space-y-6 pb-24">
      <div>
        <h1 className="text-2xl font-semibold text-foreground flex items-center gap-2">
          <Activity className="w-6 h-6 text-primary" /> Live Activity
        </h1>
        <p className="text-sm text-muted-foreground mt-1">Watch your agents work in real time</p>
      </div>

      {/* Running agents */}
      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">
          Active agents · {running.length}
        </h2>
        {running.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border h-28 flex items-center justify-center text-muted-foreground text-sm">
            No agents running right now. Dispatch a task from the AI Orchestrator.
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {running.map((t) => {
              const action = t.payload?.commands?.[0]?.action ?? 'task'
              return (
                <Card key={t.id} className="border-amber-500/30">
                  <CardContent className="p-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="font-medium">@{nameById.get(t.account_id) ?? 'account'}</span>
                      <span className="relative flex h-2.5 w-2.5">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75" />
                        <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-500" />
                      </span>
                    </div>
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      {actionIcon(action)}
                      <span className="capitalize">{action.replace(/_/g, ' ')}</span>
                      <Loader2 className="w-3.5 h-3.5 animate-spin ml-auto" />
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {(t.payload?.commands ?? []).map((c, i) => (
                        <Badge key={i} variant="outline" className="text-[10px]">{c.action.replace(/_/g, ' ')}</Badge>
                      ))}
                    </div>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        )}
      </div>

      {/* Recent feed */}
      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">Recent results</h2>
        <div className="rounded-lg border border-border bg-card divide-y divide-border">
          {recent.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">Nothing finished yet.</div>
          ) : (
            recent.map((t) => {
              const action = t.payload?.commands?.[0]?.action ?? 'task'
              const ok = t.status === 'completed'
              return (
                <div key={t.id} className="flex items-center gap-3 px-4 py-3">
                  {ok ? <CheckCircle2 className="w-4 h-4 text-success" /> : <XCircle className="w-4 h-4 text-destructive" />}
                  <span className="text-sm font-medium">@{nameById.get(t.account_id) ?? 'account'}</span>
                  <Badge variant="outline" className="text-xs capitalize">{action.replace(/_/g, ' ')}</Badge>
                  <span className="text-xs text-muted-foreground ml-auto">{new Date(t.completed_at || t.created_at).toLocaleTimeString()}</span>
                </div>
              )
            })
          )}
        </div>
      </div>

      {/* Sims-style sticky taskbar */}
      <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-20">
        <div className="flex items-center gap-4 rounded-full border border-border bg-card/95 backdrop-blur px-5 py-2.5 shadow-lg">
          <div className="flex items-center gap-2 text-sm">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
            </span>
            <span className="font-medium">{running.length}</span>
            <span className="text-muted-foreground">running</span>
          </div>
          <div className="w-px h-5 bg-border" />
          <div className="flex items-center gap-2 text-sm">
            <Clock className="w-3.5 h-3.5 text-muted-foreground" />
            <span className="font-medium">{pending.length}</span>
            <span className="text-muted-foreground">queued</span>
          </div>
        </div>
      </div>
    </div>
  )
}
