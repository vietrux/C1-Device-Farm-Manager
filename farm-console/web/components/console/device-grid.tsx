"use client"

import * as React from "react"
import {
  EllipsisVerticalIcon,
  GlobeIcon,
  MonitorSmartphoneIcon,
  PlayIcon,
  PowerOffIcon,
  RotateCwIcon,
  ShieldCheckIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Spinner } from "@/components/ui/spinner"
import { authHeaders, enc, type Device } from "@/lib/api"
import { cn } from "@/lib/utils"

// mỗi ô làm mới ảnh màn hình sau chừng này ms (ảnh nhỏ, nhưng N máy = N lần screencap)
const THUMB_MS = 2500

export type DeviceActions = {
  onScreen: (name: string) => void
  action: (name: string, a: string) => void
  remove: (d: Device) => void
  integrity: (name: string) => void
  proxy: (name: string) => void
}

export function deviceState(d: Device) {
  const run = d.state === "running"
  const booted = !d.gpu || !!d.boot_completed
  const auto = d.restart === "unless-stopped" || d.restart === "always"
  const tone: "good" | "warn" | "bad" | "idle" = run
    ? booted
      ? "good"
      : "warn"
    : d.state === "exited"
      ? "idle"
      : "bad"
  return { run, booted, auto, tone }
}

export function Dot({ tone }: { tone: "good" | "warn" | "bad" | "idle" }) {
  return (
    <span
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        tone === "good" && "bg-emerald-500",
        tone === "warn" && "bg-amber-500",
        tone === "bad" && "bg-destructive",
        tone === "idle" && "bg-muted-foreground/40"
      )}
    />
  )
}

export function DeviceMenu({
  d,
  acts,
  className,
}: {
  d: Device
  acts: DeviceActions
  className?: string
}) {
  const { run, auto } = deviceState(d)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="ghost" size="icon-xs" className={className} />}
      >
        <EllipsisVerticalIcon />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-44">
        {run ? (
          <>
            <DropdownMenuItem onClick={() => acts.onScreen(d.name)}>
              <MonitorSmartphoneIcon /> Mở màn hình
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => acts.action(d.name, "restart")}>
              <RotateCwIcon /> Khởi động lại
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => acts.action(d.name, "stop")}>
              <SquareIcon /> Dừng
            </DropdownMenuItem>
          </>
        ) : (
          <DropdownMenuItem onClick={() => acts.action(d.name, "start")}>
            <PlayIcon /> Chạy
          </DropdownMenuItem>
        )}
        <DropdownMenuCheckboxItem
          checked={auto}
          onCheckedChange={(v) =>
            acts.action(d.name, v ? "autostart-on" : "autostart-off")
          }
        >
          Tự chạy khi box khởi động
        </DropdownMenuCheckboxItem>
        {run && (
          <>
            <DropdownMenuItem onClick={() => acts.integrity(d.name)}>
              <ShieldCheckIcon /> Integrity-fix…
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => acts.proxy(d.name)}>
              <GlobeIcon /> Proxy egress…
            </DropdownMenuItem>
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onClick={() => acts.remove(d)}>
          <Trash2Icon /> Xoá
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// Ảnh màn hình thu nhỏ, tự làm mới. Dừng khi tab ẩn hoặc khi đang mở màn hình lớn.
function Thumb({
  box,
  name,
  paused,
  delay,
}: {
  box: string
  name: string
  paused: boolean
  delay: number
}) {
  const [src, setSrc] = React.useState<string | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  const hostRef = React.useRef<HTMLDivElement>(null)
  const [visible, setVisible] = React.useState(false)

  // chỉ poll ô đang hiển thị trong khung nhìn (cuộn/nhiều máy = tiết kiệm nhiều)
  React.useEffect(() => {
    const el = hostRef.current
    if (!el || typeof IntersectionObserver === "undefined") {
      setVisible(true)
      return
    }
    const io = new IntersectionObserver(
      ([e]) => setVisible(e.isIntersecting),
      { rootMargin: "100px" }
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  React.useEffect(() => {
    if (paused || !visible) return
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    let last: string | null = null
    const tick = async () => {
      if (stop) return
      if (!document.hidden) {
        try {
          const r = await fetch(
            `/api/boxes/${enc(box)}/devices/${enc(name)}/screen?t=${Date.now()}`,
            { headers: authHeaders() }
          )
          if (!r.ok)
            throw new Error(
              ((await r.json()) as { detail?: string }).detail || r.statusText
            )
          const url = URL.createObjectURL(await r.blob())
          if (stop) {
            URL.revokeObjectURL(url)
            return
          }
          if (last) URL.revokeObjectURL(last)
          last = url
          setSrc(url)
          setErr(null)
        } catch (e) {
          if (!stop) setErr((e as Error).message)
        }
      }
      if (!stop) timer = setTimeout(tick, THUMB_MS)
    }
    // rải đều các ô để không chụp tất cả máy cùng một lúc
    timer = setTimeout(tick, delay)
    return () => {
      stop = true
      clearTimeout(timer)
      if (last) URL.revokeObjectURL(last)
    }
  }, [box, name, paused, delay, visible])

  return (
    <div ref={hostRef} className="absolute inset-0">
      {src && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={name}
          draggable={false}
          className="size-full object-contain"
        />
      )}
      {!src && (
        <div className="absolute inset-0 grid place-items-center p-3 text-center text-[10px] text-white/60">
          {err ? err : <Spinner />}
        </div>
      )}
    </div>
  )
}

function DeviceTile({
  box,
  d,
  acts,
  paused,
  index,
}: {
  box: string
  d: Device
  acts: DeviceActions
  paused: boolean
  index: number
}) {
  const { run, booted, tone } = deviceState(d)
  const live = run && booted
  return (
    <div className="group flex min-w-0 flex-col ring-1 ring-foreground/10">
      <button
        type="button"
        disabled={!live}
        onClick={() => acts.onScreen(d.name)}
        className={cn(
          "relative aspect-[9/16] w-full overflow-hidden bg-black outline-none",
          live && "cursor-pointer focus-visible:ring-2 focus-visible:ring-ring"
        )}
        title={live ? `Mở màn hình ${d.name}` : undefined}
      >
        {live ? (
          <>
            <Thumb
              box={box}
              name={d.name}
              paused={paused}
              delay={(index % 6) * 400}
            />
            <span className="absolute inset-0 grid place-items-center bg-black/0 text-xs font-medium text-white opacity-0 transition group-hover:bg-black/40 group-hover:opacity-100">
              <span className="flex items-center gap-1.5">
                <MonitorSmartphoneIcon className="size-4" /> Điều khiển
              </span>
            </span>
          </>
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-xs text-white/60">
            {run ? (
              <>
                <Spinner /> đang boot…
              </>
            ) : (
              <>
                <PowerOffIcon className="size-6" />
                {d.state}
              </>
            )}
          </div>
        )}
      </button>
      <div className="flex items-center gap-2 px-2 py-1.5">
        <Dot tone={tone} />
        <div className="grid min-w-0 flex-1 leading-tight">
          <span className="truncate text-xs font-medium">{d.name}</span>
          <span className="truncate text-[10px] text-muted-foreground tabular-nums">
            {d.ip || "—"} · binder {d.binders.join("/")}
          </span>
        </div>
        {!run && (
          <Button
            size="icon-xs"
            variant="outline"
            title="Chạy"
            onClick={() => acts.action(d.name, "start")}
          >
            <PlayIcon />
          </Button>
        )}
        <DeviceMenu d={d} acts={acts} />
      </div>
      {(d.vendor || (d.gpu && !d.mali_bind) || d.privileged) && (
        <div className="flex flex-wrap gap-1 px-2 pb-2">
          {d.vendor && <Badge variant="secondary">của hãng</Badge>}
          {d.gpu && !d.mali_bind && (
            <Badge variant="destructive">thiếu bind mali0</Badge>
          )}
          {d.privileged && <Badge variant="destructive">privileged</Badge>}
        </div>
      )}
    </div>
  )
}

export function DeviceGrid({
  box,
  devices,
  acts,
  paused,
}: {
  box: string
  devices: Device[]
  acts: DeviceActions
  paused: boolean
}) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
      {devices.map((d, i) => (
        <DeviceTile
          key={d.name}
          box={box}
          d={d}
          acts={acts}
          paused={paused}
          index={i}
        />
      ))}
    </div>
  )
}
