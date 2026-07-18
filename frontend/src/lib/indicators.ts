// 前端技术指标计算与评分(纯函数, 不依赖后端返回哪些列)。
// 输入 K 线原始 OHLCV 序列, 计算 MACD / RSI / KDJ / BOLL / 均线,
// 综合打分产出技术面倾向(偏多/偏空/中性) + 信号标签。
import type { KlineRow } from './api'

export type Tone = 'bullish' | 'bearish' | 'neutral'

export interface TechSignal {
  text: string
  tone: Tone
}

export interface TechSuggestion {
  direction: '偏多' | '偏空' | '中性'
  score: number // -100 ~ 100
  signals: TechSignal[]
}

function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1)
  const out: number[] = []
  let prev = values[0]
  for (let i = 0; i < values.length; i++) {
    prev = i === 0 ? values[0] : values[i] * k + prev * (1 - k)
    out.push(prev)
  }
  return out
}

function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    out.push(i >= period - 1 ? sum / period : null)
  }
  return out
}

function rsi(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  let avgGain = 0
  let avgLoss = 0
  for (let i = 0; i < values.length; i++) {
    if (i === 0) {
      out.push(null)
      continue
    }
    const ch = values[i] - values[i - 1]
    const gain = ch > 0 ? ch : 0
    const loss = ch < 0 ? -ch : 0
    if (i <= period) {
      avgGain += gain
      avgLoss += loss
      if (i === period) {
        avgGain /= period
        avgLoss /= period
        out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss))
      } else {
        out.push(null)
      }
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period
      avgLoss = (avgLoss * (period - 1) + loss) / period
      out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss))
    }
  }
  return out
}

function stddev(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      out.push(null)
      continue
    }
    const slice = values.slice(i - period + 1, i + 1)
    const mean = slice.reduce((a, b) => a + b, 0) / period
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period
    out.push(Math.sqrt(variance))
  }
  return out
}

function kdj(
  highs: number[],
  lows: number[],
  closes: number[],
): { k: (number | null)[]; d: (number | null)[]; j: (number | null)[] } {
  const N = 9
  const k: (number | null)[] = []
  const d: (number | null)[] = []
  const j: (number | null)[] = []
  let prevK = 50
  let prevD = 50
  for (let i = 0; i < closes.length; i++) {
    if (i < N - 1) {
      k.push(null)
      d.push(null)
      j.push(null)
      continue
    }
    const hh = Math.max(...highs.slice(i - N + 1, i + 1))
    const ll = Math.min(...lows.slice(i - N + 1, i + 1))
    const rsv = hh === ll ? 50 : ((closes[i] - ll) / (hh - ll)) * 100
    const curK = prevK + (2 / 3) * (rsv - prevK)
    const curD = prevD + (2 / 3) * (curK - prevD)
    k.push(curK)
    d.push(curD)
    j.push(3 * curK - 2 * curD)
    prevK = curK
    prevD = curD
  }
  return { k, d, j }
}

function lastValid(arr: (number | null)[]): number | null {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] != null && Number.isFinite(arr[i] as number)) return arr[i] as number
  }
  return null
}

export function buildTechSuggestion(rows: KlineRow[]): TechSuggestion | null {
  if (!rows || rows.length < 30) return null
  const closes = rows.map((r) => Number(r.close))
  const highs = rows.map((r) => Number(r.high))
  const lows = rows.map((r) => Number(r.low))
  const n = closes.length
  const last = n - 1

  const ema12 = ema(closes, 12)
  const ema26 = ema(closes, 26)
  const dif = closes.map((_, i) => ema12[i] - ema26[i])
  const dea = ema(dif, 9)
  const hist = dif.map((v, i) => 2 * (v - dea[i]))

  const ma5 = sma(closes, 5)
  const ma20 = sma(closes, 20)
  const ma60 = sma(closes, 60)

  const rsi6 = rsi(closes, 6)
  const rsi14 = rsi(closes, 14)
  const rsi24 = rsi(closes, 24)

  const mid = sma(closes, 20)
  const sd = stddev(closes, 20)
  const upper = mid.map((m, i) => (m != null && sd[i] != null ? m + 2 * sd[i] : null))
  const lower = mid.map((m, i) => (m != null && sd[i] != null ? m - 2 * sd[i] : null))

  const { k, d, j } = kdj(highs, lows, closes)

  const DIF = lastValid(dif)
  const DEA = lastValid(dea)
  const HIST = lastValid(hist)
  const MA5 = lastValid(ma5)
  const MA20 = lastValid(ma20)
  const MA60 = lastValid(ma60)
  const RSI6 = lastValid(rsi6)
  const RSI14 = lastValid(rsi14)
  const RSI24 = lastValid(rsi24)
  const UP = lastValid(upper)
  const LO = lastValid(lower)
  const MID = lastValid(mid)
  const K = lastValid(k)
  const D = lastValid(d)
  const J = lastValid(j)

  const signals: TechSignal[] = []
  let score = 0

  // 均线排列
  if (MA5 != null && MA20 != null && MA60 != null) {
    if (MA5 > MA20 && MA20 > MA60) {
      score += 18
      signals.push({ text: '均线多头排列', tone: 'bullish' })
    } else if (MA5 < MA20 && MA20 < MA60) {
      score -= 18
      signals.push({ text: '均线空头排列', tone: 'bearish' })
    } else {
      signals.push({ text: '均线交织', tone: 'neutral' })
    }
  }
  // 价格与 20 日线
  if (MA20 != null) {
    if (closes[last] > MA20) score += 6
    else score -= 6
  }
  // MACD 状态 + 金叉/死叉
  if (DIF != null && DEA != null && HIST != null) {
    if (DIF > 0 && HIST > 0) {
      score += 12
      signals.push({ text: 'MACD 多头', tone: 'bullish' })
    } else if (DIF < 0 && HIST < 0) {
      score -= 12
      signals.push({ text: 'MACD 空头', tone: 'bearish' })
    }
    const curHist = hist[last]
    const prevHist = hist[last - 1]
    if (prevHist != null && curHist != null) {
      if (prevHist <= 0 && curHist > 0) {
        score += 8
        signals.push({ text: 'MACD 金叉', tone: 'bullish' })
      } else if (prevHist >= 0 && curHist < 0) {
        score -= 8
        signals.push({ text: 'MACD 死叉', tone: 'bearish' })
      }
    }
  }
  // RSI
  if (RSI14 != null) {
    if (RSI14 >= 70) {
      score -= 4
      signals.push({ text: 'RSI 超买', tone: 'bearish' })
    } else if (RSI14 <= 30) {
      score += 4
      signals.push({ text: 'RSI 超卖', tone: 'bullish' })
    }
  }
  if (RSI6 != null && RSI14 != null && RSI24 != null) {
    if (RSI6 > RSI14 && RSI14 > RSI24) signals.push({ text: 'RSI 多头', tone: 'bullish' })
    else if (RSI6 < RSI14 && RSI14 < RSI24) signals.push({ text: 'RSI 空头', tone: 'bearish' })
  }
  // KDJ
  if (K != null && D != null && J != null) {
    if (J > 100) {
      score -= 3
      signals.push({ text: 'KDJ 超买', tone: 'bearish' })
    } else if (J < 0) {
      score += 3
      signals.push({ text: 'KDJ 超卖', tone: 'bullish' })
    }
    const prevK = k[last - 1]
    const prevD = d[last - 1]
    if (prevK != null && prevD != null) {
      if (prevK <= prevD && K > D) {
        score += 5
        signals.push({ text: 'KDJ 金叉', tone: 'bullish' })
      } else if (prevK >= prevD && K < D) {
        score -= 5
        signals.push({ text: 'KDJ 死叉', tone: 'bearish' })
      }
    }
  }
  // BOLL 位置
  if (UP != null && LO != null && MID != null) {
    const c = closes[last]
    if (c > UP) {
      score -= 2
      signals.push({ text: '触及布林上轨', tone: 'bearish' })
    } else if (c < LO) {
      score += 2
      signals.push({ text: '触及布林下轨', tone: 'bullish' })
    }
  }

  score = Math.max(-100, Math.min(100, Math.round(score)))
  const direction: TechSuggestion['direction'] = score > 12 ? '偏多' : score < -12 ? '偏空' : '中性'
  return { direction, score, signals: signals.slice(0, 8) }
}
