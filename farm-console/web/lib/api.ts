// Gọi API của farm-console (cùng origin; FastAPI phục vụ cả trang tĩnh này).

export type Box = {
  name: string
  ssh: string
  note: string
  main_image?: string | null
  has_password: boolean
}

export type BoxInfo = {
  hostname: string
  model: string
  compatible: string
  kernel: string
  uptime_s: number
  load: [number, number, number]
  cpus: number
  mem_mb: { MemTotal: number; MemAvailable: number }
  disk: { root: Disk | null; data: Disk | null }
  temp_c: number | null
  gpu: {
    governor: string
    freq_mhz: number
    load: string
    firmware: "ok" | "failed" | "unknown"
  }
  selinux_enforce: string
  tee_supplicant: boolean
  vendor_mytsvr: "running" | "stopped" | "absent"
  docker: boolean
  binders: number
}
type Disk = { total_gb: number; free_gb: number }

export type Device = {
  name: string
  id: string
  image: string
  state: string
  started_at: string
  restart: string
  ip: string | null
  binders: number[]
  gpu: boolean
  mali_bind: boolean
  privileged: boolean
  managed: boolean
  vendor: boolean
  data: string | null
  boot_completed: boolean | null
}
export type DeviceList = { devices: Device[]; unsafe: string[] }
export type FleetBox = {
  name: string
  error?: string
  devices: Device[]
  unsafe: string[]
}
export type Fleet = { boxes: FleetBox[]; at: number }
export type Image = {
  ref: string
  size: string
  created: string
  proven: boolean
}

export type Profile = {
  key: string
  model: string
  device: string
  fingerprint: string
}

export type Job = {
  id: number
  kind: string
  box: string
  title: string
  status: "running" | "ok" | "failed"
  result: unknown
  lines: string[]
  nlines: number
}

export type Meta = {
  version: string
  adbkey: boolean
  adb: boolean
  sshpass: boolean
  scrcpy: boolean
  load_guard: number
}

export type IntegrityStatus = {
  chain: {
    md5: string
    name: string | null
    known: boolean
    revoked: boolean | null
    expires: string | null
    note: string | null
  }
  device: "ok" | "revoked" | "expired" | "factory" | "unknown"
  basic: {
    fingerprint: string
    flash_locked: string
    vbmeta_state: string
    ok: boolean
  }
  apps: { keyattestation: boolean; spic: boolean }
  fixed: boolean
  legacy: {
    magisk: string
    daemon: boolean
    zygisk: string
    pif_module: boolean
    pif_fingerprint: string
    keymaster_xml: boolean
    keymaster_xml_device: string
  }
}

export type Keybox = {
  name: string
  device_id: string
  usable: boolean
  revoked: boolean
  not_after: string | null
  algorithms: string[]
  score: number
  error: string | null
  keys: { algorithm: string; root: string | null; ok: boolean; errors: string[] }[]
}

export type KeyboxList = {
  dir: string | null
  revocation: string
  keyboxes: Keybox[]
}

export type ProxyStatus = {
  applied: boolean | null
  ip: string
  port: string
  type: string
  raw: string
}

function token(): string {
  if (typeof window === "undefined") return ""
  const t = new URLSearchParams(window.location.search).get("token")
  if (t) localStorage.setItem("fc_token", t)
  return t || localStorage.getItem("fc_token") || ""
}

export function authHeaders(): Record<string, string> {
  const t = token()
  return t ? { "X-Token": t } : {}
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number
  ) {
    super(message)
  }
}

export async function api<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown } = {}
): Promise<T> {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  })
  let data: unknown = null
  try {
    data = await r.json()
  } catch {}
  if (!r.ok) {
    const d = (data as { detail?: unknown } | null)?.detail
    const msg =
      typeof d === "string"
        ? d
        : Array.isArray(d)
          ? (d[0] as { msg?: string })?.msg
          : r.statusText
    throw new ApiError(msg || `HTTP ${r.status}`, r.status)
  }
  return data as T
}

export const enc = encodeURIComponent

export function fmtUptime(s: number): string {
  if (s > 86400)
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

// dòng kết quả của df-host.sh:  "  [ OK ]  docker   DOCKER_OPTS=..."
export type ProvItem = {
  status: "ok" | "fix" | "skip" | "info"
  item: string
  msg: string
}
export function parseProvision(lines: string[]): ProvItem[] {
  const out: ProvItem[] = []
  for (const l of lines) {
    const m =
      l.match(/^\s*\[\s*(OK|SỬA|--)\s*\]\s+(\S+)\s+(.*)$/) ||
      l.match(/^\s*(\.\.\.)\s+(\S+)\s+(.*)$/)
    if (!m) continue
    const st =
      m[1] === "OK"
        ? "ok"
        : m[1] === "SỬA"
          ? "fix"
          : m[1] === "--"
            ? "skip"
            : "info"
    out.push({ status: st, item: m[2], msg: m[3] })
  }
  return out
}
