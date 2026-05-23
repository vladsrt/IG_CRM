'use client'

import { Check, Cpu, User as UserIcon, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/lib/auth-context'

const PLANS = [
  { tier: 'free', name: 'Free', agents: 1, price: '$0', perks: ['1 parallel agent', 'Basic dashboard', 'Manual tasks'] },
  { tier: 'pro', name: 'Pro', agents: 5, price: '$29', perks: ['5 parallel agents', 'AI orchestrator', 'Priority queue'] },
  { tier: 'enterprise', name: 'Enterprise', agents: 10, price: '$99', perks: ['10 parallel agents', 'Capacity-aware scaling', 'Admin tools'] },
]

export default function SettingsPage() {
  const { user } = useAuth()
  const currentTier = user?.tier ?? 'free'

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">Your profile, plan and agents</p>
      </div>

      <Card>
        <CardHeader><CardTitle className="text-base flex items-center gap-2"><UserIcon className="w-4 h-4" /> Profile</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <div className="flex justify-between"><span className="text-muted-foreground">Email</span><span className="text-foreground">{user?.email}</span></div>
          <div className="flex justify-between"><span className="text-muted-foreground">Role</span><span>{user?.isAdmin ? <Badge>Admin</Badge> : <Badge variant="secondary">User</Badge>}</span></div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base flex items-center gap-2"><Cpu className="w-4 h-4" /> Plan &amp; Agents</CardTitle></CardHeader>
        <CardContent className="space-y-1 text-sm">
          <div className="flex justify-between"><span className="text-muted-foreground">Current plan</span><Badge variant="secondary" className="capitalize">{currentTier}</Badge></div>
          <div className="flex justify-between"><span className="text-muted-foreground">Parallel agents</span><span className="font-medium">{user?.agentsLimit ?? 1}</span></div>
        </CardContent>
      </Card>

      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">Plans</h2>
        <div className="grid gap-4 md:grid-cols-3">
          {PLANS.map((p) => {
            const active = p.tier === currentTier
            return (
              <Card key={p.tier} className={active ? 'border-primary' : ''}>
                <CardContent className="p-5 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-foreground">{p.name}</span>
                    {active && <Badge>Current</Badge>}
                  </div>
                  <div className="text-2xl font-bold">{p.price}<span className="text-sm font-normal text-muted-foreground">/mo</span></div>
                  <ul className="space-y-1.5">
                    {p.perks.map((perk) => (
                      <li key={perk} className="flex items-center gap-2 text-sm text-muted-foreground">
                        <Check className="w-3.5 h-3.5 text-success" /> {perk}
                      </li>
                    ))}
                  </ul>
                  <Button
                    variant={active ? 'outline' : 'default'}
                    className="w-full"
                    disabled={active}
                    onClick={() => toast.info(user?.isAdmin
                      ? 'As admin, set tiers from the Admin panel.'
                      : 'Payments are not enabled in this test build — ask an admin to upgrade you.')}
                  >
                    <Sparkles className="w-4 h-4 mr-2" /> {active ? 'Active' : 'Upgrade'}
                  </Button>
                </CardContent>
              </Card>
            )
          })}
        </div>
      </div>
    </div>
  )
}
