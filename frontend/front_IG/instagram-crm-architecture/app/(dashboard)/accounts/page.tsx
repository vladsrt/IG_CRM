'use client'

import { useState } from 'react'
import { Plus, MoreHorizontal, RefreshCw, Trash2, AlertCircle, Loader2, Pencil, Server } from 'lucide-react'
import useSWR from 'swr'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Separator } from '@/components/ui/separator'
import { api, type Account, type Proxy, type CreateProxyPayload } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'
import { cn } from '@/lib/utils'

type ProxyProto = 'http' | 'https' | 'socks4' | 'socks5'

interface FormState {
  username: string
  authMethod: 'cookies' | 'manual'
  password: string
  cookies: string
  platform: 'windows' | 'macos' | 'linux'
  tags: string
  status: string
  // inline proxy (per-account)
  proxyHost: string
  proxyPort: string
  proxyUser: string
  proxyPass: string
  proxyProtocol: ProxyProto
}

const emptyForm: FormState = {
  username: '', authMethod: 'cookies', password: '', cookies: '', platform: 'windows',
  tags: '', status: '', proxyHost: '', proxyPort: '8080', proxyUser: '', proxyPass: '', proxyProtocol: 'http',
}

function StatusIndicator({ status }: { status: Account['status'] }) {
  const isActive = status === 'active' || status === 'valid'
  const isCheckpoint = status === 'checkpoint_required'
  return (
    <div className="flex items-center gap-2">
      <span className="relative flex h-2.5 w-2.5">
        {(isActive || isCheckpoint) && (
          <span className={cn('animate-ping absolute inline-flex h-full w-full rounded-full opacity-75', isActive && 'bg-success', isCheckpoint && 'bg-destructive')} />
        )}
        <span className={cn('relative inline-flex rounded-full h-2.5 w-2.5', isActive && 'bg-success', isCheckpoint && 'bg-destructive', !isActive && !isCheckpoint && 'bg-muted-foreground')} />
      </span>
      <span className={cn('text-sm capitalize', isActive && 'text-success', isCheckpoint && 'text-destructive', !isActive && !isCheckpoint && 'text-muted-foreground')}>
        {status === 'checkpoint_required' ? 'Checkpoint' : (status || 'unknown')}
      </span>
    </div>
  )
}

export default function AccountsPage() {
  const { user } = useAuth()
  const { data: accounts, error, mutate, isLoading } = useSWR<Account[]>('accounts', () => api.getAccounts())
  const { data: proxies, mutate: mutateProxies } = useSWR<Proxy[]>('proxies', () => api.getProxies().catch(() => []))

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Account | null>(null)
  const [form, setForm] = useState<FormState>(emptyForm)
  const [saving, setSaving] = useState(false)
  const [validatingId, setValidatingId] = useState<string | null>(null)

  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }))

  const openCreate = () => { setEditing(null); setForm(emptyForm); setOpen(true) }

  const openEdit = (acc: Account) => {
    const px = proxies?.find((p) => p.id === acc.proxy_id)
    setEditing(acc)
    setForm({
      ...emptyForm,
      username: acc.ig_username,
      authMethod: acc.auth_method,
      platform: acc.platform,
      tags: acc.tags.join(', '),
      status: acc.status || '',
      proxyHost: px?.host || '',
      proxyPort: px ? String(px.port) : '8080',
      proxyUser: px?.username || '',
      proxyPass: '',
      proxyProtocol: (px?.protocol as ProxyProto) || 'http',
    })
    setOpen(true)
  }

  // create / update the per-account proxy, return the proxy_id to attach (or null)
  const resolveProxyId = async (existing: Account | null): Promise<string | null> => {
    if (!form.proxyHost.trim()) return null // no proxy / remove
    const common = {
      host: form.proxyHost.trim(),
      port: Number(form.proxyPort) || 8080,
      username: form.proxyUser || '',
      protocol: form.proxyProtocol,
    }
    if (existing?.proxy_id) {
      const patch: Partial<CreateProxyPayload> = { ...common }
      if (form.proxyPass) patch.password = form.proxyPass // only change if provided
      await api.updateProxy(existing.proxy_id, patch)
      mutateProxies()
      return existing.proxy_id
    }
    const created = await api.createProxy({ ...common, password: form.proxyPass || '' })
    mutateProxies()
    return created.id
  }

  const save = async () => {
    if (!user) { toast.error('Not authenticated'); return }
    setSaving(true)
    try {
      const tags = form.tags.split(',').map((t) => t.trim().toLowerCase()).filter(Boolean)
      const proxyId = await resolveProxyId(editing)

      if (editing) {
        const patch: Record<string, unknown> = { tags, proxy_id: proxyId }
        if (form.status) patch.status = form.status
        if (form.authMethod === 'manual' && form.password) patch.ig_password = form.password
        if (form.authMethod === 'cookies' && form.cookies.trim()) {
          try { patch.cookies = JSON.parse(form.cookies) } catch { toast.error('Cookies must be valid JSON'); setSaving(false); return }
        }
        await api.updateAccount(editing.id, patch)
        toast.success(`@${editing.ig_username} updated`)
      } else {
        let parsedCookies: unknown[] = []
        if (form.authMethod === 'cookies' && form.cookies.trim()) {
          try { parsedCookies = JSON.parse(form.cookies) } catch { toast.error('Cookies must be valid JSON'); setSaving(false); return }
        }
        await api.createAccount({
          ig_username: form.username.replace(/^@/, '').trim(),
          ig_password: form.authMethod === 'manual' ? form.password : (form.password || ''),
          auth_method: form.authMethod,
          user_id: user.id,
          proxy_id: proxyId,
          cookies: parsedCookies,
          platform: form.platform,
          tags,
        })
        toast.success(`@${form.username} connected`)
      }
      setOpen(false)
      mutate()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try { await api.deleteAccount(id); toast.success('Account removed'); mutate() }
    catch (e) { toast.error(e instanceof Error ? e.message : 'Delete failed') }
  }

  const handleValidate = async (id: string) => {
    setValidatingId(id)
    try { await api.validateAccount(id); toast.success('Validation queued') }
    catch (e) { toast.error(e instanceof Error ? e.message : 'Failed to queue validation') }
    finally { setValidatingId(null) }
  }

  const formatLastCheck = (date: string | null) => {
    if (!date) return 'Never'
    const diff = Math.floor((Date.now() - new Date(date).getTime()) / 1000)
    if (diff < 60) return 'Just now'
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
    return new Date(date).toLocaleDateString()
  }

  const proxyFor = (acc: Account) => proxies?.find((p) => p.id === acc.proxy_id)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Accounts</h1>
          <p className="text-sm text-muted-foreground mt-1">Manage your Instagram fleet — proxy is set per account</p>
        </div>
        <Button onClick={openCreate}><Plus className="w-4 h-4 mr-2" /> Connect Account</Button>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 text-destructive px-4 py-3 text-sm">
          {error instanceof Error ? error.message : 'Failed to load accounts'}
        </div>
      )}

      <div className="rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-[200px]">Username</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Tags</TableHead>
              <TableHead>Proxy</TableHead>
              <TableHead>Platform</TableHead>
              <TableHead>Last Checked</TableHead>
              <TableHead className="w-[50px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 7 }).map((_, j) => (
                    <TableCell key={j}><div className="h-4 w-20 bg-secondary animate-pulse rounded" /></TableCell>
                  ))}
                </TableRow>
              ))
            ) : accounts && accounts.length > 0 ? (
              accounts.map((account) => {
                const px = proxyFor(account)
                return (
                  <TableRow key={account.id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 rounded-full bg-secondary flex items-center justify-center">
                          <span className="text-xs text-secondary-foreground">{account.ig_username.charAt(0).toUpperCase()}</span>
                        </div>
                        @{account.ig_username}
                      </div>
                    </TableCell>
                    <TableCell><StatusIndicator status={account.status} /></TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {account.tags.map((tag) => (<Badge key={tag} variant="secondary" className="text-xs">{tag}</Badge>))}
                      </div>
                    </TableCell>
                    <TableCell>
                      {px ? (
                        <span className="text-xs text-muted-foreground flex items-center gap-1"><Server className="w-3 h-3" />{px.host}:{px.port}</span>
                      ) : account.proxy_id ? (
                        <span className="text-xs text-muted-foreground">attached</span>
                      ) : (
                        <span className="text-xs text-muted-foreground/60">none</span>
                      )}
                    </TableCell>
                    <TableCell><span className="text-sm text-muted-foreground capitalize">{account.platform}</span></TableCell>
                    <TableCell><span className="text-sm text-muted-foreground">{formatLastCheck(account.last_check)}</span></TableCell>
                    <TableCell>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="icon-sm"><MoreHorizontal className="w-4 h-4" /></Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem onClick={() => openEdit(account)}>
                            <Pencil className="w-4 h-4 mr-2" /> Edit / Proxy
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleValidate(account.id)} disabled={validatingId === account.id}>
                            {validatingId === account.id ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <RefreshCw className="w-4 h-4 mr-2" />}
                            Validate Session
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => handleDelete(account.id)}>
                            <Trash2 className="w-4 h-4 mr-2" /> Remove Account
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                )
              })
            ) : (
              <TableRow>
                <TableCell colSpan={7} className="h-32 text-center">
                  <div className="flex flex-col items-center gap-2 text-muted-foreground">
                    <AlertCircle className="w-8 h-8" />
                    <p>No accounts connected yet</p>
                    <Button variant="outline" size="sm" onClick={openCreate}>Connect your first account</Button>
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {/* Add / Edit dialog */}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? `Edit @${editing.ig_username}` : 'Connect Instagram Account'}</DialogTitle>
            <DialogDescription>{editing ? 'Update tags, status and the per-account proxy.' : 'Add an account and (optionally) its proxy.'}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            {!editing && (
              <div className="space-y-2">
                <Label>Instagram Username</Label>
                <Input placeholder="@username" value={form.username} onChange={(e) => set({ username: e.target.value })} />
              </div>
            )}

            <div className="space-y-2">
              <Label>Auth method</Label>
              <Select value={form.authMethod} onValueChange={(v) => set({ authMethod: v as 'cookies' | 'manual' })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="cookies">Session cookies</SelectItem>
                  <SelectItem value="manual">Username / password</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {form.authMethod === 'manual' ? (
              <div className="space-y-2">
                <Label>
                  Password{' '}
                  {editing && (editing.has_password
                    ? <span className="text-success">✓ set (leave blank to keep)</span>
                    : <span className="text-muted-foreground">(none stored)</span>)}
                </Label>
                <Input type="password" value={form.password} onChange={(e) => set({ password: e.target.value })} />
              </div>
            ) : (
              <div className="space-y-2">
                <Label>
                  Session Cookies (JSON){' '}
                  {editing && (editing.has_cookies
                    ? <span className="text-success">✓ stored (leave blank to keep)</span>
                    : <span className="text-muted-foreground">(none stored)</span>)}
                </Label>
                <Textarea placeholder='[{"name":"sessionid","value":"..."}]' className="min-h-24 font-mono text-xs" value={form.cookies} onChange={(e) => set({ cookies: e.target.value })} />
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-2">
                <Label>Platform</Label>
                <Select value={form.platform} onValueChange={(v) => set({ platform: v as FormState['platform'] })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="windows">Windows</SelectItem>
                    <SelectItem value="macos">macOS</SelectItem>
                    <SelectItem value="linux">Linux</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {editing && (
                <div className="space-y-2">
                  <Label>Status</Label>
                  <Select value={form.status || 'active'} onValueChange={(v) => set({ status: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="active">active</SelectItem>
                      <SelectItem value="checkpoint_required">checkpoint</SelectItem>
                      <SelectItem value="inactive">inactive</SelectItem>
                      <SelectItem value="banned">banned</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>

            <div className="space-y-2">
              <Label>Tags (comma-separated)</Label>
              <Input placeholder="crypto, finance" value={form.tags} onChange={(e) => set({ tags: e.target.value })} />
            </div>

            <Separator />
            <div className="flex items-center gap-2 text-sm font-medium text-foreground">
              <Server className="w-4 h-4" /> Proxy <span className="text-muted-foreground font-normal">(per account — leave host blank for none)</span>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="col-span-2 space-y-1.5">
                <Label className="text-xs">Host</Label>
                <Input value={form.proxyHost} onChange={(e) => set({ proxyHost: e.target.value })} placeholder="1.2.3.4" />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Port</Label>
                <Input type="number" value={form.proxyPort} onChange={(e) => set({ proxyPort: e.target.value })} />
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="space-y-1.5">
                <Label className="text-xs">User</Label>
                <Input value={form.proxyUser} onChange={(e) => set({ proxyUser: e.target.value })} />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Pass {editing && <span className="text-muted-foreground">(blank=keep)</span>}</Label>
                <Input type="password" value={form.proxyPass} onChange={(e) => set({ proxyPass: e.target.value })} />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Protocol</Label>
                <Select value={form.proxyProtocol} onValueChange={(v) => set({ proxyProtocol: v as ProxyProto })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {(['http', 'https', 'socks4', 'socks5'] as ProxyProto[]).map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={save} disabled={saving || (!editing && !form.username)}>
              {saving ? 'Saving...' : editing ? 'Save changes' : 'Connect Account'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
