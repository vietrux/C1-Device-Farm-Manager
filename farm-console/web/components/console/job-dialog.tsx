"use client"

import * as React from "react"

import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Spinner } from "@/components/ui/spinner"
import type { Job } from "@/lib/api"

export function JobDialog({
  open,
  onOpenChange,
  job,
  lines,
  title,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  job: Job | null
  lines: string[]
  title?: string
}) {
  const endRef = React.useRef<HTMLDivElement>(null)
  React.useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" })
  }, [lines.length])

  const status = job?.status
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span className="flex-1">
              {title || (job ? `${job.title} — ${job.box}` : "…")}
            </span>
            {status === "running" && (
              <Badge variant="secondary">
                <Spinner /> đang chạy
              </Badge>
            )}
            {status === "ok" && <Badge>xong</Badge>}
            {status === "failed" && <Badge variant="destructive">lỗi</Badge>}
          </DialogTitle>
        </DialogHeader>
        <ScrollArea className="h-[min(60vh,480px)] bg-muted/50 ring-1 ring-foreground/10">
          <pre className="p-3 text-xs leading-relaxed whitespace-pre-wrap">
            {lines.join("\n")}
            <div ref={endRef} />
          </pre>
        </ScrollArea>
        <DialogFooter showCloseButton />
      </DialogContent>
    </Dialog>
  )
}
