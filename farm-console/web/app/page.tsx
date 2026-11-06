"use client"

import * as React from "react"
import { PlusIcon, PowerOffIcon, ServerIcon } from "lucide-react"
import { toast } from "sonner"

import { AppSidebar } from "@/components/console/app-sidebar"
import { BoxView } from "@/components/console/box-view"
import { useConfirm } from "@/components/console/confirm"
import {
  AddBoxDialog,
  CopyIdDialog,
  CreateDeviceDialog,
} from "@/components/console/forms"
import { JobDialog } from "@/components/console/job-dialog"
import { ScreenDialog } from "@/components/console/screen-dialog"
import { useJob } from "@/components/console/use-job"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import {
  api,
  enc,
  parseProvision,
  type Box,
  type BoxInfo,
  type DeviceList,
  type Job,
  type Meta,
  type ProvItem,
} from "@/lib/api"

type Err = { error: string }
const REFRESH_MS = 15000

export default function Page() {
  const [meta, setMeta] = React.useState<Meta | null>(null)
  const [offline, setOffline] = React.useState(false)
  const [boxes, setBoxes] = React.useState<Box[] | null>(null)
  const [infos, setInfos] = React.useState<Record<string, BoxInfo | Err>>({})
  const [current, setCurrent] = React.useState<string | null>(null)
  const [devices, setDevices] = React.useState<DeviceList | Err | null>(null)
  const [prov, setProv] = React.useState<
    Record<string, { items: ProvItem[]; at: number; raw: string[] }>
  >({})

  const [addOpen, setAddOpen] = React.useState(false)
  const [copyOpen, setCopyOpen] = React.useState(false)
  const [createOpen, setCreateOpen] = React.useState(false)
  const [jobOpen, setJobOpen] = React.useState(false)
  const [logView, setLogView] = React.useState<{
    title: string
    lines: string[]
  } | null>(null)
  const [screen, setScreen] = React.useState<string | null>(null)
  const { confirm, node: confirmNode } = useConfirm()
  const currentRef = React.useRef(current)
  currentRef.current = current

  const refreshInfo = React.useCallback(async (name: string) => {
    try {
      const i = await api<BoxInfo>(`/api/boxes/${enc(name)}/info`)
      setInfos((s) => ({ ...s, [name]: i }))
    } catch (e) {
      setInfos((s) => ({ ...s, [name]: { error: (e as Error).message } }))
    }
  }, [])

  const refreshDevices = React.useCallback(async () => {
    const name = currentRef.current
    if (!name) return
    try {
      const d = await api<DeviceList>(`/api/boxes/${enc(name)}/devices`)
      if (currentRef.current === name) setDevices(d)
    } catch (e) {
      if (currentRef.current === name)
        setDevices({ error: (e as Error).message })
    }
  }, [])

  const refreshAll = React.useCallback(() => {
    if (currentRef.current) refreshInfo(currentRef.current)
    refreshDevices()
  }, [refreshInfo, refreshDevices])

  const job = useJob((j: Job) => {
    if (j.kind === "provision")
      setProv((s) => ({
        ...s,
        [j.box]: {
          items: parseProvision(j.lines),
          at: Date.now(),
          raw: j.lines,
        },
      }))
    if (j.status === "ok") toast.success(`${j.title}: xong`)
    else if (j.kind !== "provision" || j.title !== "Kiểm tra box")
      toast.error(`${j.title}: lỗi`)
    refreshInfo(j.box)
    if (j.box === currentRef.current) refreshDevices()
  })

  const startJob = (j: Job, opts?: { silent?: boolean }) => {
    job.watch(j.id)
    setLogView(null)
    if (!opts?.silent) setJobOpen(true)
  }

  const loadBoxes = React.useCallback(async () => {
    const bs = await api<Box[]>("/api/boxes")
    setBoxes(bs)
    bs.forEach((b) => refreshInfo(b.name))
    return bs
  }, [refreshInfo])

  // báo console "tab vẫn mở" (kể cả khi tab ẩn) để nó không tự tắt
  React.useEffect(() => {
    const t = setInterval(() => {
      api("/api/ping")
        .then(() => setOffline(false))
        .catch((e) => {
          // fetch ném TypeError khi không kết nối được: console đã tắt
          if (e instanceof TypeError) setOffline(true)
        })
    }, 30000)
    return () => clearInterval(t)
  }, [])

  React.useEffect(() => {
    api<Meta>("/api/meta")
      .then(setMeta)
      .catch((e) => toast.error((e as Error).message))
    loadBoxes()
      .then((bs) => {
        const saved = localStorage.getItem("fc_box")
        const pick =
          bs.find((b) => b.name === saved)?.name ?? bs[0]?.name ?? null
        setCurrent(pick)
      })
      .catch((e) => toast.error((e as Error).message))
  }, [loadBoxes])

  React.useEffect(() => {
    if (!current) return
    localStorage.setItem("fc_box", current)
    setDevices(null)
    refreshAll()
    const t = setInterval(() => {
      if (!document.hidden) refreshAll()
    }, REFRESH_MS)
    return () => clearInterval(t)
  }, [current, refreshAll])

  const box = boxes?.find((b) => b.name === current) ?? null

  const removeBox = async () => {
    if (!box) return
    const yes = await confirm({
      title: `Bỏ ${box.name} khỏi console?`,
      body: "Chỉ xoá khỏi danh sách của console, không đụng gì tới box.",
      ok: "Bỏ",
    })
    if (!yes) return
    await api(`/api/boxes/${enc(box.name)}`, { method: "DELETE" })
    const bs = await loadBoxes()
    setCurrent(bs[0]?.name ?? null)
  }

  const showLogs = async (dev: string) => {
    try {
      const r = await api<{ logs: string }>(
        `/api/boxes/${enc(current!)}/devices/${enc(dev)}/logs?n=300`
      )
      setLogView({ title: `Log ${dev}`, lines: r.logs.split("\n") })
      setJobOpen(true)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  return (
    <SidebarProvider>
      <AppSidebar
        boxes={boxes}
        infos={infos}
        current={current}
        onSelect={setCurrent}
        onAdd={() => setAddOpen(true)}
        version={meta?.version}
      />
      <SidebarInset>
        {box ? (
          <BoxView
            key={box.name}
            box={box}
            info={infos[box.name]}
            devices={devices}
            prov={prov[box.name]}
            refresh={refreshAll}
            onJob={startJob}
            onCopyId={() => setCopyOpen(true)}
            onCreate={() => setCreateOpen(true)}
            onScreen={setScreen}
            paused={!!screen}
            onRemoveBox={removeBox}
            confirm={confirm}
          />
        ) : (
          boxes && (
            <Empty className="m-auto">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <ServerIcon />
                </EmptyMedia>
                <EmptyTitle>Chưa có box nào</EmptyTitle>
                <EmptyDescription>
                  Thêm box đầu tiên: nhập địa chỉ SSH, cài key, rồi lược bỏ hệ
                  của hãng để tự vận hành.
                </EmptyDescription>
              </EmptyHeader>
              <EmptyContent>
                <Button onClick={() => setAddOpen(true)}>
                  <PlusIcon /> Thêm box
                </Button>
              </EmptyContent>
            </Empty>
          )
        )}
      </SidebarInset>

      <AddBoxDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onAdded={async (b, sshError) => {
          await loadBoxes()
          setCurrent(b.name)
          if (sshError) {
            toast.warning("Chưa SSH vào box bằng key được, cài SSH key trước.")
            setCopyOpen(true)
          }
        }}
      />
      {current && (
        <>
          <CopyIdDialog
            open={copyOpen}
            onOpenChange={setCopyOpen}
            box={current}
            onJob={startJob}
          />
          <CreateDeviceDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            box={current}
            onJob={startJob}
            confirmUnproven={() =>
              confirm({
                title: "Image chưa kiểm chứng",
                body: "Image này chưa từng chạy thử trên box RK3588. Image lạ có thể làm treo box (phải rút điện). Vẫn tạo?",
                ok: "Vẫn tạo",
              })
            }
          />
          <ScreenDialog
            box={current}
            device={screen}
            onClose={() => setScreen(null)}
            scrcpy={!!meta?.scrcpy}
            onLogs={showLogs}
          />
        </>
      )}
      <JobDialog
        open={jobOpen}
        onOpenChange={setJobOpen}
        job={logView ? null : job.job}
        lines={logView ? logView.lines : job.lines}
        title={logView?.title}
      />
      {confirmNode}
      {offline && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-background/90 backdrop-blur-sm">
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <PowerOffIcon />
              </EmptyMedia>
              <EmptyTitle>farm-console đã tắt</EmptyTitle>
              <EmptyDescription>
                Mở lại bằng biểu tượng farm-console hoặc lệnh{" "}
                <code>tools/farm-console.sh</code>. Mỗi lần mở là một địa chỉ
                mới, có thể đóng tab này.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        </div>
      )}
    </SidebarProvider>
  )
}
