'use client'

import { Cpu, User as UserIcon, FlaskConical } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { useAuth } from '@/lib/auth-context'

export default function SettingsPage() {
  const { user } = useAuth()
  const currentTier = user?.tier ?? 'free'

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">Your profile and agents</p>
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

      <Card className="border-warning/40 bg-warning/5">
        <CardContent className="p-5 flex items-start gap-3">
          <FlaskConical className="w-5 h-5 text-warning shrink-0 mt-0.5" />
          <div className="space-y-1">
            <div className="font-medium text-foreground">Test account</div>
            <p className="text-sm text-muted-foreground">
              Billing is disabled in this build. To raise your parallel-agent limit, ask an admin to bump it in the Admin panel.
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
