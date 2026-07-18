import { useQuery } from '@tanstack/react-query'
import { LineChart, Sparkles, TrendingUp, TrendingDown } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { AnalysisKChart, type PriceLevel, type LevelType } from '@/components/stock-analysis/AnalysisKChart'

/**
 * AI 个股分析报告「图文并茂」渲染视图。
 *
 * 在纯 Markdown 正文之上,叠加一个快照区:
 *   1) 摘要卡(代码/名称/报告时收盘价/涨跌色/关注点/摘要)
 *   2) 内嵌日 K 图(含关键价位线)—— K 线走实时行情,价位线优先用报告快照 levels
 * 让原本的纯文本报告变成「图 + 文」的可视化页面。
 *
 * 注意:K 线为当前行情快照,AI 正文是报告生成时的结论,二者时间可能不一致,
 * 故图下有一行极小说明,避免误读。
 */

interface ReportMeta {
  summary?: string
  levels?: Record<LevelType, PriceLevel[]>
  close?: number | null
}

interface Props {
  symbol: string
  name: string
  meta?: ReportMeta | null
  content: string
  /** 是否处于流式生成中(正文末尾显示光标) */
  streaming?: boolean
}

export function StockAnalysisReportView({ symbol, name, meta, content, streaming }: Props) {
  // K 线走实时行情(报告不存 rows);价位线优先用报告快照 levels,缺失时拉实时。
  const kline = useQuery({
    queryKey: ['kline', symbol, 'report-view'],
    queryFn: () => api.klineDaily(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })
  const liveLevels = useQuery({
    queryKey: QK.stockLevels(symbol),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  const rows = kline.data?.rows ?? []
  // 价位线:报告快照优先;带 band 序列的 series 用实时(仅影响通道曲线,不影响价位线)
  const levels = (meta?.levels ?? liveLevels.data?.levels ?? {}) as Record<LevelType, PriceLevel[]>
  const close = meta?.close ?? liveLevels.data?.close

  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const isUp = last ? (prev ? last.close >= prev.close : last.close >= last.open) : true

  return (
    <div className="space-y-4">
      {/* ===== 摘要卡 ===== */}
      <div className="rounded-xl border border-border/50 bg-gradient-to-br from-sky-500/[0.07] via-blue-500/[0.03] to-transparent px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500/20 to-blue-500/15 border border-sky-400/30 shrink-0">
              <LineChart className="h-4 w-4 text-sky-300" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-sm font-semibold text-foreground truncate">{name || symbol}</span>
                <span className="text-[10px] font-mono text-muted shrink-0">{symbol}</span>
              </div>
              {meta?.summary ? (
                <div className="mt-0.5 flex items-center gap-1 text-[11px] text-secondary truncate">
                  <Sparkles className="h-2.5 w-2.5 text-sky-400 shrink-0" />
                  <span className="truncate">{meta.summary}</span>
                </div>
              ) : null}
            </div>
          </div>
          {close != null && (
            <div className="flex items-center gap-1.5 shrink-0">
              {isUp ? (
                <TrendingUp className="h-4 w-4 text-bull" />
              ) : (
                <TrendingDown className="h-4 w-4 text-bear" />
              )}
              <span className={`text-lg font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
                {close.toFixed(2)}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ===== 内嵌日 K 图(含关键价位) ===== */}
      {rows.length > 0 ? (
        <div className="rounded-xl border border-border/50 bg-surface/40 overflow-hidden">
          <div className="px-3 py-2 border-b border-border/40 flex items-center gap-2">
            <span className="text-xs font-medium text-foreground">关键价位 · 日 K</span>
            <span className="text-[10px] text-muted/70">图表为当前行情,正文为报告结论</span>
          </div>
          <div className="p-2">
            <AnalysisKChart
              rows={rows}
              levels={levels}
              series={liveLevels.data?.series}
              seriesDates={liveLevels.data?.dates}
              defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
              height={360}
            />
          </div>
        </div>
      ) : null}

      {/* ===== AI 正文 ===== */}
      <div className="relative">
        <MarkdownRenderer content={content} />
        {streaming && (
          <span className="inline-block w-1.5 h-3.5 bg-sky-400 ml-0.5 align-middle animate-pulse rounded-sm" />
        )}
      </div>
    </div>
  )
}
