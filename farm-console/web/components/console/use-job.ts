"use client"

import * as React from "react"

import { api, type Job } from "@/lib/api"

// Theo dõi 1 job (provision, tạo máy, cài key): hỏi log mới mỗi giây tới khi xong.
export function useJob(onDone?: (job: Job) => void) {
  const [job, setJob] = React.useState<Job | null>(null)
  const [lines, setLines] = React.useState<string[]>([])
  const doneRef = React.useRef(onDone)
  React.useEffect(() => {
    doneRef.current = onDone
  }, [onDone])

  const watch = React.useCallback((id: number) => {
    let since = 0
    let stopped = false
    setLines([])
    setJob(null)
    const tick = async () => {
      if (stopped) return
      try {
        const j = await api<Job>(`/api/jobs/${id}?since=${since}`)
        since = j.nlines
        setJob(j)
        if (j.lines.length) setLines((l) => [...l, ...j.lines])
        if (j.status !== "running") {
          stopped = true
          const full = await api<Job>(`/api/jobs/${id}`)
          doneRef.current?.(full)
          return
        }
      } catch {
        // mạng chập chờn: thử lại ở nhịp sau
      }
      setTimeout(tick, 1000)
    }
    tick()
    return () => {
      stopped = true
    }
  }, [])

  return { job, lines, watch }
}
