'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { Shield, ShieldAlert, Cpu, MemoryStick, MonitorPlay, Check } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { api, type AdminUser } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'

function CapacityBar({ label, value, icon: Icon, limit }: { label: string; value: number; icon: React.ElementType; limit?: number }) {
  const danger = limit !== undefined && value >= limit
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-muted-foreground flex items-center gap-2"><Icon className="w-4 h-4" />{label}</span>
          <span className={`text-sm font-semibold ${danger ? 'text-destructive' : 'text-foreground'}`}>{value.toFixed(0)}%</span>
        </div>
        <div className="h-2 rounded-full bg-secondary overflow-hidden">
          <div className={`h-full ${danger ? 'bg-destructive' : 'bg-primary'}`} style={{ width: `${Math.min(100, value)}%` }} />
        </div>
      </CardContent>
    </Card>
  )
}

export default function AdminPage() {
  const { user } = useAuth()
  const isAdmin = user?.isAdmin
  const { data: users, mutate, isLoading } = useSWR<AdminUser[]>(isAdmin ? 'admin-users' : null, () => api.getAdminUsers())
  const { data: cap } = useSWR(isAdmin ? 'admin-capacity' : null, () => api.getCapacity(), { refreshInterval: 4000 })
  const [agentEdits, setAgentEdits] = useState<Record<string, string>>({})

  if (user && !isAdmin) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-muted-foreground gap-2">
        <ShieldAlert className="w-10 h-10" /><p>Admins only.</p>
      </div>
    )
  }

  const setTier = async (id: string, tier: string) => {
    try { await api.updateAdminUser(id, { tier }); toast.success(`Tier → ${tier}`); mutate() }
    catch (e) { toast.error(e instanceof Error ? e.message : 'Failed') }
  }
  const setAgents = async (id: string) => {
    const n = parseInt(agentEdits[id], 10)
    if (Number.isNaN(n)) return
    try { await api.updateAdminUser(id, { agents_limit: n }); toast.success(`Agents → ${n}`); mutate() }
    catch (e) { toast.error(e instanceof Error ? e.message : 'Failed') }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground flex items-center gap-2"><Shield className="w-6 h-6 text-primary" /> Admin</h1>
        <p className="text-sm text-muted-foreground mt-1">Server capacity and per-user plans / agents</p>
      </div>

      {/* Capacity */}
      <div className="grid gap-4 sm:grid-cols-3">
        <CapacityBar label="CPU" value={cap?.cpu_percent ?? 0} icon={Cpu} limit={cap?.limits.cpu_percent} />
        <CapacityBar label="RAM" value={cap?.ram_percent ?? 0} icon={MemoryStick} limit={cap?.limits.ram_percent} />
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm text-muted-foreground flex items-center gap-2"><MonitorPlay className="w-4 h-4" />Browsers</span>
              <span className="text-sm font-semibold text-foreground">{cap?.our_browsers ?? 0} / {cap?.limits.max_browsers ?? 0}</span>
            </div>
            <p className="text-xs text-muted-foreground">Active headless Chromium instances</p>
          </CardContent>
        </Card>
      </div>

      {/* Users */}
      <div className="rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Email</TableHead>
              <TableHead>Accounts</TableHead>
              <TableHead>Running</TableHead>
              <TableHead className="w-[150px]">Plan</TableHead>
              <TableHead className="w-[180px]">Agents</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow><TableCell colSpan={5} className="h-20 text-center text-muted-foreground">Loading...</TableCell></TableRow>
            ) : users && users.length > 0 ? (
              users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell className="font-medium">{u.email}</TableCell>
                  <TableCell>{u.accounts_count}</TableCell>
                  <TableCell>{u.running_tasks > 0 ? <Badge>{u.running_tasks}</Badge> : <span className="text-muted-foreground">0</span>}</TableCell>
                  <TableCell>
                    <Select value={u.tier} onValueChange={(v) => setTier(u.id, v)}>
                      <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="free">Free</SelectItem>
                        <SelectItem value="pro">Pro</SelectItem>
                        <SelectItem value="enterprise">Enterprise</SelectItem>
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1.5">
                      <Input
                        type="number"
                        className="h-8 w-16"
                        defaultValue={u.agents_limit}
                        onChange={(e) => setAgentEdits((p) => ({ ...p, [u.id]: e.target.value }))}
                      />
                      <Button size="icon-sm" variant="outline" onClick={() => setAgents(u.id)}><Check className="w-4 h-4" /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow><TableCell colSpan={5} className="h-32 text-center text-muted-foreground">No users</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
