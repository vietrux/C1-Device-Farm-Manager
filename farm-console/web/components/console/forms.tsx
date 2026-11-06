"use client"

import * as React from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { api, enc, type Box, type Image, type Job, type Profile } from "@/lib/api"

type DialogProps = { open: boolean; onOpenChange: (o: boolean) => void }

function useSubmit() {
  const [busy, setBusy] = React.useState(false)
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return { busy, run }
}

// ------------------------------------------------------------------ thêm box
export function AddBoxDialog({
  open,
  onOpenChange,
  onAdded,
}: DialogProps & { onAdded: (box: Box, sshError: string | null) => void }) {
  const { busy, run } = useSubmit()
  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<
      string,
      string
    >
    run(async () => {
      const r = await api<Box & { ssh_error: string | null }>("/api/boxes", {
        method: "POST",
        body: f,
      })
      onOpenChange(false)
      onAdded(r, r.ssh_error)
    })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Thêm box</DialogTitle>
            <DialogDescription>
              Box mới mua: thêm vào đây, cài SSH key, rồi bấm “Lược bỏ hệ
              vendor”.
            </DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="name">Tên gọi</FieldLabel>
              <Input
                id="name"
                name="name"
                required
                placeholder="box-01"
                pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,40}"
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="ssh">SSH</FieldLabel>
              <Input
                id="ssh"
                name="ssh"
                required
                placeholder="user@100.98.135.123"
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="sudo_password">
                Mật khẩu sudo trên box
              </FieldLabel>
              <Input
                id="sudo_password"
                name="sudo_password"
                type="password"
                placeholder="box vendor mặc định: myt"
              />
              <FieldDescription>
                Lưu ở máy chạy console (file quyền 0600), không gửi đi nơi khác.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="note">Ghi chú</FieldLabel>
              <Input id="note" name="note" placeholder="phòng A, kệ 2" />
            </Field>
          </FieldGroup>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" type="button" />}>
              Huỷ
            </DialogClose>
            <Button type="submit" disabled={busy}>
              {busy && <Spinner />} Thêm
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------ cài ssh key
export function CopyIdDialog({
  open,
  onOpenChange,
  box,
  onJob,
}: DialogProps & { box: string; onJob: (job: Job) => void }) {
  const { busy, run } = useSubmit()
  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const f = Object.fromEntries(new FormData(e.currentTarget))
    run(async () => {
      const j = await api<Job>(`/api/boxes/${enc(box)}/copy-id`, {
        method: "POST",
        body: f,
      })
      onOpenChange(false)
      onJob(j)
    })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Cài SSH key lên {box}</DialogTitle>
            <DialogDescription>
              Console điều khiển box bằng SSH key. Nhập mật khẩu đăng nhập SSH
              của box một lần để cài key.
            </DialogDescription>
          </DialogHeader>
          <Field>
            <FieldLabel htmlFor="ssh_password">Mật khẩu SSH</FieldLabel>
            <Input
              id="ssh_password"
              name="ssh_password"
              type="password"
              required
              autoFocus
            />
          </Field>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" type="button" />}>
              Huỷ
            </DialogClose>
            <Button type="submit" disabled={busy}>
              {busy && <Spinner />} Cài key
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------ tạo máy
type CreateProps = {
  box: string
  onJob: (job: Job) => void
  confirmUnproven: () => Promise<boolean>
}

export function CreateDeviceDialog({
  open,
  onOpenChange,
  ...rest
}: DialogProps & CreateProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {open && (
          <CreateDeviceForm {...rest} onDone={() => onOpenChange(false)} />
        )}
      </DialogContent>
    </Dialog>
  )
}

function CreateDeviceForm({
  box,
  onJob,
  confirmUnproven,
  onDone,
}: CreateProps & { onDone: () => void }) {
  const { busy, run } = useSubmit()
  const [images, setImages] = React.useState<Image[] | null>(null)
  const [image, setImage] = React.useState<string | null>(null)
  const [mainImage, setMainImage] = React.useState<string | null>(null)
  const [profiles, setProfiles] = React.useState<Profile[] | null>(null)
  const [profile, setProfile] = React.useState<string>("auto")
  const [autostart, setAutostart] = React.useState(true)

  React.useEffect(() => {
    let alive = true
    // image chính của box (main_image trong boxes.yaml) — mặc định cho máy mới
    api<Box[]>(`/api/boxes`)
      .then((r) => {
        if (alive) setMainImage(r.find((b) => b.name === box)?.main_image ?? null)
      })
      .catch(() => {})
    api<{ images: Image[] }>(`/api/boxes/${enc(box)}/images`)
      .then((r) => {
        if (!alive) return
        setImages(r.images)
      })
      .catch((e) => {
        if (!alive) return
        setImages([])
        toast.error((e as Error).message)
      })
    api<{ profiles: Profile[] }>(`/api/boxes/${enc(box)}/profiles`)
      .then((r) => alive && setProfiles(r.profiles))
      .catch(() => alive && setProfiles([]))
    return () => {
      alive = false
    }
  }, [box])

  // Ưu tiên image chính; chưa biết thì tạm chọn image đầu (agent sắp proven lên trước)
  React.useEffect(() => {
    if (images) setImage(mainImage && images.some((i) => i.ref === mainImage) ? mainImage : images[0]?.ref ?? null)
  }, [images, mainImage])

  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<
      string,
      string
    >
    run(async () => {
      const img = images?.find((i) => i.ref === image)
      if (img && !img.proven && !(await confirmUnproven())) return
      const j = await api<Job>(`/api/boxes/${enc(box)}/devices`, {
        method: "POST",
        body: {
          name: f.name,
          image,
          width: +f.width,
          height: +f.height,
          dpi: +f.dpi,
          fps: +f.fps,
          autostart,
          profile,
          count: Math.min(Math.max(+f.count || 1, 1), 12),
        },
      })
      onDone()
      onJob(j)
    })
  }

  const items = (images || []).map((i) => ({
    value: i.ref,
    label: `${i.proven ? "✔" : "⚠"} ${i.ref.split("/").pop()} (${i.size})`,
  }))

  return (
    <form onSubmit={submit} className="grid gap-4">
      <DialogHeader>
        <DialogTitle>Tạo máy Android trên {box}</DialogTitle>
        <DialogDescription>
          Binder và IP được cấp tự động. Máy luôn dùng công thức đã kiểm chứng
          (bind /dev/mali0).
        </DialogDescription>
      </DialogHeader>
      <FieldGroup>
        <div className="grid grid-cols-[1fr_7rem] gap-3">
          <Field>
            <FieldLabel htmlFor="dname">Tên máy</FieldLabel>
            <Input
              id="dname"
              name="name"
              required
              placeholder="phone"
              pattern="[a-z0-9][a-z0-9_\-]{0,30}"
              autoFocus
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="count">Số lượng</FieldLabel>
            <Input
              id="count"
              name="count"
              type="number"
              min={1}
              max={12}
              defaultValue={1}
            />
          </Field>
        </div>
        <FieldDescription className="-mt-2">
          Tạo nhiều máy: tên sẽ thành <code>tên-1</code>, <code>tên-2</code>… Boot lần lượt.
          <br />Box này chạy <b>~5 máy đồng thời</b> (giới hạn secure-world/TEE).
          Máy hỏng sẽ tự bị xoá để trả chỗ; muốn thay máy cũ thì <b>Xoá</b> (dừng không trả chỗ).
        </FieldDescription>
        <Field>
          <FieldLabel>Image</FieldLabel>
          <Select
            items={items}
            value={image}
            onValueChange={(v) => setImage(v as string | null)}
          >
            <SelectTrigger className="w-full">
              <SelectValue
                placeholder={images ? "không có image" : "đang tải…"}
              />
            </SelectTrigger>
            <SelectContent>
              {items.map((i) => (
                <SelectItem key={i.value} value={i.value}>
                  {i.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>
            ✔ đã kiểm chứng trên RK3588 · ⚠ chưa thử (có thể treo box)
          </FieldDescription>
        </Field>
        <Field>
          <FieldLabel>Hồ sơ thiết bị</FieldLabel>
          <Select value={profile} onValueChange={(v) => setProfile(v as string)}>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="auto" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">Tự chọn (trải đều, ổn định theo tên)</SelectItem>
              <SelectItem value="random">Ngẫu nhiên</SelectItem>
              {(profiles || []).map((p) => (
                <SelectItem key={p.key} value={p.key}>
                  {p.model} ({p.device})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>
            Mỗi máy đội lốt một Pixel Android 12 thật, nhất quán (fingerprint + model
            + serial riêng) — tránh trùng dấu vết. Tạo hàng loạt: mỗi máy một serial.
          </FieldDescription>
        </Field>
        <div className="grid grid-cols-4 gap-3">
          {(
            [
              ["width", "Rộng", 720],
              ["height", "Cao", 1280],
              ["dpi", "DPI", 320],
              ["fps", "FPS", 24],
            ] as const
          ).map(([k, l, v]) => (
            <Field key={k}>
              <FieldLabel htmlFor={k}>{l}</FieldLabel>
              <Input id={k} name={k} type="number" defaultValue={v} />
            </Field>
          ))}
        </div>
        <Field orientation="horizontal">
          <Checkbox
            id="autostart"
            checked={autostart}
            onCheckedChange={setAutostart}
          />
          <FieldLabel htmlFor="autostart" className="font-normal">
            Tự chạy khi box khởi động lại (bật sau khi boot xong)
          </FieldLabel>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" type="button" />}>
          Huỷ
        </DialogClose>
        <Button type="submit" disabled={busy || !image}>
          {busy && <Spinner />} Tạo máy
        </Button>
      </DialogFooter>
    </form>
  )
}
