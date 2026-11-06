"use client"

import * as React from "react"
import { GlobeIcon, UnplugIcon } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { api, enc, type Job, type ProxyStatus } from "@/lib/api"

export function ProxyDialog({
  box,
  device,
  open,
  onOpenChange,
  onJob,
}: {
  box: string
  device: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onJob: (job: Job) => void
}) {
  const [status, setStatus] = React.useState<ProxyStatus | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [kind, setKind] = React.useState("socks5")
  const [host, setHost] = React.useState("")
  const [port, setPort] = React.useState("")
  const [user, setUser] = React.useState("")
  const [password, setPassword] = React.useState("")

  React.useEffect(() => {
    if (!open) return
    setStatus(null)
    setErr(null)
    api<ProxyStatus>(`/api/boxes/${enc(box)}/devices/${enc(device)}/proxy`)
      .then(setStatus)
      .catch((e) => setErr((e as Error).message))
  }, [open, box, device])

  const start = async (path: string, body?: unknown) => {
    setBusy(true)
    try {
      const j = await api<Job>(
        `/api/boxes/${enc(box)}/devices/${enc(device)}/proxy/${path}`,
        { method: "POST", body }
      )
      onJob(j)
      onOpenChange(false)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const applied = status?.applied === true

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GlobeIcon className="size-4" /> Proxy egress · {device}
          </DialogTitle>
          <DialogDescription>
            Cho máy đi mạng qua một IP riêng (SOCKS5/HTTP). Mỗi máy một IP sạch
            giúp qua anti-bot của X/bank/TikTok — tách biệt với Play Integrity.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Trạng thái</Label>
            {err ? (
              <p className="text-sm text-destructive">{err}</p>
            ) : !status ? (
              <Spinner className="size-4" />
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={applied ? "default" : "outline"}>
                  {applied ? "CÓ PROXY" : "egress trực tiếp"}
                </Badge>
                {status.raw && (
                  <span className="truncate text-xs text-muted-foreground">
                    {status.raw.slice(0, 80)}
                  </span>
                )}
              </div>
            )}
          </div>

          <div className="grid gap-3">
            <div className="grid gap-2">
              <Label>Loại</Label>
              <Select value={kind} onValueChange={(v) => setKind(v ?? "socks5")}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="socks5">SOCKS5</SelectItem>
                  <SelectItem value="http">HTTP</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-[1fr_7rem] gap-2">
              <div className="grid gap-2">
                <Label htmlFor="px-host">IP / host</Label>
                <Input
                  id="px-host"
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                  placeholder="1.2.3.4"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="px-port">Port</Label>
                <Input
                  id="px-port"
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                  placeholder="1080"
                  inputMode="numeric"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="grid gap-2">
                <Label htmlFor="px-user">User (tuỳ chọn)</Label>
                <Input
                  id="px-user"
                  value={user}
                  onChange={(e) => setUser(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="px-pass">Mật khẩu (tuỳ chọn)</Label>
                <Input
                  id="px-pass"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            disabled={busy || status?.applied === false}
            onClick={() => start("clear")}
          >
            <UnplugIcon /> Xoá proxy
          </Button>
          <Button
            disabled={busy || !host || !port}
            onClick={() =>
              start("set", {
                host,
                port: Number(port),
                kind,
                user,
                password,
              })
            }
          >
            {busy ? <Spinner className="size-4" /> : <GlobeIcon />} Áp dụng
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
