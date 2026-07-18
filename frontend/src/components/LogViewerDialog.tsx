import { useCallback, useEffect, useRef, useState } from 'react'
import { Search, X, RefreshCw, Trash2, ScrollText, Filter } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type LogEntry } from '@/lib/api'

interface Props {
  open: boolean
  onClose: () => void
}

const PAGE_SIZE = 200
// 级别 → 颜色: 与项目色板一致 (accent=蓝、warning=黄、bear=红、muted=灰)。
// 圆点 + 文字, 模拟 image 里 "● INFO" 的视觉, 便于扫读。
const LEVEL_STYLE: Record<string, { dot: string; text: string }> = {
  DEBUG:     { dot: 'bg-muted/60',     text: 'text-muted' },
  INFO:      { dot: 'bg-accent',        text: 'text-accent' },
  WARNING:   { dot: 'bg-warning',      text: 'text-warning' },
  ERROR:     { dot: 'bg-bear',         text: 'text-bear' },
  CRITICAL:  { dot: 'bg-bear',         text: 'text-bear font-semibold' },
}

function levelStyle(level: string) {
  return LEVEL_STYLE[level] ?? { dot: 'bg-muted/60', text: 'text-muted' }
}

export function LogViewerDialog({ open, onClose }: Props) {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 打开时拉第一页; 关闭时不重置 (组件可能被频繁开关, 保留已加载数据体验更好)。
  // 但"清空缓冲"后需要拉一次新数据, 用 bumpKey 触发。
  const [bumpKey, setBumpKey] = useState(0)
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    api.logsGet(0, PAGE_SIZE)
      .then((snap) => {
        if (cancelled) return
        setEntries(snap.entries)
        setTotal(snap.total)
        setHasMore(snap.has_more)
        // 滚到顶: 最新日志在顶, 符合"刚打开先看最新"直觉。
        scrollRef.current?.scrollTo({ top: 0 })
      })
      .catch((e) => {
        toast(`加载日志失败: ${e?.message ?? e}`, 'error')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, bumpKey])

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return
    setLoadingMore(true)
    try {
      const snap = await api.logsGet(entries.length, PAGE_SIZE)
      setEntries((prev) => [...prev, ...snap.entries])
      setTotal(snap.total)
      setHasMore(snap.has_more)
    } catch (e: any) {
      toast(`加载更多失败: ${e?.message ?? e}`, 'error')
    } finally {
      setLoadingMore(false)
    }
  }, [entries.length, hasMore, loadingMore])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const snap = await api.logsGet(0, PAGE_SIZE)
      setEntries(snap.entries)
      setTotal(snap.total)
      setHasMore(snap.has_more)
      scrollRef.current?.scrollTo({ top: 0 })
    } catch (e: any) {
      toast(`刷新失败: ${e?.message ?? e}`, 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  const clearBuffer = useCallback(async () => {
    if (!confirm('确定清空服务端日志缓冲?此操作仅影响查看器,不影响已写入文件的日志。')) return
    try {
      const r = await api.logsClear()
      toast(`已清空 ${r.removed} 条`, 'success')
      setBumpKey((k) => k + 1)
    } catch (e: any) {
      toast(`清空失败: ${e?.message ?? e}`, 'error')
    }
  }, [])

  // 过滤: 客户端按 logger / message 子串匹配 (大小写不敏感)。空 = 全部。
  const trimmed = filter.trim().toLowerCase()
  const filtered = trimmed
    ? entries.filter(
        (e) =>
          e.logger.toLowerCase().includes(trimmed) ||
          e.message.toLowerCase().includes(trimmed),
      )
    : entries

  if (!open) return null

  return (
    <Modal
      onClose={onClose}
      ariaLabel="服务端运行日志"
      panelClassName="w-[95vw] max-w-6xl h-[85vh] bg-surface border border-border rounded-card shadow-xl flex flex-col overflow-hidden"
    >
      {/* 标题栏 */}
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <ScrollText className="h-4 w-4 text-accent shrink-0" />
          <h2 className="text-sm font-semibold text-foreground">服务端运行日志</h2>
          <span className="text-[11px] text-muted">最近 2000 条 · 重启后清空</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={refresh}
            disabled={loading}
            title="刷新 (回到最新一页)"
            className="flex items-center justify-center rounded-btn p-1.5 text-foreground/70 hover:bg-elevated hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={clearBuffer}
            title="清空服务端缓冲"
            className="flex items-center justify-center rounded-btn p-1.5 text-foreground/70 hover:bg-elevated hover:text-foreground"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={onClose}
            title="关闭"
            className="flex items-center justify-center rounded-btn p-1.5 text-foreground/70 hover:bg-elevated hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* 过滤栏 */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2 shrink-0">
        <div className="relative flex-1 max-w-md">
          <Filter className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted" />
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤 logger / 消息 (范围: 业务优先)"
            className="h-8 w-full rounded-btn border border-border bg-base pl-7 pr-7 text-xs text-foreground placeholder:text-muted/70 focus:border-accent/50 focus:outline-none"
          />
          {filter && (
            <button
              onClick={() => setFilter('')}
              title="清空过滤"
              className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center rounded p-1 text-muted hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
        {filter && (
          <span className="text-[11px] text-muted shrink-0">
            匹配 {filtered.length} / {entries.length} 条
          </span>
        )}
        {filter && (
          <button
            onClick={() => setFilter('')}
            className="text-[11px] text-accent hover:underline shrink-0"
          >
            清空过滤
          </button>
        )}
      </div>

      {/* 表格区 (可滚动) */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-auto scrollbar-gutter-stable">
        {loading && entries.length === 0 ? (
          <div className="flex items-center justify-center py-16 text-xs text-muted">
            <RefreshCw className="h-4 w-4 animate-spin mr-2" />
            加载中…
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-xs text-muted">
            <Search className="h-6 w-6 mb-2 text-muted/60" />
            {entries.length === 0 ? '暂无日志 (服务端启动后产生的日志会出现在这里)' : '没有匹配当前过滤的日志'}
          </div>
        ) : (
          <table className="min-w-full text-left text-[11px]">
            <thead className="sticky top-0 z-10 bg-elevated/95 backdrop-blur-sm text-muted">
              <tr>
                <th className="px-3 py-2 font-medium w-[150px]">时间</th>
                <th className="px-3 py-2 font-medium w-[68px]">级别</th>
                <th className="px-3 py-2 font-medium w-[180px]">LOGGER</th>
                <th className="px-3 py-2 font-medium w-[140px]">链路</th>
                <th className="px-3 py-2 font-medium">消息</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {filtered.map((e, i) => {
                const ls = levelStyle(e.level)
                return (
                  <tr
                    key={`${e.ts}-${i}`}
                    className="border-t border-border/40 hover:bg-elevated/40 transition-colors"
                  >
                    <td className="px-3 py-1.5 text-muted whitespace-nowrap align-top">{e.ts}</td>
                    <td className="px-3 py-1.5 align-top">
                      <span className={`inline-flex items-center gap-1.5 ${ls.text}`}>
                        <span className={`inline-block h-1.5 w-1.5 rounded-full ${ls.dot}`} />
                        {e.level}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-secondary whitespace-nowrap align-top">{e.logger}</td>
                    <td className="px-3 py-1.5 text-muted whitespace-nowrap align-top">{e.path}</td>
                    <td className="px-3 py-1.5 text-foreground/90 whitespace-pre-wrap break-words align-top">
                      {e.message}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* 底栏: 已加载 / 总数 + 加载更多 */}
      <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2.5 text-[11px] shrink-0">
        <span className="text-muted">
          已加载 <span className="font-mono text-foreground">{entries.length}</span>
          {' / '}
          <span className="font-mono text-foreground">{total}</span>
        </span>
        {hasMore && (
          <button
            onClick={loadMore}
            disabled={loadingMore}
            className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1 text-[11px] font-medium text-foreground hover:bg-elevated/70 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loadingMore && <RefreshCw className="h-3 w-3 animate-spin" />}
            {loadingMore ? '加载中…' : '加载更多'}
          </button>
        )}
      </div>
    </Modal>
  )
}
