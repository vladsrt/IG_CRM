'use client'

import { useState, useRef, useEffect } from 'react'
import {
  Send, AlertTriangle, Check, Loader2, Rocket, Clock, Target, Zap,
  Plus, Trash2, X, Users,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger,
} from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Users as UsersIcon, RotateCcw, Film } from 'lucide-react'
import { api, type Account, type Asset, type ParsedTaskPlan, type FanOutResponse, type ActionArg } from '@/lib/api'
import { cn } from '@/lib/utils'

const INPUT_KEY = 'orch_input'
const MSGS_KEY = 'orch_messages'

interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: Date
}

type TargetMode = 'tags' | 'all' | 'select'

// Actions the worker can run + a template of args, so the user picks from a
// list instead of guessing action names / arg keys.
const ACTION_DEFS: { value: string; label: string; hint: string; args: ActionArg[] }[] = [
  { value: 'warmup', label: 'Warmup', hint: 'scroll/like feed for N minutes', args: [{ key: 'duration_minutes', value: '15' }] },
  { value: 'upload_reels', label: 'Upload Reel', hint: 'post a video reel', args: [{ key: 'file_path', value: '' }, { key: 'caption', value: '' }] },
  { value: 'upload_post', label: 'Upload Post', hint: 'post a photo/video', args: [{ key: 'file_path', value: '' }, { key: 'caption', value: '' }] },
  { value: 'upload_story', label: 'Upload Story', hint: 'post a story', args: [{ key: 'file_path', value: '' }] },
  { value: 'update_profile', label: 'Update Profile', hint: 'bio / avatar / privacy', args: [{ key: 'bio', value: '' }] },
  { value: 'send_dm', label: 'Send DM', hint: 'message a user', args: [{ key: 'recipient', value: '' }, { key: 'text', value: '' }] },
  { value: 'comment_post', label: 'Comment', hint: 'comment on a post', args: [{ key: 'url', value: '' }, { key: 'text', value: '' }] },
  { value: 'like_post', label: 'Like Post', hint: 'like a post by url', args: [{ key: 'url', value: '' }] },
  { value: 'follow_user', label: 'Follow', hint: 'follow a username', args: [{ key: 'username', value: '' }] },
  { value: 'unfollow_user', label: 'Unfollow', hint: 'unfollow a username', args: [{ key: 'username', value: '' }] },
]

function actionIcon(action: string) {
  switch (action) {
    case 'warmup':
      return <Clock className="w-4 h-4" />
    case 'upload_reels':
    case 'upload_post':
    case 'upload_story':
      return <Rocket className="w-4 h-4" />
    default:
      return <Zap className="w-4 h-4" />
  }
}

const actionLabel = (a: string) => a.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())

// ---- Editable command card -------------------------------------------------
function CommandCard({
  command, index, isLast, onChange, onRemove,
}: {
  command: { action: string; args: ActionArg[] }
  index: number
  isLast: boolean
  onChange: (args: ActionArg[]) => void
  onRemove: () => void
}) {
  const setArg = (i: number, patch: Partial<ActionArg>) =>
    onChange(command.args.map((a, j) => (j === i ? { ...a, ...patch } : a)))
  const removeArg = (i: number) => onChange(command.args.filter((_, j) => j !== i))
  const addArg = () => onChange([...command.args, { key: '', value: '' }])

  return (
    <div className="flex gap-3">
      <div className="flex flex-col items-center">
        <div className="w-8 h-8 rounded-full bg-secondary flex items-center justify-center text-foreground shrink-0">
          {actionIcon(command.action)}
        </div>
        {!isLast && <div className="w-px h-full bg-border min-h-6" />}
      </div>
      <div className="flex-1 pb-5">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Step {index + 1}</span>
            <Badge variant="outline" className="text-xs">{actionLabel(command.action)}</Badge>
          </div>
          <Button variant="ghost" size="icon" className="h-6 w-6 text-muted-foreground hover:text-destructive" onClick={onRemove}>
            <Trash2 className="w-3.5 h-3.5" />
          </Button>
        </div>
        <div className="space-y-1.5">
          {command.args.map((arg, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <Input
                value={arg.key}
                onChange={(e) => setArg(i, { key: e.target.value })}
                placeholder="key"
                className="h-7 text-xs w-1/3"
              />
              <Input
                value={arg.value}
                onChange={(e) => setArg(i, { value: e.target.value })}
                placeholder="value"
                className="h-7 text-xs flex-1"
              />
              <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground hover:text-destructive" onClick={() => removeArg(i)}>
                <X className="w-3.5 h-3.5" />
              </Button>
            </div>
          ))}
          <Button variant="ghost" size="sm" className="h-6 text-xs text-muted-foreground" onClick={addArg}>
            <Plus className="w-3 h-3 mr-1" /> add arg
          </Button>
        </div>
      </div>
    </div>
  )
}

// ---- Plan preview / editor -------------------------------------------------
function TaskPlanPreview({
  plan, accounts, targetMode, setTargetMode, selectedIds, toggleId, selectAll, clearSel,
  onChangePlan, onApprove, isDispatching, dispatchResult,
}: {
  plan: ParsedTaskPlan | null
  accounts: Account[]
  targetMode: TargetMode
  setTargetMode: (m: TargetMode) => void
  selectedIds: Set<string>
  toggleId: (id: string) => void
  selectAll: () => void
  clearSel: () => void
  onChangePlan: (p: ParsedTaskPlan) => void
  onApprove: () => void
  isDispatching: boolean
  dispatchResult: FanOutResponse | null
}) {
  if (!plan) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-center p-8">
        <div className="w-16 h-16 rounded-full bg-secondary flex items-center justify-center mb-4">
          <Target className="w-8 h-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-medium text-foreground mb-2">No Task Plan Yet</h3>
        <p className="text-sm text-muted-foreground max-w-xs">
          Type your instructions in the chat and the AI will generate an editable task plan.
        </p>
      </div>
    )
  }

  const setCommandArgs = (cmdIdx: number, args: ActionArg[]) =>
    onChangePlan({ ...plan, commands: plan.commands.map((c, i) => (i === cmdIdx ? { ...c, args } : c)) })
  const removeCommand = (cmdIdx: number) =>
    onChangePlan({ ...plan, commands: plan.commands.filter((_, i) => i !== cmdIdx) })
  const addCommand = (action: string) => {
    const def = ACTION_DEFS.find((d) => d.value === action)
    const args = def ? def.args.map((a) => ({ ...a })) : []
    onChangePlan({ ...plan, commands: [...plan.commands, { action, args }] })
  }

  const targetCount =
    targetMode === 'all' ? accounts.length : targetMode === 'select' ? selectedIds.size : -1

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b border-border">
        <h3 className="font-semibold text-foreground">Task Plan</h3>
        <p className="text-sm text-muted-foreground mt-1">{plan.summary}</p>
      </div>

      <ScrollArea className="flex-1">
        <div className="p-4 space-y-5">
          <div className="flex flex-wrap gap-2 items-center">
            <Badge variant={plan.priority === 'high' || plan.priority === 'urgent' ? 'default' : 'secondary'}>
              Priority: {plan.priority}
            </Badge>
            {plan.target_tags.map((tag) => (
              <Badge key={tag} variant="outline">{tag}</Badge>
            ))}
          </div>

          {/* Targeting */}
          <div className="rounded-lg border border-border p-3 space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-foreground">
              <Users className="w-4 h-4" /> Targeting
            </div>
            <div className="flex gap-1.5">
              {(['tags', 'all', 'select'] as TargetMode[]).map((m) => (
                <Button
                  key={m}
                  size="sm"
                  variant={targetMode === m ? 'default' : 'outline'}
                  className="h-7 text-xs flex-1"
                  onClick={() => setTargetMode(m)}
                >
                  {m === 'tags' ? 'By tags' : m === 'all' ? 'All' : 'Pick'}
                </Button>
              ))}
            </div>
            {targetMode === 'tags' && (
              <p className="text-xs text-muted-foreground">
                {plan.target_tags.length > 0
                  ? `Accounts tagged: ${plan.target_tags.join(', ')}`
                  : 'No tags in plan — pick "All" or "Pick" instead.'}
              </p>
            )}
            {targetMode === 'all' && (
              <p className="text-xs text-muted-foreground">All {accounts.length} of your accounts.</p>
            )}
            {targetMode === 'select' && (
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Button size="sm" variant="ghost" className="h-6 text-xs" onClick={selectAll}>Select all</Button>
                  <Button size="sm" variant="ghost" className="h-6 text-xs" onClick={clearSel}>Clear</Button>
                </div>
                <div className="max-h-40 overflow-y-auto space-y-1">
                  {accounts.length === 0 && <p className="text-xs text-muted-foreground">No accounts yet.</p>}
                  {accounts.map((a) => (
                    <label key={a.id} className="flex items-center gap-2 text-xs cursor-pointer py-0.5">
                      <Checkbox checked={selectedIds.has(a.id)} onCheckedChange={() => toggleId(a.id)} />
                      <span className="text-foreground">@{a.ig_username}</span>
                      <span className="text-muted-foreground">{a.tags?.join(', ')}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Commands (editable) */}
          <div>
            <h4 className="text-sm font-medium text-foreground mb-3">Execution Steps</h4>
            {plan.commands.length === 0 ? (
              <p className="text-xs text-muted-foreground mb-2">No steps yet — add one below.</p>
            ) : (
              plan.commands.map((command, index) => (
                <CommandCard
                  key={index}
                  command={command}
                  index={index}
                  isLast={index === plan.commands.length - 1}
                  onChange={(args) => setCommandArgs(index, args)}
                  onRemove={() => removeCommand(index)}
                />
              ))
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="w-full mt-1">
                  <Plus className="w-3.5 h-3.5 mr-1" /> Add step
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent className="w-64">
                {ACTION_DEFS.map((d) => (
                  <DropdownMenuItem key={d.value} onClick={() => addCommand(d.value)} className="flex flex-col items-start gap-0.5">
                    <span className="text-sm font-medium">{d.label}</span>
                    <span className="text-xs text-muted-foreground">{d.hint}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          {dispatchResult && (
            <div className="rounded-lg border border-border bg-card p-4 space-y-3">
              <div className="flex items-center gap-2 text-success">
                <Check className="w-4 h-4" />
                <span className="font-medium">Dispatched</span>
              </div>
              <div className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <span className="text-muted-foreground">Dispatched</span>
                  <p className="text-foreground font-medium">{dispatchResult.dispatched_count} accounts</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Skipped</span>
                  <p className="text-foreground font-medium">{dispatchResult.skipped_count} accounts</p>
                </div>
              </div>
              {dispatchResult.skipped.length > 0 && (
                <div className="text-xs text-muted-foreground">
                  {dispatchResult.skipped.map((s, i) => (
                    <div key={i}>· {s.reason}</div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </ScrollArea>

      {!dispatchResult && (
        <div className="p-4 border-t border-border space-y-2">
          {targetCount === 0 && (
            <p className="text-xs text-destructive">Select at least one target account.</p>
          )}
          <Button
            onClick={onApprove}
            disabled={isDispatching || !!plan.clarification_needed || plan.commands.length === 0 || targetCount === 0}
            className="w-full h-12 text-base font-semibold"
          >
            {isDispatching ? (
              <><Loader2 className="w-5 h-5 mr-2 animate-spin" /> Dispatching...</>
            ) : (
              <><Rocket className="w-5 h-5 mr-2" /> Approve &amp; Fan-Out</>
            )}
          </Button>
        </div>
      )}
    </div>
  )
}

export default function OrchestratorPage() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: '1',
      role: 'assistant',
      content:
        'Hi! Tell me what to do with your Instagram accounts — e.g. "warm up my crypto accounts for 15 min, then post /media/reel.mp4 with caption gm".',
      timestamp: new Date(),
    },
  ])
  const [input, setInput] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [currentPlan, setCurrentPlan] = useState<ParsedTaskPlan | null>(null)
  const [isDispatching, setIsDispatching] = useState(false)
  const [dispatchResult, setDispatchResult] = useState<FanOutResponse | null>(null)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [targetMode, setTargetMode] = useState<TargetMode>('tags')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [selectedMediaIds, setSelectedMediaIds] = useState<Set<string>>(new Set())
  const [sheetOpen, setSheetOpen] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.getAccounts().then(setAccounts).catch(() => setAccounts([]))
    api.getAssets().then(setAssets).catch(() => setAssets([]))
  }, [])

  // restore chat input + history so switching tabs doesn't wipe what you typed
  useEffect(() => {
    try {
      const savedInput = localStorage.getItem(INPUT_KEY)
      if (savedInput) setInput(savedInput)
      const savedMsgs = localStorage.getItem(MSGS_KEY)
      if (savedMsgs) {
        const parsed = JSON.parse(savedMsgs) as Message[]
        if (Array.isArray(parsed) && parsed.length) {
          setMessages(parsed.map((m) => ({ ...m, timestamp: new Date(m.timestamp) })))
        }
      }
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { try { localStorage.setItem(INPUT_KEY, input) } catch { /* */ } }, [input])
  useEffect(() => { try { localStorage.setItem(MSGS_KEY, JSON.stringify(messages)) } catch { /* */ } }, [messages])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const addMessage = (role: Message['role'], content: string) =>
    setMessages((prev) => [...prev, { id: `${Date.now()}-${Math.random()}`, role, content, timestamp: new Date() }])

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!input.trim() || isGenerating) return

    const prompt = input.trim()
    addMessage('user', prompt)
    setInput('')
    setIsGenerating(true)
    setDispatchResult(null)

    try {
      const plan = await api.generateTask(prompt)
      if (plan.clarification_needed) {
        addMessage('assistant', plan.clarification_needed)
        setCurrentPlan(null)
      } else {
        setCurrentPlan(plan)
        setTargetMode(plan.target_tags.length > 0 ? 'tags' : 'all')
        addMessage(
          'assistant',
          `Plan ready: ${plan.commands.length} step(s)${
            plan.target_tags.length ? `, tags: ${plan.target_tags.join(', ')}` : ''
          }. Review and edit it on the right, choose targets, then Approve.`,
        )
      }
    } catch (err) {
      addMessage('system', err instanceof Error ? err.message : 'Failed to generate the plan.')
    } finally {
      setIsGenerating(false)
    }
  }

  const handleApprove = async () => {
    if (!currentPlan) return
    setIsDispatching(true)
    try {
      const ids =
        targetMode === 'all'
          ? accounts.map((a) => a.id)
          : targetMode === 'select'
            ? Array.from(selectedIds)
            : []
      const result = await api.fanOut(currentPlan, ids)
      setDispatchResult(result)
      addMessage(
        'assistant',
        `Dispatched to ${result.dispatched_count} account(s), skipped ${result.skipped_count}. Track them on the Tasks / Activity page.`,
      )
    } catch (err) {
      addMessage('system', err instanceof Error ? err.message : 'Fan-out failed.')
    } finally {
      setIsDispatching(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const toggleId = (id: string) =>
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  const selectAll = () => setSelectedIds(new Set(accounts.map((a) => a.id)))
  const clearSel = () => setSelectedIds(new Set())

  const toggleMedia = (id: string) =>
    setSelectedMediaIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })

  const insertText = (text: string) =>
    setInput((prev) => (prev ? prev.trimEnd() + ' ' : '') + text)

  const insertAccounts = () => {
    const chosen = accounts.filter((a) => selectedIds.has(a.id))
    if (!chosen.length) return
    insertText(`accounts: ${chosen.map((a) => '@' + a.ig_username).join(', ')}`)
    setTargetMode('select') // chosen accounts also become the fan-out targets
    setSheetOpen(false)
  }

  const insertMedia = () => {
    const chosen = assets.filter((a) => selectedMediaIds.has(a.id))
    if (!chosen.length) return
    insertText(chosen.map((a) => a.file_path).join(' '))
    setSheetOpen(false)
  }

  const newChat = () => {
    setMessages([{ id: 'greet', role: 'assistant', content: 'New chat — what should I do with your accounts?', timestamp: new Date() }])
    setInput('')
    setCurrentPlan(null)
    setDispatchResult(null)
    try { localStorage.removeItem(MSGS_KEY); localStorage.removeItem(INPUT_KEY) } catch { /* */ }
  }

  const mediaName = (a: Asset) =>
    (a.metadata?.original_filename as string) || a.file_path.split('/').pop() || 'file'

  return (
    <div className="h-[calc(100vh-7rem)] flex gap-6">
      {/* Chat */}
      <div className="flex-1 flex flex-col rounded-lg border border-border bg-card overflow-hidden">
        <ScrollArea className="flex-1">
          <div className="p-4 space-y-4">
            {messages.map((message) => (
              <div key={message.id} className={cn('flex gap-3', message.role === 'user' && 'flex-row-reverse')}>
                <div
                  className={cn(
                    'w-8 h-8 rounded-full flex items-center justify-center shrink-0',
                    message.role === 'user' && 'bg-primary text-primary-foreground',
                    message.role === 'assistant' && 'bg-secondary text-secondary-foreground',
                    message.role === 'system' && 'bg-destructive/20 text-destructive',
                  )}
                >
                  {message.role === 'user' && 'U'}
                  {message.role === 'assistant' && 'AI'}
                  {message.role === 'system' && <AlertTriangle className="w-4 h-4" />}
                </div>
                <div
                  className={cn(
                    'max-w-[80%] rounded-lg px-4 py-2',
                    message.role === 'user' && 'bg-primary text-primary-foreground',
                    message.role === 'assistant' && 'bg-secondary text-secondary-foreground',
                    message.role === 'system' && 'bg-destructive/10 text-destructive border border-destructive/20',
                  )}
                >
                  <p className="text-sm whitespace-pre-wrap">{message.content}</p>
                  <p className="text-xs opacity-60 mt-1">
                    {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </p>
                </div>
              </div>
            ))}
            {isGenerating && (
              <div className="flex gap-3">
                <div className="w-8 h-8 rounded-full bg-secondary flex items-center justify-center">AI</div>
                <div className="bg-secondary rounded-lg px-4 py-3">
                  <div className="flex gap-1">
                    <span className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        </ScrollArea>

        <div className="p-4 border-t border-border space-y-2">
          <div className="flex items-center justify-between">
            <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={newChat}>
              <RotateCcw className="w-4 h-4 mr-1.5" /> New chat
            </Button>
            <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="sm">
                  <UsersIcon className="w-4 h-4 mr-1.5" /> Accounts &amp; Media
                </Button>
              </SheetTrigger>
              <SheetContent className="flex flex-col">
                <SheetHeader><SheetTitle>Pick accounts &amp; media → insert into prompt</SheetTitle></SheetHeader>
                <Tabs defaultValue="accounts" className="flex-1 flex flex-col mt-3 min-h-0">
                  <TabsList className="grid grid-cols-2">
                    <TabsTrigger value="accounts">Accounts ({accounts.length})</TabsTrigger>
                    <TabsTrigger value="media">Media ({assets.length})</TabsTrigger>
                  </TabsList>

                  <TabsContent value="accounts" className="flex-1 min-h-0 flex flex-col">
                    <div className="flex-1 overflow-y-auto space-y-2 pr-1 mt-2">
                      {accounts.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No accounts yet.</p>
                      ) : accounts.map((a) => (
                        <label key={a.id} className="flex items-start gap-2 rounded-lg border border-border p-2.5 cursor-pointer">
                          <Checkbox checked={selectedIds.has(a.id)} onCheckedChange={() => toggleId(a.id)} className="mt-0.5" />
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-medium">@{a.ig_username}</span>
                              <span className="text-xs text-muted-foreground capitalize">{a.status || 'unknown'}</span>
                            </div>
                            <div className="flex flex-wrap gap-1 mt-1">
                              {a.tags.length ? a.tags.map((t) => <Badge key={t} variant="secondary" className="text-[10px]">{t}</Badge>)
                                : <span className="text-[10px] text-muted-foreground">no tags</span>}
                            </div>
                          </div>
                        </label>
                      ))}
                    </div>
                    <Button className="mt-2" disabled={selectedIds.size === 0} onClick={insertAccounts}>
                      Insert {selectedIds.size} account(s) → prompt &amp; target
                    </Button>
                  </TabsContent>

                  <TabsContent value="media" className="flex-1 min-h-0 flex flex-col">
                    <div className="flex-1 overflow-y-auto space-y-2 pr-1 mt-2">
                      {assets.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No media yet.</p>
                      ) : assets.map((a) => (
                        <label key={a.id} className="flex items-start gap-2 rounded-lg border border-border p-2.5 cursor-pointer">
                          <Checkbox checked={selectedMediaIds.has(a.id)} onCheckedChange={() => toggleMedia(a.id)} className="mt-0.5" />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <Film className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                              <span className="text-sm font-medium truncate">{mediaName(a)}</span>
                              {a.is_unique && <Badge variant="secondary" className="text-[10px]">copy</Badge>}
                            </div>
                            <code className="text-[10px] text-muted-foreground truncate block mt-0.5">{a.file_path}</code>
                          </div>
                        </label>
                      ))}
                    </div>
                    <Button className="mt-2" disabled={selectedMediaIds.size === 0} onClick={insertMedia}>
                      Insert {selectedMediaIds.size} file path(s) → prompt
                    </Button>
                  </TabsContent>
                </Tabs>
              </SheetContent>
            </Sheet>
          </div>
          <form onSubmit={handleSubmit} className="flex gap-2">
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type your instructions..."
              className="min-h-[44px] max-h-32 resize-none"
              rows={1}
            />
            <Button type="submit" size="icon" disabled={!input.trim() || isGenerating}>
              <Send className="w-4 h-4" />
            </Button>
          </form>
          <p className="text-xs text-muted-foreground mt-2">Enter to send · Shift+Enter for a new line</p>
        </div>
      </div>

      {/* Plan editor */}
      <div className="w-[26rem] rounded-lg border border-border bg-card overflow-hidden">
        <TaskPlanPreview
          plan={currentPlan}
          accounts={accounts}
          targetMode={targetMode}
          setTargetMode={setTargetMode}
          selectedIds={selectedIds}
          toggleId={toggleId}
          selectAll={selectAll}
          clearSel={clearSel}
          onChangePlan={setCurrentPlan}
          onApprove={handleApprove}
          isDispatching={isDispatching}
          dispatchResult={dispatchResult}
        />
      </div>
    </div>
  )
}
