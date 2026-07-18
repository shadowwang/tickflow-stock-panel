import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Sparkles, Newspaper, FileText, ChevronDown, ChevronUp } from 'lucide-react'
import { api } from '@/lib/api'
import { buildTechSuggestion, type Tone } from '@/lib/indicators'
import { useHistoryReports, loadHistory, openHistoryReport, stripThinking } from '@/lib/stockAnalysisStore'

interface Props {
  symbol: string
  name?: string
}

const SOURCE_LABELS: Record<string, string> = {
  eastmoney_news: '东财资讯',
  eastmoney: '东财公告',
}

function fmtTime(s?: string): string {
  if (!s) return ''
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) return s.slice(0, 16)
  const d = new Date(s)
  if (!isNaN(d.getTime())) {
    const p = (x: number) => String(x).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  }
  return s
}

function stripMd(s: string): string {
  return (s || '')
    .replace(/<think(?:ing)?>[\s\S]*?(?:<\/think(?:ing)?>|$)/gi, '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*_`~\-]/g, '')
    .replace(/\n+/g, ' ')
    .trim()
    .slice(0, 160)
}

function directionClass(dir: string): string {
  if (dir === '偏多') return 'text-danger bg-danger/10 border-danger/30'
  if (dir === '偏空') return 'text-bear bg-bear/10 border-bear/30'
  return 'text-muted bg-elevated border-border'
}

function toneClass(tone: Tone): string {
  if (tone === 'bullish') return 'text-danger bg-danger/10 border-danger/20'
  if (tone === 'bearish') return 'text-bear bg-bear/10 border-bear/20'
  return 'text-muted bg-elevated border-border'
}

function CardState({ text, loading }: { text: string; loading?: boolean }) {
  return (
    <div className={`text-[12px] text-muted py-4 text-center ${loading ? 'animate-pulse' : ''}`}>
      {loading ? '加载中…' : text}
    </div>
  )
}

function TechCard({ symbol }: { symbol: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['insight-kline', symbol],
    queryFn: () => api.klineDaily(symbol, 120),
    enabled: !!symbol,
  })
  const sug = data ? buildTechSuggestion(data.rows) : null
  return (
    <div className="rounded-card border border-border bg-surface p-3.5 flex flex-col">
      <div className="flex items-center gap-1.5 mb-2">
        <Activity className="h-3.5 w-3.5 text-accent" />
        <span className="text-[12px] font-medium text-foreground">技术指标建议</span>
      </div>
      {isLoading && <CardState loading />}
      {isError && <CardState text="指标计算失败" />}
      {!isLoading && !isError && !sug && <CardState text="数据不足, 无法计算" />}
      {sug && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={`px-2 py-0.5 rounded-full text-[12px] font-medium border ${directionClass(sug.direction)}`}>
              {sug.direction}
            </span>
            <span className="text-[11px] text-muted">技术评分 {sug.score > 0 ? '+' : ''}{sug.score}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {sug.signals.map((s, i) => (
              <span key={i} className={`px-1.5 py-0.5 rounded text-[10px] border ${toneClass(s.tone)}`}>
                {s.text}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function AiCard({ symbol }: { symbol: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['insight-suggest', symbol],
    queryFn: () => api.stockSuggest(symbol),
    enabled: !!symbol,
  })
  return (
    <div className="rounded-card border border-border bg-surface p-3.5 flex flex-col">
      <div className="flex items-center gap-1.5 mb-2">
        <Sparkles className="h-3.5 w-3.5 text-accent" />
        <span className="text-[12px] font-medium text-foreground">AI 建议</span>
      </div>
      {isLoading && <CardState loading />}
      {isError && <CardState text="AI 建议获取失败" />}
      {data && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={`px-2 py-0.5 rounded-full text-[12px] font-medium border ${directionClass(data.direction)}`}>
              {data.direction}
            </span>
            <span className="text-[11px] text-muted">置信度 {data.confidence}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
            <div className="h-full rounded-full bg-accent" style={{ width: `${data.confidence}%` }} />
          </div>
          <p className="text-[12px] text-foreground/90 leading-relaxed">{stripThinking(data.reason)}</p>
          <p className="text-[10px] text-muted/70">
            {data.as_of ? `数据截至 ${data.as_of}${data.close != null ? ` · 收盘 ${data.close}` : ''} · ` : ''}仅客观技术面倾向, 不构成买卖建议
          </p>
        </div>
      )}
    </div>
  )
}

function NewsCard({ symbol, name }: { symbol: string; name?: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['insight-news', symbol, name],
    queryFn: () => api.insightsNews(symbol, name),
    enabled: !!symbol,
  })
  const items = data?.items ?? []
  return (
    <div className="rounded-card border border-border bg-surface p-3.5 flex flex-col">
      <div className="flex items-center gap-1.5 mb-2">
        <Newspaper className="h-3.5 w-3.5 text-accent" />
        <span className="text-[12px] font-medium text-foreground">新闻</span>
      </div>
      {isLoading && <CardState loading />}
      {isError && <CardState text="新闻获取失败" />}
      {!isLoading && !isError && items.length === 0 && <CardState text="暂无相关新闻" />}
      {items.length > 0 && (
        <div className="space-y-1.5 flex-1">
          {items.slice(0, 5).map((it) => (
            <a
              key={it.external_id}
              href={it.url}
              target="_blank"
              rel="noreferrer"
              className="block rounded-lg border border-border/40 bg-elevated/30 px-2.5 py-1.5 hover:bg-elevated/60 transition-colors"
            >
              <div className="text-[12px] text-foreground line-clamp-2">{it.title}</div>
              <div className="mt-0.5 text-[10px] text-muted">
                {SOURCE_LABELS[it.source] ?? it.source} · {fmtTime(it.publish_time)}
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

function ReportCard({ symbol }: { symbol: string }) {
  const [showAll, setShowAll] = useState(false)
  const { reports, loaded } = useHistoryReports()
  useEffect(() => {
    if (!loaded) loadHistory()
  }, [loaded])
  const list = reports
    .filter((r) => r.symbol === symbol)
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
  const latest = list[0]
  const titleOf = (r: { summary?: string; name?: string }) => r.summary || r.name || '分析报告'
  return (
    <div className="rounded-card border border-border bg-surface p-3.5 flex flex-col">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <FileText className="h-3.5 w-3.5 text-accent" />
          <span className="text-[12px] font-medium text-foreground">AI 分析报告</span>
        </div>
        {list.length > 1 && (
          <button
            onClick={() => setShowAll((v) => !v)}
            className="inline-flex items-center gap-0.5 text-[11px] text-accent hover:text-accent/80"
          >
            {showAll ? '收起' : `更多(${list.length})`}
            {showAll ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
        )}
      </div>
      {!loaded && <CardState loading />}
      {loaded && list.length === 0 && <CardState text="暂无分析报告" />}
      {loaded && latest && !showAll && (
        <button
          onClick={() => openHistoryReport(latest.id)}
          className="text-left w-full rounded-lg border border-border/40 bg-elevated/30 px-2.5 py-2 hover:bg-elevated/60 transition-colors"
        >
          <div className="text-[11px] text-muted">{fmtTime(latest.created_at)}</div>
          <div className="mt-0.5 text-[13px] font-medium line-clamp-1">{titleOf(latest)}</div>
          <div className="mt-1 text-[12px] text-foreground/85 line-clamp-3">{stripMd(latest.content)}</div>
        </button>
      )}
      {showAll && (
        <div className="space-y-1.5 flex-1 max-h-60 overflow-auto">
          {list.map((r) => (
            <button
              key={r.id}
              onClick={() => openHistoryReport(r.id)}
              className="text-left w-full rounded-lg border border-border/40 bg-elevated/30 px-2.5 py-1.5 hover:bg-elevated/60 transition-colors"
            >
              <div className="text-[11px] text-muted">{fmtTime(r.created_at)}</div>
              <div className="mt-0.5 text-[12px] font-medium line-clamp-1">{titleOf(r)}</div>
              <div className="mt-0.5 text-[11px] text-foreground/75 line-clamp-2">{stripMd(r.content)}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function StockInsightCards({ symbol, name }: Props) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
      <TechCard symbol={symbol} />
      <AiCard symbol={symbol} />
      <NewsCard symbol={symbol} name={name} />
      <ReportCard symbol={symbol} />
    </div>
  )
}
