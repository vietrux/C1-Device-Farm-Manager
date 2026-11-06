// Phát H.264 Annex-B (từ screenrecord trên máy) bằng WebCodecs → vẽ lên canvas.
//
// Vì sao không MSE/<video>: screenrecord xuất elementary stream Annex-B thô (không
// phải fMP4), lại khởi động lại mỗi 180s. WebCodecs nhận trực tiếp Annex-B, độ trễ
// thấp, và tự bắt lại ở mỗi SPS/PPS/IDR mới khi stream restart.
//
// Trình duyệt thiếu WebCodecs (Safari cũ, Firefox cũ) → gọi onUnsupported để UI lùi
// về chế độ poll ảnh PNG.

export type H264PlayerOpts = {
  url: string
  headers: Record<string, string>
  canvas: HTMLCanvasElement
  onResize?: (w: number, h: number) => void
  onError?: (msg: string) => void
  onUnsupported?: () => void
  onFirstFrame?: () => void
}

// vị trí của byte 0x01 trong start-code (00 00 01 hoặc 00 00 00 01), từ `from`
function nextStart(buf: Uint8Array, from: number): number {
  for (let i = from; i + 2 < buf.length; i++) {
    if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) return i + 2
  }
  return -1
}

function codecString(sps: Uint8Array): string {
  // sps[0] = header NAL (0x67); profile/constraint/level ở 3 byte kế tiếp
  const h = (n: number) => n.toString(16).padStart(2, "0")
  return `avc1.${h(sps[1])}${h(sps[2])}${h(sps[3])}`
}

export function isSupported(): boolean {
  return typeof window !== "undefined" && "VideoDecoder" in window
}

export function startH264Player(opts: H264PlayerOpts): () => void {
  const { url, headers, canvas, onResize, onError, onUnsupported, onFirstFrame } = opts
  if (!isSupported()) {
    onUnsupported?.()
    return () => {}
  }

  const ctx = canvas.getContext("2d")
  const abort = new AbortController()
  let decoder: VideoDecoder | null = null
  let configured = false
  let sps: Uint8Array | null = null
  let pps: Uint8Array | null = null
  let ts = 0
  let gotFrame = false
  let stopped = false

  const paint = (frame: VideoFrame) => {
    const w = frame.displayWidth
    const h = frame.displayHeight
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w
      canvas.height = h
      onResize?.(w, h)
    }
    ctx?.drawImage(frame, 0, 0, w, h)
    frame.close()
    if (!gotFrame) {
      gotFrame = true
      onFirstFrame?.()
    }
  }

  const ensureDecoder = () => {
    if (decoder && decoder.state !== "closed") return
    decoder = new VideoDecoder({
      output: paint,
      error: (e) => onError?.(String((e as Error)?.message || e)),
    })
  }

  // concat các NAL (kèm start-code 4 byte) thành 1 access unit cho EncodedVideoChunk
  const annexb = (nals: Uint8Array[]): Uint8Array => {
    const total = nals.reduce((n, x) => n + 4 + x.length, 0)
    const out = new Uint8Array(total)
    let o = 0
    for (const n of nals) {
      out[o + 3] = 1
      out.set(n, o + 4)
      o += 4 + n.length
    }
    return out
  }

  const onNal = (nal: Uint8Array) => {
    if (nal.length === 0) return
    const type = nal[0] & 0x1f
    if (type === 7) {
      sps = nal
      return
    }
    if (type === 8) {
      pps = nal
      return
    }
    if (type !== 1 && type !== 5) return // chỉ quan tâm slice (VCL)

    ensureDecoder()
    if (!decoder) return
    const key = type === 5
    if (!configured) {
      if (!sps || !pps || !key) return // chờ keyframe + param set đầu tiên
      try {
        decoder.configure({
          codec: codecString(sps),
          optimizeForLatency: true,
        } as VideoDecoderConfig)
        configured = true
      } catch (e) {
        onError?.(String((e as Error)?.message || e))
        onUnsupported?.()
        return
      }
    }
    // keyframe: kèm SPS+PPS để decoder bám lại khi stream restart
    const parts = key && sps && pps ? [sps, pps, nal] : [nal]
    try {
      decoder.decode(
        new EncodedVideoChunk({
          type: key ? "key" : "delta",
          timestamp: ts,
          data: annexb(parts),
        })
      )
      ts += 33333 // ~30fps, chỉ để timestamp tăng dần
    } catch (e) {
      // decoder có thể cần reset sau lỗi/restart → cấu hình lại ở keyframe kế
      configured = false
      try {
        decoder.close()
      } catch {}
      decoder = null
    }
  }

  ;(async () => {
    let buf = new Uint8Array(0)
    try {
      const r = await fetch(url, { headers, signal: abort.signal })
      if (!r.ok || !r.body) throw new Error(r.ok ? "no body" : `HTTP ${r.status}`)
      const reader = r.body.getReader()
      while (!stopped) {
        const { value, done } = await reader.read()
        if (done) break
        if (!value || value.length === 0) continue
        const merged = new Uint8Array(buf.length + value.length)
        merged.set(buf)
        merged.set(value, buf.length)
        buf = merged

        // payload NAL nằm giữa hai start-code liên tiếp; xử lý mọi NAL đã hoàn
        // chỉnh, giữ lại từ start-code cuối cùng (có thể còn đang nhận dở).
        let s = nextStart(buf, 0)
        if (s < 0) continue
        let payload = s + 1 // byte đầu payload (sau 0x01)
        while (true) {
          const nxt = nextStart(buf, payload)
          if (nxt < 0) break
          // nxt trỏ tới 0x01 của start-code kế; loại 00 00 (và 00 nếu 4-byte)
          let end = nxt - 2
          if (end > payload && buf[end - 1] === 0) end -= 1
          onNal(buf.subarray(payload, end))
          payload = nxt + 1
        }
        buf = buf.subarray(payload - 3) // giữ lại start-code + phần dở
      }
    } catch (e) {
      if (!stopped && (e as Error)?.name !== "AbortError")
        onError?.(String((e as Error)?.message || e))
    }
  })()

  return () => {
    stopped = true
    abort.abort()
    try {
      decoder?.close()
    } catch {}
  }
}
