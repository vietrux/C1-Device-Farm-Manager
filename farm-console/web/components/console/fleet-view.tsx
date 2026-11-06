"use client"

// Dashboard "Tất cả máy": toàn bộ phone của mọi box trong 1 màn hình —
// mỗi máy 1 card với ảnh màn hình live, lọc theo box/tên/trạng thái,
// bấm card để mở điều khiển (ScreenDialog).

import * as React from "react"
import {
  LoaderCircleIcon,
  MonitorSmartphoneIcon,
  SearchIcon,
  SmartphoneIcon,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { api, enc, type Device, type Fleet } from "@/lib/api"

const FLEET_MS = 15000
const THUMB_MS = 45000

export function FleetView({
  onScreen,
  paused,
}: {
  onScreen: (box: string, device: string) => void
  paused: boolean
}) {
  const [fleet, setFleet] = React.useState<Fleet | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  const [thumbV, setThumbV] = React.useState(0)
  const [boxFilter, setBoxFilter] = React.useState<string>("*")
  const [q, setQ] = React.useState("")
  const [onlyRun, setOnlyRun] = React.useState(false)

  const refresh = React.useCallback(async () => {
    try {
      setFleet(await api<Fleet>("/api/fleet"))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  React.useEffect(() => {
    refresh()
    const t = setInterval(() => {
      if (!document.hidden && !paused) refresh()
    }, FLEET_MS)
    const th = setInterval(() => {
      if (!document.hidden && !paused) setThumbV((v) => v + 1)
    }, THUMB_MS)
    return () => {
      clearInterval(t)
      clearInterval(th)
    }
  }, [refresh, paused])

  const all = React.useMemo(
    () =>
      (fleet?.boxes ?? []).flatMap((b) =>
        b.devices
          .filter((d) => !d.vendor)
          .map((d) => ({ ...d, box: b.name, boxErr: b.error })),
      ),
    [fleet],
  )
  const boxesOnline = (fleet?.boxes ?? []).filter((b) => !b.error)
  const running = all.filter((d) => d.state === "running")
  const live = running.filter((d) => !d.gpu || !!d.boot_completed)

  const shown = all.filter(
    (d) =>
      (boxFilter === "*" || d.box === boxFilter) &&
      (!onlyRun || d.state === "running") &&
      (!q || d.name.toLowerCase().includes(q.toLowerCase())),
  )

  return (
    <div className="flex h-full flex-col">
      <header className="border-b bg-background">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-2">
            <MonitorSmartphoneIcon className="size-5 text-primary" />
            <h1 className="text-lg font-semibold">Tất cả máy</h1>
          </div>
          <div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
            <span>
              <b className="text-foreground">{running.length}</b>/{all.length} máy
            </span>
            <span className="hidden sm:inline">
              <b className="text-foreground">{live.length}</b> live
            </span>
            <span>
              <b className="text-foreground">{boxesOnline.length}</b>/
              {fleet?.boxes.length ?? 0} box
            </span>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <div className="relative">
              <SearchIcon className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="tìm máy…"
                className="h-9 w-44 pl-8"
              />
            </div>
            <Button
              variant={onlyRun ? "default" : "outline"}
              size="sm"
              className="h-9"
              onClick={() => setOnlyRun(!onlyRun)}
            >
              đang chạy
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-9"
              onClick={refresh}
              disabled={!fleet}
            >
              <LoaderCircleIcon
                className={cn(fleet && "animate-none")}
              />{" "}
              Làm mới
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 px-4 pb-3 sm:px-6">
          <Chip
            active={boxFilter === "*"}
            onClick={() => setBoxFilter("*")}
            label={`mọi box · ${all.length}`}
          />
          {(fleet?.boxes ?? []).map((b) => (
            <Chip
              key={b.name}
              active={boxFilter === b.name}
              onClick={() => setBoxFilter(b.name)}
              label={`${b.name} · ${b.devices.filter((d) => !d.vendor).length}`}
              error={!!b.error}
            />
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4 sm:p-6">
        {err && (
          <p className="mb-4 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm">
            Lỗi tải danh sách: {err}
          </p>
        )}
        {(fleet?.boxes ?? [])
          .filter((b) => b.error)
          .map((b) => (
            <p
              key={b.name}
              className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm"
            >
              <b>{b.name}</b>: {b.error}
            </p>
          ))}
        {!fleet ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <div
                key={i}
                className="aspect-[9/16] animate-pulse rounded-xl bg-muted"
              />
            ))}
          </div>
        ) : shown.length === 0 ? (
          <div className="grid place-items-center py-24 text-center text-muted-foreground">
            <SmartphoneIcon className="mb-3 size-10 opacity-40" />
            <p className="text-sm">
              Không có máy nào khớp bộ lọc — bấm “Làm mới” hoặc tạo máy mới ở
              trang box.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-4">
            {shown.map((d, i) => (
              <PhoneCard
                key={`${d.box}/${d.name}`}
                d={d}
                thumbV={thumbV}
                delayMs={(i % 12) * 250}
                onOpen={() => onScreen(d.box, d.name)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function PhoneCard({
  d,
  thumbV,
  delayMs,
  onOpen,
}: {
  d: Device & { box: string; boxErr?: string }
  thumbV: number
  delayMs: number
  onOpen: () => void
}) {
  const [ok, setOk] = React.useState(true)
  const live = d.state === "running" && (!d.gpu || !!d.boot_completed)
  const booting = d.state === "running" && !live
  const tone = live
    ? "bg-emerald-500"
    : booting
      ? "bg-amber-500 animate-pulse"
      : "bg-muted-foreground/50"

  return (
    <button
      onClick={onOpen}
      className="group overflow-hidden rounded-xl border bg-card text-left shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md focus-visible:outline-2"
    >
      <div className="relative aspect-[9/16] w-full overflow-hidden bg-gradient-to-br from-muted via-muted/60 to-muted/20">
        {ok ? (
          <img
            src={`/api/boxes/${enc(d.box)}/devices/${enc(
              d.name,
            )}/screen?v=${thumbV}`}
            alt={d.name}
            loading="lazy"
            style={{ animationDelay: `${delayMs}ms` }}
            className="size-full animate-in fade-in object-cover object-top duration-700"
            onError={() => setOk(false)}
          />
        ) : (
          <div className="grid size-full place-items-center text-muted-foreground">
            <SmartphoneIcon className="size-8 opacity-40" />
          </div>
        )}
        <div className="absolute inset-x-0 top-0 flex items-start justify-between gap-1 p-2">
          <span className="flex items-center gap-1.5 rounded-full bg-black/55 px-2 py-0.5 text-[11px] font-medium text-white backdrop-blur">
            <span className={cn("size-1.5 rounded-full", tone)} />
            <span className="max-w-24 truncate">{d.name}</span>
          </span>
          <span className="rounded-full bg-black/55 px-2 py-0.5 text-[10px] text-white/90 backdrop-blur">
            {d.box}
          </span>
        </div>
        {booting && (
          <span className="absolute inset-x-0 bottom-0 bg-amber-500/85 py-0.5 text-center text-[10px] font-medium text-black">
            đang boot…
          </span>
        )}
        {d.state !== "running" && (
          <span className="absolute inset-x-0 bottom-0 bg-muted-foreground/85 py-0.5 text-center text-[10px] font-medium text-white">
            {d.state}
          </span>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 px-2.5 py-2">
        <div className="min-w-0 leading-tight">
          <p className="truncate text-xs font-medium">{d.name}</p>
          <p className="truncate text-[10px] text-muted-foreground">
            {shortImage(d.image)}
          </p>
        </div>
        <span
          className={cn(
            "size-2 shrink-0 rounded-full",
            tone,
          )}
        />
      </div>
    </button>
  )
}

function shortImage(ref: string) {
  return ref.replace(/^.*dobox:/, "").replace(/^Q12_/, "")
}

function Chip({
  label,
  active,
  error,
  onClick,
}: {
  label: string
  active?: boolean
  error?: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-primary bg-primary text-primary-foreground"
          : "bg-background text-muted-foreground hover:bg-muted",
        error && !active && "border-amber-500/50 text-amber-600",
      )}
    >
      {label}
    </button>
  )
}
