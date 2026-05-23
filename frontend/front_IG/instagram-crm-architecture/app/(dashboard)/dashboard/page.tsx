'use client'

import { useEffect, useState } from 'react'
import { Activity, Users, Eye, CheckCircle2, XCircle, Loader2, Play } from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  api,
  type Account,
  type AccountMetric,
  type DashboardMetrics,
  type Task,
} from '@/lib/api'

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function MetricCard({
  title,
  value,
  subtitle,
  icon: Icon,
}: {
  title: string
  value: string | number
  subtitle?: string
  icon: React.ElementType
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold text-foreground">{value}</div>
        {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
      </CardContent>
    </Card>
  )
}

export default function DashboardPage() {
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [series, setSeries] = useState<{ date: string; followers: number }[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [m, t, accs] = await Promise.all([
          api.getDashboardMetrics(),
          api.getTasks().catch(() => [] as Task[]),
          api.getAccounts().catch(() => [] as Account[]),
        ])
        if (cancelled) return
        setMetrics(m)
        setTasks(t.slice(0, 6))
        setAccounts(accs)

        // followers chart: use the first account that has metric data
        if (accs.length > 0) {
          for (const acc of accs.slice(0, 3)) {
            try {
              const res = await api.getAccountMetrics(acc.id)
              const points = (res.metrics || [])
                .filter((x: AccountMetric) => x.metric_type === 'followers')
                .map((x) => ({
                  date: new Date(x.captured_at).toLocaleDateString([], { month: 'short', day: 'numeric' }),
                  followers: x.value,
                }))
              if (points.length > 0) {
                if (!cancelled) setSeries(points)
                break
              }
            } catch {
              /* ignore per-account metric errors */
            }
          }
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load dashboard')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  const usernameById = new Map(accounts.map((a) => [a.id, a.ig_username]))

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Overview of your Instagram fleet performance</p>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 text-destructive px-4 py-3 text-sm">
          {error}
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          title="Total Accounts"
          value={metrics?.accounts_total ?? 0}
          subtitle={`${metrics?.accounts_active ?? 0} active, ${metrics?.accounts_checkpoint ?? 0} in checkpoint`}
          icon={Users}
        />
        <MetricCard
          title="Total Followers"
          value={(metrics?.total_followers ?? 0).toLocaleString()}
          subtitle="Across all accounts"
          icon={Eye}
        />
        <MetricCard
          title="Tasks (24h)"
          value={metrics?.tasks_completed_24h ?? 0}
          subtitle={`${metrics?.tasks_failed_24h ?? 0} failed`}
          icon={Activity}
        />
        <MetricCard
          title="Running Now"
          value={metrics?.tasks_running ?? 0}
          subtitle="Tasks currently executing"
          icon={Play}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="text-base">Audience Growth (followers)</CardTitle>
          </CardHeader>
          <CardContent>
            {series.length > 0 ? (
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <defs>
                      <linearGradient id="fl" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
                        <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
                    <XAxis dataKey="date" tick={{ fontSize: 12 }} className="text-muted-foreground" />
                    <YAxis tick={{ fontSize: 12 }} className="text-muted-foreground" />
                    <Tooltip />
                    <Area type="monotone" dataKey="followers" stroke="hsl(var(--primary))" fill="url(#fl)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div className="h-64 flex items-center justify-center rounded-lg bg-secondary/30 border border-dashed border-border">
                <div className="text-center">
                  <Activity className="w-10 h-10 text-muted-foreground mx-auto mb-2" />
                  <p className="text-sm text-muted-foreground">No analytics data yet</p>
                  <p className="text-xs text-muted-foreground mt-1">Run tasks to start collecting follower metrics</p>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Recent Activity</CardTitle>
          </CardHeader>
          <CardContent>
            {tasks.length === 0 ? (
              <p className="text-sm text-muted-foreground">No tasks yet.</p>
            ) : (
              <div className="space-y-4">
                {tasks.map((task) => {
                  const action = task.payload?.commands?.[0]?.action ?? 'task'
                  const ok = task.status === 'completed'
                  const failed = task.status === 'failed'
                  return (
                    <div key={task.id} className="flex items-start gap-3">
                      <div className="mt-0.5">
                        {ok ? (
                          <CheckCircle2 className="w-4 h-4 text-success" />
                        ) : failed ? (
                          <XCircle className="w-4 h-4 text-destructive" />
                        ) : (
                          <Loader2 className="w-4 h-4 text-muted-foreground animate-spin" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-foreground truncate">
                            @{usernameById.get(task.account_id) ?? 'account'}
                          </span>
                          <Badge variant="outline" className="text-xs shrink-0">
                            {action.replace(/_/g, ' ')}
                          </Badge>
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {task.status} · {timeAgo(task.created_at)}
                        </p>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
