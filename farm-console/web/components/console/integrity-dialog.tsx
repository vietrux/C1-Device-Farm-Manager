"use client"

import * as React from "react"
import { KeyRoundIcon, ShieldCheckIcon, TrashIcon } from "lucide-react"
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
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import {
  api,
  enc,
  type IntegrityStatus,
  type Job,
  type Keybox,
  type KeyboxList,
} from "@/lib/api"

/** Verdict chuỗi keybox nhúng (nguồn DEVICE thật) → màu/nhãn hiển thị. */
function chainVerdict(st: IntegrityStatus): {
  cls: string
  label: string
} {
  switch (st.device) {
    case "ok":
      return { cls: "bg-emerald-600", label: "CHAIN SẠCH" }
    case "revoked":
      return { cls: "bg-destructive", label: "CHAIN BỊ REVOKE" }
    case "expired":
      return { cls: "bg-amber-600", label: "CHAIN HẾT HẠN" }
    case "factory":
      return { cls: "bg-destructive", label: "CHAIN FACTORY (revoked)" }
    default:
      return { cls: "bg-muted text-foreground", label: "CHAIN KHÔNG RÕ" }
  }
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <span className="w-24 shrink-0 text-xs uppercase text-muted-foreground">
        {k}
      </span>
      <span className="text-sm break-all">{children}</span>
    </div>
  )
}

export function IntegrityDialog({
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
  const [status, setStatus] = React.useState<IntegrityStatus | null>(null)
  const [kb, setKb] = React.useState<KeyboxList | null>(null)
  const [pick, setPick] = React.useState<string>("") // "" = best, "none", or name
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!open) return
    setStatus(null)
    setErr(null)
    api<KeyboxList>("/api/keyboxes")
      .then(setKb)
      .catch((e) => toast.error((e as Error).message))
    api<IntegrityStatus>(
      `/api/boxes/${enc(box)}/devices/${enc(device)}/integrity`
    )
      .then(setStatus)
      .catch((e) => setErr((e as Error).message))
  }, [open, box, device])

  const start = async (path: string, body?: unknown) => {
    setBusy(true)
    try {
      const j = await api<Job>(
        `/api/boxes/${enc(box)}/devices/${enc(device)}/integrity/${path}`,
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

  const usable = (kb?.keyboxes ?? []).filter((k) => k.usable)
  const legacy = status?.legacy
  const hasLegacy = !!(
    legacy &&
    (legacy.keymaster_xml || legacy.pif_module || legacy.daemon)
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheckIcon className="size-4" /> Integrity · {device}
          </DialogTitle>
          <DialogDescription>
            Đo từ sự thật: DEVICE = chuỗi keybox nhúng trong vendor lib
            (md5 đối chiếu sổ chain), BASIC = props máy (MYTPIF + hồ sơ).
            Xoay keybox = patch lib trong layer image rồi tạo lại máy.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          {/* trạng thái hiện tại */}
          <div className="grid gap-2">
            <Label>Trạng thái thực tế</Label>
            {err ? (
              <p className="text-sm text-destructive">{err}</p>
            ) : !status ? (
              <Spinner className="size-4" />
            ) : (
              <div className="grid gap-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    className={
                      status.fixed
                        ? "bg-emerald-600"
                        : "bg-muted text-foreground"
                    }
                  >
                    {status.fixed ? "SẴN SÀNG" : "CHƯA ĐẠT"}
                  </Badge>
                  <Badge className={chainVerdict(status).cls}>
                    {chainVerdict(status).label}
                  </Badge>
                  <Badge
                    className={
                      status.basic.ok
                        ? "bg-emerald-600"
                        : "bg-amber-600"
                    }
                  >
                    BASIC props {status.basic.ok ? "ok" : "lạ"}
                  </Badge>
                  <Badge variant="outline" className="font-mono">
                    md5 {status.chain.md5.slice(0, 8)}
                  </Badge>
                </div>

                <Row k="keybox">
                  {status.chain.name ? (
                    <>
                      <b>{status.chain.name}</b>
                      {status.chain.revoked != null && (
                        <span className="text-xs">
                          ·{" "}
                          {status.chain.revoked ? (
                            <span className="text-destructive">revoked</span>
                          ) : (
                            <span className="text-emerald-600">không revoke</span>
                          )}
                        </span>
                      )}
                      {status.chain.expires && (
                        <span className="text-xs text-muted-foreground">
                          {" "}
                          · hết hạn {status.chain.expires.slice(0, 10)}
                        </span>
                      )}
                      {status.chain.note && (
                        <span className="text-xs text-muted-foreground">
                          {" "}
                          · {status.chain.note}
                        </span>
                      )}
                    </>
                  ) : status.chain.known ? (
                    "?"
                  ) : (
                    <span className="text-amber-600">
                      chain lạ — chưa ghi sổ (xoay keybox hoặc ghi sổ tay)
                    </span>
                  )}
                </Row>
                {status.device === "revoked" && (
                  <p className="text-xs text-muted-foreground">
                    Revoked = nằm trong danh sách chính thức của Google (chữ ký
                    vẫn hợp lệ, nhiều app vẫn chấp nhận) — khác với hết hạn
                    (chain không verify được nữa). Thực tế dùng ngon hay không
                    thì đo bằng app trên máy.
                  </p>
                )}
                <Row k="fingerprint">
                  <span className="font-mono text-xs">
                    {status.basic.fingerprint || "—"}
                  </span>
                </Row>
                <Row k="bootloader">
                  flash.locked={status.basic.flash_locked || "?"} ·{" "}
                  vbmeta={status.basic.vbmeta_state || "?"}
                </Row>
                <Row k="app đo">
                  KeyAttestation {status.apps.keyattestation ? "✓" : "✗"} ·
                  IntegrityChecker {status.apps.spic ? "✓" : "✗"}
                </Row>
                {legacy && hasLegacy && (
                  <div className="mt-1 rounded-md border border-dashed p-2 text-xs text-muted-foreground">
                    Di sản (không ảnh hưởng PI trong container): keymaster.xml{" "}
                    {legacy.keymaster_xml
                      ? `(${legacy.keymaster_xml_device || "?"})`
                      : "—"}{" "}
                    · magisk {legacy.daemon ? "chạy" : "—"} · PIF module{" "}
                    {legacy.pif_module ? "cài" : "—"}
                    <div>
                      (HAL không đọc keymaster.xml; module PIF không tải được —
                      chỉ giữ để đối chiếu.)
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* chọn keybox */}
          <div className="grid gap-2">
            <Label>Keybox xoay tới (nguồn DEVICE)</Label>
            <Select value={pick} onValueChange={(v) => setPick(v ?? "")}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Tốt nhất tự động" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">
                  Tốt nhất tự động{usable[0] ? ` (${usable[0].name})` : ""}
                </SelectItem>
                <SelectItem value="none">Không đổi keybox</SelectItem>
                {(kb?.keyboxes ?? []).map((k) => (
                  <SelectItem key={k.name} value={k.name} disabled={!!k.error}>
                    {k.name} · {k.algorithms.join("/") || "?"} ·{" "}
                    {k.not_after?.slice(0, 10) || "?"}
                    {k.error
                      ? " · lỗi file"
                      : !k.usable
                        ? k.revoked
                          ? " · revoked (Google)"
                          : " · không hợp lệ"
                        : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Chọn tự do: revoked/hết hạn là đối chiếu danh sách chính thức của
              Google — chain chữ ký vẫn có thể chạy ngon (trickbox revoked vẫn
              chạy cả farm). Verdict trên máy sẽ báo đúng sự thật sau khi xoay.
            </p>
            <KeyboxTable kb={kb} />
          </div>
        </div>

        <DialogFooter>
          {hasLegacy && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => start("remove")}
            >
              <TrashIcon /> Dọn di sản
            </Button>
          )}
          <Button
            disabled={busy}
            onClick={() =>
              start("apply", { keybox: pick === "" ? null : pick })
            }
          >
            {busy ? (
              <Spinner className="size-4" />
            ) : (
              <KeyRoundIcon />
            )}{" "}
            Xoay keybox
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function KeyboxTable({ kb }: { kb: KeyboxList | null }) {
  if (!kb) return <Spinner className="size-4" />
  if (!kb.dir)
    return (
      <p className="text-xs text-muted-foreground">
        Chưa cấu hình thư mục keybox (--keyboxes).
      </p>
    )
  if (kb.keyboxes.length === 0)
    return (
      <p className="text-xs text-muted-foreground">
        Không có keybox trong {kb.dir}.
      </p>
    )
  return (
    <div className="max-h-48 overflow-auto rounded-md border text-xs">
      <table className="w-full">
        <thead className="sticky top-0 bg-muted/50 text-muted-foreground">
          <tr>
            <th className="px-2 py-1 text-left">nguồn</th>
            <th className="px-2 py-1 text-left">root</th>
            <th className="px-2 py-1 text-left">hạn</th>
            <th className="px-2 py-1 text-left">điểm</th>
            <th className="px-2 py-1 text-left">trạng thái</th>
          </tr>
        </thead>
        <tbody>
          {kb.keyboxes.map((k) => (
            <KeyboxRow key={k.name} k={k} />
          ))}
        </tbody>
      </table>
      <div className="px-2 py-1 text-[11px] text-muted-foreground">
        revoke: {kb.revocation} · {kb.dir}
      </div>
    </div>
  )
}

function KeyboxRow({ k }: { k: Keybox }) {
  const roots = Array.from(
    new Set(k.keys.filter((x) => x.ok && x.root).map((x) => x.root))
  ).join(",")
  return (
    <tr className="border-t">
      <td className="px-2 py-1 font-medium">{k.name}</td>
      <td className="px-2 py-1">{roots || "—"}</td>
      <td className="px-2 py-1">{k.not_after?.slice(0, 10) || "—"}</td>
      <td className="px-2 py-1">{k.score || "—"}</td>
      <td className="px-2 py-1">
        {k.usable ? (
          <span className="text-emerald-600">dùng được</span>
        ) : k.revoked ? (
          <span className="text-destructive">REVOKED</span>
        ) : (
          <span className="text-muted-foreground">
            {k.error ? "lỗi file" : "không hợp lệ"}
          </span>
        )}
      </td>
    </tr>
  )
}
