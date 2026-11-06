"use client"

import * as React from "react"
import {
  CircleAlertIcon,
  EllipsisVerticalIcon,
  KeyRoundIcon,
  LayoutGridIcon,
  ListIcon,
  MonitorSmartphoneIcon,
  PlayIcon,
  PlusIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  SmartphoneIcon,
  Trash2Icon,
  Undo2Icon,
  WandSparklesIcon,
} from "lucide-react"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { IntegrityDialog } from "@/components/console/integrity-dialog"
import { ProxyDialog } from "@/components/console/proxy-dialog"
import {
  Item,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "@/components/ui/item"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  api,
  enc,
  fmtUptime,
  type Box,
  type BoxInfo,
  type Device,
  type DeviceList,
  type Job,
  type ProvItem,
} from "@/lib/api"
import { cn } from "@/lib/utils"

import { DeviceGrid, DeviceMenu, deviceState, Dot } from "./device-grid"

type Props = {
  box: Box
  info: BoxInfo | { error: string } | undefined
  devices: DeviceList | { error: string } | null
  prov: { items: ProvItem[]; at: number; raw: string[] } | undefined
  refresh: () => void
  onJob: (job: Job, opts?: { silent?: boolean }) => void
  onCopyId: () => void
  onCreate: () => void
  onScreen: (device: string) => void
  // đang mở màn hình lớn: tạm dừng ảnh thu nhỏ để box không phải chụp song song
  paused?: boolean
  onRemoveBox: () => void
  confirm: (o: {
    title: string
    body: React.ReactNode
    ok?: string
    destructive?: boolean
  }) => Promise<boolean>
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 p-3 ring-1 ring-foreground/10">
      <span className="text-[10px] tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      <span className="truncate text-base font-medium tabular-nums">
        {value}{" "}
        {sub && (
          <span className="text-xs font-normal text-muted-foreground">
            {sub}
          </span>
        )}
      </span>
    </div>
  )
}

export function BoxView(p: Props) {
  const { box, info, devices } = p
  const ok = info && !("error" in info) ? info : null

  const provision = async (mode: "check" | "apply" | "takeover" | "revert") => {
    if (mode === "takeover") {
      const yes = await p.confirm({
        title: "Lược bỏ hệ của hãng?",
        ok: "Lược bỏ",
        body: (
          <div className="grid gap-2">
            <p>
              Tắt vĩnh viễn agent của hãng (<b>mytsvr</b>), xoá các container
              Android và SDK của hãng (image và data của hãng vẫn giữ trên đĩa),
              đóng Docker API đang mở ra mạng, và cài các service của mình:
              tee-supplicant, tune GPU, tự reboot khi treo, mạng df-br.
            </p>
            <p>
              App/SDK quản lý của hãng sẽ <b>ngừng hoạt động</b> với box này.
              Cấu hình host hoàn tác được, nhưng container của hãng đã xoá thì
              không tự tạo lại.
            </p>
          </div>
        ),
      })
      if (!yes) return
    }
    if (mode === "revert") {
      const yes = await p.confirm({
        title: "Hoàn tác cấu hình host?",
        ok: "Hoàn tác",
        body: "Trả cấu hình host về như trước khi chuẩn hoá (bật lại mytsvr nếu trước đó bật).",
      })
      if (!yes) return
    }
    try {
      const j = await api<Job>(
        `/api/boxes/${enc(box.name)}/provision/${mode}`,
        { method: "POST" }
      )
      p.onJob(j, { silent: mode === "check" })
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const action = async (dev: string, a: string) => {
    try {
      await api(`/api/boxes/${enc(box.name)}/devices/${enc(dev)}/${a}`, {
        method: "POST",
      })
      toast.success(`${dev}: ${a}`)
    } catch (e) {
      toast.error((e as Error).message)
    }
    p.refresh()
  }

  const remove = async (d: Device) => {
    let purge = false
    const yes = await p.confirm({
      title: `Xoá máy ${d.name}?`,
      ok: "Xoá",
      body: (
        <div className="grid gap-3">
          <p>Container sẽ bị xoá.</p>
          <Field orientation="horizontal">
            <Checkbox id="purge" onCheckedChange={(v) => (purge = v)} />
            <FieldLabel htmlFor="purge" className="font-normal">
              Xoá luôn dữ liệu ({d.data || "?"})
            </FieldLabel>
          </Field>
        </div>
      ),
    })
    if (!yes) return
    try {
      await api(
        `/api/boxes/${enc(box.name)}/devices/${enc(d.name)}?purge=${purge}`,
        { method: "DELETE" }
      )
      toast.success(`đã xoá ${d.name}`)
    } catch (e) {
      toast.error((e as Error).message)
    }
    p.refresh()
  }

  const devList = devices && !("error" in devices) ? devices : null
  const [integrityDev, setIntegrityDev] = React.useState<string | null>(null)
  const [proxyDev, setProxyDev] = React.useState<string | null>(null)
  const acts = {
    onScreen: p.onScreen,
    action,
    remove,
    integrity: (name: string) => setIntegrityDev(name),
    proxy: (name: string) => setProxyDev(name),
  }
  const [view, setView] = React.useState<"grid" | "list">(() =>
    typeof window !== "undefined" && localStorage.getItem("fc_view") === "list"
      ? "list"
      : "grid"
  )
  const changeView = (v: "grid" | "list") => {
    setView(v)
    localStorage.setItem("fc_view", v)
  }
  const running =
    devList?.devices.filter((d) => d.state === "running").length ?? 0

  return (
    <div className="flex min-w-0 flex-col">
      <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mx-1 h-4" />
        <div className="flex min-w-0 flex-1 items-baseline gap-3">
          <h1 className="truncate text-sm font-medium">{box.name}</h1>
          <span className="truncate text-xs text-muted-foreground">
            {box.ssh}
            {box.note && ` · ${box.note}`}
          </span>
        </div>
        <Button variant="ghost" size="sm" onClick={p.refresh}>
          <RefreshCwIcon /> Làm mới
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="icon-sm" />}
          >
            <EllipsisVerticalIcon />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={p.onCopyId}>
              <KeyRoundIcon /> Cài SSH key
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={p.onRemoveBox}>
              <Trash2Icon /> Bỏ khỏi console
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <div className="grid gap-4 p-4">
        {info && "error" in info && (
          <Alert variant="destructive">
            <CircleAlertIcon />
            <AlertTitle>Không kết nối được box</AlertTitle>
            <AlertDescription>{info.error}</AlertDescription>
          </Alert>
        )}
        {ok?.vendor_mytsvr === "running" && (
          <Alert>
            <WandSparklesIcon />
            <AlertTitle>Box còn chạy hệ của hãng</AlertTitle>
            <AlertDescription>
              Bấm “Lược bỏ hệ vendor” ở phần Chuẩn hoá host để tự vận hành.
            </AlertDescription>
          </Alert>
        )}
        {devList && devList.unsafe.length > 0 && (
          <Alert variant="destructive">
            <CircleAlertIcon />
            <AlertTitle>Container dùng GPU thiếu bind /dev/mali0</AlertTitle>
            <AlertDescription>
              {devList.unsafe.join(", ")} — dừng/start lẫn lộn với chúng có thể
              treo box. Xoá chúng trước khi tạo hoặc start máy khác.
            </AlertDescription>
          </Alert>
        )}

        {/* host */}
        <Card>
          <CardHeader>
            <CardTitle>Box</CardTitle>
            <CardDescription>
              {ok
                ? `${ok.hostname} · ${ok.compatible.split(" ")[0]}`
                : "đang hỏi…"}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {!info ? (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
                {Array.from({ length: 12 }).map((_, i) => (
                  <Skeleton key={i} className="h-16" />
                ))}
              </div>
            ) : ok ? (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
                <Stat label="Kernel" value={ok.kernel} />
                <Stat label="Uptime" value={fmtUptime(ok.uptime_s)} />
                <Stat
                  label={`Load (${ok.cpus} nhân)`}
                  value={ok.load[0].toFixed(2)}
                  sub={`${ok.load[1].toFixed(2)} ${ok.load[2].toFixed(2)}`}
                />
                <Stat
                  label="RAM trống"
                  value={(ok.mem_mb.MemAvailable / 1024).toFixed(1)}
                  sub={`/ ${(ok.mem_mb.MemTotal / 1024).toFixed(1)} GB`}
                />
                <Stat
                  label="Đĩa /mmc trống"
                  value={ok.disk.data?.free_gb ?? "?"}
                  sub={`/ ${ok.disk.data?.total_gb ?? "?"} GB`}
                />
                <Stat
                  label="Nhiệt độ"
                  value={ok.temp_c ? `${ok.temp_c.toFixed(1)}°C` : "?"}
                />
                <Stat
                  label="GPU"
                  value={`${ok.gpu.freq_mhz} MHz`}
                  sub={ok.gpu.governor}
                />
                <Stat
                  label="Firmware GPU"
                  value={
                    ok.gpu.firmware === "ok" ? (
                      <Badge>đã nạp</Badge>
                    ) : ok.gpu.firmware === "failed" ? (
                      <Badge variant="destructive">LỖI</Badge>
                    ) : (
                      <Badge variant="outline">?</Badge>
                    )
                  }
                />
                <Stat
                  label="tee-supplicant"
                  value={
                    ok.tee_supplicant ? (
                      <Badge>chạy</Badge>
                    ) : (
                      <Badge variant="destructive">dừng</Badge>
                    )
                  }
                />
                <Stat
                  label="Hệ của hãng"
                  value={
                    ok.vendor_mytsvr === "running" ? (
                      <Badge variant="destructive">đang chạy</Badge>
                    ) : (
                      <Badge variant="secondary">
                        {ok.vendor_mytsvr === "absent" ? "không có" : "đã tắt"}
                      </Badge>
                    )
                  }
                />
                <Stat
                  label="Binder"
                  value={ok.binders}
                  sub={`→ tối đa ${Math.floor(ok.binders / 3)} máy`}
                />
                <Stat
                  label="Máy đang chạy"
                  value={devList ? running : "…"}
                  sub={devList ? `/ ${devList.devices.length}` : ""}
                />
              </div>
            ) : null}
          </CardContent>
        </Card>

        {/* provision */}
        <Card>
          <CardHeader>
            <CardTitle>Chuẩn hoá host</CardTitle>
            <CardDescription>
              Box mới mua: <b>Lược bỏ hệ vendor</b> một lần là xong. Chạy lại
              bao nhiêu lần cũng an toàn.
            </CardDescription>
            <CardAction className="flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => provision("check")}
              >
                <ShieldCheckIcon /> Kiểm tra
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => provision("apply")}
              >
                <WandSparklesIcon /> Chuẩn hoá
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => provision("takeover")}
              >
                <Trash2Icon /> Lược bỏ hệ vendor
              </Button>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => provision("revert")}
                    />
                  }
                >
                  <Undo2Icon />
                </TooltipTrigger>
                <TooltipContent>Hoàn tác cấu hình host</TooltipContent>
              </Tooltip>
            </CardAction>
          </CardHeader>
          <CardContent>
            {p.prov ? (
              <ItemGroup className="grid gap-x-4 sm:grid-cols-2 lg:grid-cols-3">
                {p.prov.items.map((x) => (
                  <Item key={x.item + x.msg} size="sm" className="px-0">
                    <ItemMedia>
                      <Dot
                        tone={
                          x.status === "ok"
                            ? "good"
                            : x.status === "fix"
                              ? "bad"
                              : "idle"
                        }
                      />
                    </ItemMedia>
                    <ItemContent className="min-w-0">
                      <ItemTitle>{x.item}</ItemTitle>
                      <ItemDescription className="truncate" title={x.msg}>
                        {x.msg}
                      </ItemDescription>
                    </ItemContent>
                  </Item>
                ))}
              </ItemGroup>
            ) : (
              <p className="text-xs text-muted-foreground">
                Bấm “Kiểm tra” để xem box đã chuẩn chưa.
              </p>
            )}
          </CardContent>
        </Card>

        {/* devices */}
        <Card>
          <CardHeader>
            <CardTitle>Máy Android</CardTitle>
            <CardDescription>
              Mỗi máy một bộ 3 binder riêng, IP trên mạng df-br (172.30.0.0/24).
            </CardDescription>
            <CardAction className="flex items-center gap-2">
              <ToggleGroup
                variant="outline"
                size="sm"
                value={[view]}
                onValueChange={(v: string[]) =>
                  v[0] && changeView(v[0] as "grid" | "list")
                }
              >
                <ToggleGroupItem
                  value="grid"
                  aria-label="Xem màn hình"
                  className="aria-pressed:bg-primary aria-pressed:text-primary-foreground"
                >
                  <LayoutGridIcon /> Màn hình
                </ToggleGroupItem>
                <ToggleGroupItem
                  value="list"
                  aria-label="Xem danh sách"
                  className="aria-pressed:bg-primary aria-pressed:text-primary-foreground"
                >
                  <ListIcon /> Danh sách
                </ToggleGroupItem>
              </ToggleGroup>
              <Button size="sm" onClick={p.onCreate}>
                <PlusIcon /> Tạo máy
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {!devices ? (
              <div className="grid gap-2">
                <Skeleton className="h-8" />
                <Skeleton className="h-8" />
              </div>
            ) : "error" in devices ? (
              <Alert variant="destructive">
                <CircleAlertIcon />
                <AlertDescription>{devices.error}</AlertDescription>
              </Alert>
            ) : devices.devices.length === 0 ? (
              <Empty>
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <SmartphoneIcon />
                  </EmptyMedia>
                  <EmptyTitle>Chưa có máy nào</EmptyTitle>
                  <EmptyDescription>
                    Tạo máy Android đầu tiên trên box này.
                  </EmptyDescription>
                </EmptyHeader>
                <EmptyContent>
                  <Button size="sm" onClick={p.onCreate}>
                    <PlusIcon /> Tạo máy
                  </Button>
                </EmptyContent>
              </Empty>
            ) : view === "grid" ? (
              <DeviceGrid
                box={box.name}
                devices={devices.devices}
                acts={acts}
                paused={!!p.paused}
              />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-6" />
                    <TableHead>Tên</TableHead>
                    <TableHead>Trạng thái</TableHead>
                    <TableHead>IP</TableHead>
                    <TableHead>Binder</TableHead>
                    <TableHead>Image</TableHead>
                    <TableHead>Tự chạy</TableHead>
                    <TableHead />
                    <TableHead className="text-right" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {devices.devices.map((d) => {
                    const { run, booted, auto, tone } = deviceState(d)
                    return (
                      <TableRow
                        key={d.name}
                        className={cn(!run && "text-muted-foreground")}
                      >
                        <TableCell>
                          <Dot tone={tone} />
                        </TableCell>
                        <TableCell className="font-medium">{d.name}</TableCell>
                        <TableCell>
                          {run ? (
                            booted ? (
                              "đang chạy"
                            ) : (
                              <span className="flex items-center gap-1.5">
                                <Spinner /> đang boot
                              </span>
                            )
                          ) : (
                            d.state
                          )}
                        </TableCell>
                        <TableCell className="tabular-nums">{d.ip}</TableCell>
                        <TableCell className="tabular-nums">
                          {d.binders.join("/")}
                        </TableCell>
                        <TableCell
                          className="max-w-48 truncate"
                          title={d.image}
                        >
                          {d.image.split(":").pop()}
                        </TableCell>
                        <TableCell>
                          <Switch
                            checked={auto}
                            onCheckedChange={(v) =>
                              action(
                                d.name,
                                v ? "autostart-on" : "autostart-off"
                              )
                            }
                          />
                        </TableCell>
                        <TableCell className="flex gap-1">
                          {d.vendor && (
                            <Badge variant="secondary">của hãng</Badge>
                          )}
                          {d.gpu && !d.mali_bind && (
                            <Badge variant="destructive">
                              thiếu bind mali0
                            </Badge>
                          )}
                          {d.privileged && (
                            <Badge variant="destructive">privileged</Badge>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1">
                            {run && booted && (
                              <Button
                                size="xs"
                                onClick={() => p.onScreen(d.name)}
                              >
                                <MonitorSmartphoneIcon /> Màn hình
                              </Button>
                            )}
                            {!run && (
                              <Button
                                size="xs"
                                variant="outline"
                                onClick={() => action(d.name, "start")}
                              >
                                <PlayIcon /> Chạy
                              </Button>
                            )}
                            <DeviceMenu d={d} acts={acts} />
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
        <p className="text-center text-[10px] text-muted-foreground">
          Lần cập nhật cuối: {new Date().toLocaleTimeString()} · tự làm mới mỗi
          15 giây
        </p>
      </div>
      {integrityDev && (
        <IntegrityDialog
          box={box.name}
          device={integrityDev}
          open={!!integrityDev}
          onOpenChange={(o) => !o && setIntegrityDev(null)}
          onJob={(j) => p.onJob(j)}
        />
      )}
      {proxyDev && (
        <ProxyDialog
          box={box.name}
          device={proxyDev}
          open={!!proxyDev}
          onOpenChange={(o) => !o && setProxyDev(null)}
          onJob={(j) => p.onJob(j)}
        />
      )}
    </div>
  )
}
