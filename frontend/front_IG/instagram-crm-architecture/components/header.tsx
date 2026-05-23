'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { LogOut, Sparkles, Wifi, Shield } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useAuth } from '@/lib/auth-context'

const pathLabels: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/accounts': 'Accounts',
  '/orchestrator': 'AI Orchestrator',
  '/activity': 'Activity',
  '/tasks': 'Tasks',
  '/media': 'Media Library',
  '/settings': 'Settings',
  '/admin': 'Admin',
}

export function Header() {
  const pathname = usePathname()
  const { user, logout } = useAuth()

  const currentLabel = Object.entries(pathLabels).find(
    ([path]) => pathname === path || pathname.startsWith(`${path}/`)
  )?.[1] || 'Dashboard'

  return (
    <header className="h-14 border-b border-border bg-background/80 backdrop-blur-sm flex items-center justify-between px-6">
      {/* Breadcrumbs */}
      <div className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">Instagram CRM</span>
        <span className="text-muted-foreground">/</span>
        <span className="text-foreground font-medium">{currentLabel}</span>
      </div>

      {/* Right side */}
      <div className="flex items-center gap-4">
        {/* System Status */}
        <div className="flex items-center gap-2 text-sm">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-success opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-success"></span>
          </span>
          <span className="text-muted-foreground flex items-center gap-1">
            <Wifi className="w-3 h-3" />
            Workers Online
          </span>
        </div>

        {/* Upgrade Button */}
        <Button asChild variant="outline" size="sm" className="gap-1.5">
          <Link href="/settings">
            <Sparkles className="w-3.5 h-3.5" />
            Upgrade
          </Link>
        </Button>

        {/* User Menu */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" className="rounded-full">
              <div className="w-8 h-8 rounded-full bg-secondary flex items-center justify-center">
                <span className="text-xs font-medium text-secondary-foreground">
                  {user?.email?.charAt(0).toUpperCase() || 'U'}
                </span>
              </div>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <div className="px-2 py-1.5">
              <p className="text-sm font-medium">{user?.email || 'User'}</p>
              <p className="text-xs text-muted-foreground capitalize">{user?.tier ?? 'free'} plan</p>
            </div>
            <DropdownMenuSeparator />
            {user?.isAdmin && (
              <DropdownMenuItem asChild>
                <Link href="/admin"><Shield className="w-4 h-4 mr-2" /> Admin Panel</Link>
              </DropdownMenuItem>
            )}
            <DropdownMenuItem asChild>
              <Link href="/settings"><Sparkles className="w-4 h-4 mr-2" /> Settings</Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={logout} className="text-destructive focus:text-destructive">
              <LogOut className="w-4 h-4 mr-2" />
              Sign Out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  )
}
