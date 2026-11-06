"use client"

import * as React from "react"
import {
  ArrowLeftIcon,
  CircleIcon,
  LayoutListIcon,
  MonitorUpIcon,
  PowerIcon,
  ScrollTextIcon,
  SendIcon,
  Volume1Icon,
  Volume2Icon,
} from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { api, authHeaders, enc } from "@/lib/api"
import { isSupported, startH264Player } from "@/lib/h264"

// Màn hình từ xa. Mặc định: video H.264 (screenrecord phần cứng encode) giải mã
// bằng WebCodecs → mượt, ít băng thông, hết `adb timeout`. Trình duyệt không hỗ
// trợ WebCodecs → lùi về chụp ảnh PNG định kỳ. Bấm = chạm, kéo = vuốt.
type ScreenProps = {
  box: string
  scrcpy: boolean
  onLogs: (device: string) => void
}

export function ScreenDialog({
  device,
  onClose,
  ...rest
}: ScreenProps & { device: string | null; onClose: () => void }) {
  return (
    <Dialog open={!!device} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-[min(820px,96vw)]">
        <DialogHeader>
          <DialogTitle>{device}</DialogTitle>
          <DialogDescription>
            Bấm = chạm, kéo = vuốt. Video H.264 thời gian thực (lùi về ảnh PNG nếu
            trình duyệt không hỗ trợ WebCodecs).
          </DialogDescription>
        </DialogHeader>
        {device && <ScreenBody key={device} device={device} {...rest} />}
      </DialogContent>
    </Dialog>
  )
}

function ScreenBody({
  box,
  device,
  scrcpy,
  onLogs,
}: ScreenProps & { device: string }) {
  const [src, setSrc] = React.useState<string | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  const [ready, setReady] = React.useState(false)
  // video = H.264/WebCodecs; poll = chụp ảnh PNG (fallback)
  const [mode, setMode] = React.useState<"video" | "poll">(
    isSupported() ? "video" : "poll"
  )
  const [live, setLive] = React.useState(true)
  const [text, setText] = React.useState("")
  const imgRef = React.useRef<HTMLImageElement>(null)
  const canvasRef = React.useRef<HTMLCanvasElement>(null)
  const wrapRef = React.useRef<HTMLDivElement>(null)
  const size = React.useRef({ w: 720, h: 1280 })
  const drag = React.useRef<{ x: number; y: number; t: number } | null>(null)
  const busy = React.useRef(false)
  const kick = React.useRef<() => void>(() => {})
  const liveRef = React.useRef(live)

  // ---- Chế độ video: H.264 stream → WebCodecs → canvas
  React.useEffect(() => {
    if (mode !== "video") return
    const canvas = canvasRef.current
    if (!canvas) return
    const stop = startH264Player({
      url: `/api/boxes/${enc(box)}/devices/${enc(device)}/stream.h264?size=720x1280&bitrate=6000000`,
      headers: authHeaders(),
      canvas,
      onResize: (w, h) => (size.current = { w, h }),
      onFirstFrame: () => {
        setReady(true)
        setErr(null)
      },
      onError: (m) => setErr(m),
      onUnsupported: () => setMode("poll"),
    })
    return stop
  }, [box, device, mode])

  // ---- Chế độ poll: chụp PNG định kỳ (khi không có WebCodecs)
  React.useEffect(() => {
    if (mode !== "poll") return
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    let lastUrl: string | null = null
    const tick = async () => {
      if (stop || busy.current) return
      busy.current = true
      try {
        const r = await fetch(
          `/api/boxes/${enc(box)}/devices/${enc(device)}/screen?t=${Date.now()}`,
          { headers: authHeaders() }
        )
        if (!r.ok)
          throw new Error(
            ((await r.json()) as { detail?: string }).detail || r.statusText
          )
        const url = URL.createObjectURL(await r.blob())
        if (lastUrl) URL.revokeObjectURL(lastUrl)
        lastUrl = url
        if (!stop) {
          setSrc(url)
          setReady(true)
          setErr(null)
        }
      } catch (e) {
        if (!stop) setErr((e as Error).message)
      }
      busy.current = false
      clearTimeout(timer)
      if (!stop && liveRef.current) timer = setTimeout(tick, 350)
    }
    kick.current = () => {
      clearTimeout(timer)
      setTimeout(tick, 120)
    }
    tick()
    return () => {
      stop = true
      clearTimeout(timer)
      if (lastUrl) URL.revokeObjectURL(lastUrl)
    }
  }, [box, device, mode])

  React.useEffect(() => {
    liveRef.current = live
    if (live && mode === "poll") kick.current()
  }, [live, mode])

  const send = async (body: Record<string, unknown>) => {
    try {
      await api(`/api/boxes/${enc(box)}/devices/${enc(device)}/input`, {
        method: "POST",
        body,
      })
    } catch (e) {
      toast.error((e as Error).message)
    }
    kick.current()
  }

  const toDevice = (ev: React.PointerEvent) => {
    const surf = wrapRef.current
    if (!surf) return null
    const r = surf.getBoundingClientRect()
    const { w, h } = size.current
    const scale = Math.min(r.width / w, r.height / h)
    const ox = (r.width - w * scale) / 2
    const oy = (r.height - h * scale) / 2
    const x = Math.round((ev.clientX - r.left - ox) / scale)
    const y = Math.round((ev.clientY - r.top - oy) / scale)
    return x < 0 || y < 0 || x > w || y > h ? null : { x, y }
  }

  const onDown = (ev: React.PointerEvent) => {
    const p = toDevice(ev)
    if (p) drag.current = { ...p, t: Date.now() }
  }
  const onUp = (ev: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    if (!d) return
    const q = toDevice(ev) || d
    if (Math.hypot(q.x - d.x, q.y - d.y) < 12)
      send({ type: "tap", x: d.x, y: d.y })
    else
      send({
        type: "swipe",
        x: d.x,
        y: d.y,
        x2: q.x,
        y2: q.y,
        ms: Math.min(Math.max(Date.now() - d.t, 100), 1500),
      })
  }
  const key = (k: string) => send({ type: "key", key: k })

  const openScrcpy = async () => {
    try {
      await api(`/api/boxes/${enc(box)}/devices/${enc(device)}/scrcpy`, {
        method: "POST",
      })
      toast.success("đang mở scrcpy…")
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  return (
    <div className="grid grid-cols-[auto_1fr] gap-4">
      <div
        ref={wrapRef}
        className="relative aspect-[9/16] h-[min(72vh,820px)] cursor-crosshair overflow-hidden bg-black select-none"
        onPointerDown={onDown}
        onPointerUp={onUp}
      >
        {mode === "video" ? (
          <canvas
            ref={canvasRef}
            className="size-full object-contain"
          />
        ) : (
          src && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              ref={imgRef}
              src={src}
              alt=""
              draggable={false}
              className="size-full object-contain"
              onLoad={(e) => {
                size.current = {
                  w: e.currentTarget.naturalWidth,
                  h: e.currentTarget.naturalHeight,
                }
              }}
            />
          )
        )}
        {(!ready || err) && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center p-4 text-center text-xs text-white/70">
            {err ? (
              <span className="max-w-[80%]">{err}</span>
            ) : (
              <span className="flex items-center gap-2">
                <Spinner /> Đang kết nối màn hình…
              </span>
            )}
          </div>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-3">
        <div className="grid grid-cols-3 gap-2">
          <Button variant="outline" onClick={() => key("KEYCODE_BACK")}>
            <ArrowLeftIcon /> Back
          </Button>
          <Button variant="outline" onClick={() => key("KEYCODE_HOME")}>
            <CircleIcon /> Home
          </Button>
          <Button variant="outline" onClick={() => key("KEYCODE_APP_SWITCH")}>
            <LayoutListIcon /> Đa nhiệm
          </Button>
          <Button variant="outline" onClick={() => key("KEYCODE_VOLUME_DOWN")}>
            <Volume1Icon /> Vol −
          </Button>
          <Button variant="outline" onClick={() => key("KEYCODE_VOLUME_UP")}>
            <Volume2Icon /> Vol +
          </Button>
          <Button variant="outline" onClick={() => key("KEYCODE_POWER")}>
            <PowerIcon /> Nguồn
          </Button>
        </div>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (text) send({ type: "text", text })
            setText("")
          }}
        >
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="gõ chữ vào máy…"
          />
          <Button type="submit" variant="secondary" size="icon">
            <SendIcon />
          </Button>
        </form>
        {mode === "poll" && (
          <Field orientation="horizontal">
            <Switch id="live" checked={live} onCheckedChange={setLive} />
            <FieldLabel htmlFor="live" className="font-normal">
              Cập nhật liên tục
            </FieldLabel>
          </Field>
        )}
        <Separator />
        {scrcpy && (
          <Button variant="outline" onClick={openScrcpy}>
            <MonitorUpIcon /> Mở scrcpy trên máy này
          </Button>
        )}
        <Button variant="outline" onClick={() => onLogs(device)}>
          <ScrollTextIcon /> Log container
        </Button>
      </div>
    </div>
  )
}
