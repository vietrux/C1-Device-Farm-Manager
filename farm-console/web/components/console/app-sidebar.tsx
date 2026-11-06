"use client"

import { LayoutGridIcon, PlusIcon, ServerIcon } from "lucide-react"

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
} from "@/components/ui/sidebar"
import type { Box, BoxInfo } from "@/lib/api"
import { cn } from "@/lib/utils"

export function AppSidebar({
  boxes,
  infos,
  current,
  onSelect,
  onAdd,
  onFleet,
  fleetActive,
  version,
}: {
  boxes: Box[] | null
  infos: Record<string, BoxInfo | { error: string }>
  current: string | null
  onSelect: (name: string) => void
  onAdd: () => void
  onFleet: () => void
  fleetActive: boolean
  version?: string
}) {
  return (
    <Sidebar>
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1.5">
          <div className="flex size-7 items-center justify-center bg-primary text-primary-foreground">
            <ServerIcon className="size-4" />
          </div>
          <div className="grid leading-tight">
            <span className="text-sm font-medium">farm-console</span>
            <span className="text-[10px] text-muted-foreground">
              RK3588 device farm {version && `· v${version}`}
            </span>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Tổng quan</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={fleetActive}
                  onClick={onFleet}
                  tooltip="Toàn bộ phone của mọi box"
                  size="lg"
                >
                  <LayoutGridIcon />
                  <span>Tất cả máy</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>Box</SidebarGroupLabel>
          <SidebarGroupAction title="Thêm box" onClick={onAdd}>
            <PlusIcon />
          </SidebarGroupAction>
          <SidebarGroupContent>
            <SidebarMenu>
              {!boxes &&
                Array.from({ length: 2 }).map((_, i) => (
                  <SidebarMenuItem key={i}>
                    <SidebarMenuSkeleton showIcon />
                  </SidebarMenuItem>
                ))}
              {boxes?.map((b) => {
                const i = infos[b.name]
                const err = i && "error" in i
                const info = i && !("error" in i) ? i : null
                const tone = !i
                  ? "bg-muted-foreground/40"
                  : err
                    ? "bg-destructive"
                    : info?.vendor_mytsvr === "running" ||
                        (info && info.load[0] > 8)
                      ? "bg-amber-500"
                      : "bg-emerald-500"
                return (
                  <SidebarMenuItem key={b.name}>
                    <SidebarMenuButton
                      isActive={b.name === current}
                      onClick={() => onSelect(b.name)}
                      tooltip={b.ssh}
                      size="lg"
                    >
                      <span
                        className={cn("size-2 shrink-0 rounded-full", tone)}
                      />
                      <div className="grid min-w-0 leading-tight">
                        <span className="truncate">{b.name}</span>
                        <span className="truncate text-[10px] text-muted-foreground">
                          {!i
                            ? "đang hỏi…"
                            : err
                              ? "mất kết nối"
                              : `load ${info!.load[0].toFixed(2)}`}
                        </span>
                      </div>
                    </SidebarMenuButton>
                    {info?.temp_c != null && (
                      <SidebarMenuBadge>
                        {info.temp_c.toFixed(0)}°
                      </SidebarMenuBadge>
                    )}
                  </SidebarMenuItem>
                )
              })}
              {boxes && boxes.length === 0 && (
                <p className="px-2 py-1 text-xs text-muted-foreground">
                  Chưa có box nào.
                </p>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={onAdd}>
              <PlusIcon /> Thêm box
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}
