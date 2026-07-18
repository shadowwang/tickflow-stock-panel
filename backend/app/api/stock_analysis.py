"""个股分析 API — 关键价位 + AI 四维分析 + 报告持久化。

路由前缀: /api/stock-analysis

端点:
  GET  /levels?symbol=         11 类关键价位(图表 markLine 数据源)
  POST /analyze                AI 流式四维分析(NDJSON)
  GET  /reports                历史报告列表
  POST /reports                保存一条报告
  DELETE /reports/{report_id}  删除一条报告
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.indicators.levels import compute_levels, summarize_levels
from app.services import stock_reports
from app.services.stock_analyzer import analyze_stock_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-analysis", tags=["stock-analysis"])


def _to_float_list(series: pl.Series) -> list:
    """polars Series → JSON 安全的 float 列表(null/NaN → None)。"""
    out: list = []
    for v in series.to_list():
        if v is None:
            out.append(None)
            continue
        try:
            f = float(v)
            out.append(round(f, 2) if math.isfinite(f) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _build_series(df: pl.DataFrame) -> dict:
    """提取带状指标(布林带 / Keltner通道 / ATR止损)的每日时间序列。

    这些指标的本质是"每日一条线",随 MA/ATR/σ 漂移,画成曲线才能体现通道形态。
    其余固定价位(枢轴/前高前低等)不在此,仍用水平 markLine。

    返回结构(每个 value 都是按日期对齐的数组):
      {
        "boll":      {"upper": [...], "lower": [...]},
        "keltner_s": {"upper": [...], "lower": [...]},   # 短期 MA20±2ATR
        "keltner_m": {"upper": [...], "lower": [...]},   # 中期 MA60±2.5ATR
        "keltner_l": {"upper": [...], "lower": [...]},   # 长期 MA120±3ATR
        "atr":       {"stop_loss": [...], "take_profit": [...]},  # close∓2ATR
      }
    """
    if df.is_empty() or "close" not in df.columns:
        return {}

    out: dict[str, dict] = {}
    close = df["close"]
    has_atr = "atr_14" in df.columns

    # 布林带(上/下/中轨;中轨 = MA20,数据层已预计算)
    if "boll_upper" in df.columns and "boll_lower" in df.columns:
        out["boll"] = {
            "upper": _to_float_list(df["boll_upper"]),
            "lower": _to_float_list(df["boll_lower"]),
            "mid": _to_float_list(df["ma20"]) if "ma20" in df.columns else None,
        }

    # Keltner 通道三档(需要 ATR)
    if has_atr:
        atr = df["atr_14"]
        # MA120 现场算(不在预计算列中)
        ma120 = df.select(pl.col("close").rolling_mean(120))["close"] if df.height >= 120 else None

        def _channel(ma: pl.Series, n: float) -> dict:
            return {
                "upper": _to_float_list(ma + n * atr),
                "lower": _to_float_list(ma - n * atr),
            }

        if "ma20" in df.columns:
            out["keltner_s"] = _channel(df["ma20"], 2.0)
        if "ma60" in df.columns:
            out["keltner_m"] = _channel(df["ma60"], 2.5)
        if ma120 is not None:
            out["keltner_l"] = _channel(ma120, 3.0)

        # ATR 止损/止盈: close ± 2×ATR(跟随行情漂移的动态止损线)
        out["atr"] = {
            "stop_loss": _to_float_list(close - 2 * atr),
            "take_profit": _to_float_list(close + 2 * atr),
        }

    return out


@router.get("/levels")
def get_levels(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=30, le=500, description="计算样本天数"),
):
    """计算 11 类关键价位(成交密集区压力支撑 / 枢轴点 / 前高前低 /
    布林带 / Keltner短中长 / ATR止损 / 缺口 / 斐波那契 / 整数关口)。

    返回 {levels: {sr, pivot, extreme, boll, keltner_s, keltner_m, keltner_l,
    atr_stop, gap, fib, round}, close, summary, dates, series}。
    前端按 levels 的 key 渲染开关按钮,逐组显隐 markLine / 曲线。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=days * 2)
    # 按资产类型分流: ETF/指数走独立 enriched 存储, 股票保持原路径
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return {"levels": {"sr": [], "pivot": [], "extreme": [],
                           "boll": [], "keltner_s": [], "keltner_m": [], "keltner_l": [],
                           "atr_stop": [], "gap": [], "fib": [], "round": []},
                "close": None, "summary": "无数据", "symbol": symbol,
                "dates": [], "series": {}}

    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    # 日期 + 带状曲线序列(供前端画 Keltner/ATR/布林带曲线)
    dates = df["date"].to_list()
    series = _build_series(df)
    return {
        "levels": levels,
        "close": close,
        "summary": summarize_levels(levels, close),
        "symbol": symbol,
        "dates": [str(d) for d in dates],
        "series": series,
    }


class AnalyzeRequest(BaseModel):
    """AI 个股分析请求。"""
    symbol: str
    focus: str = ""  # 可选:用户追加的分析关注点


@router.post("/analyze")
async def analyze_stock(request: Request, req: AnalyzeRequest):
    """AI 个股四维分析 — NDJSON 流式返回。

    组合 K 线(技术指标)+ 财务表 + 关键价位 → 客观技术分析提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。
    """
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    async def stream_gen():
        async for chunk in analyze_stock_stream(repo, data_dir, req.symbol, req.focus):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 报告 CRUD(历史报告持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 个股分析报告。"""
    symbol: str
    name: str = ""
    focus: str = ""
    content: str
    summary: str = ""
    close: float | None = None
    levels: dict | None = None


@router.get("/reports")
def list_reports(request: Request):
    """获取全部历史报告(按时间降序,后端已裁剪到上限)。"""
    return {"reports": stock_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条报告。"""
    report = stock_reports.save_report({
        "symbol": req.symbol,
        "name": req.name,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "close": req.close,
        "levels": req.levels,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条报告。"""
    ok = stock_reports.delete_report(report_id)
    return {"ok": ok}


# ================================================================
# 轻量 AI 技术面倾向判断(非买卖建议)
# ================================================================

# 仅保留对"倾向判断"有用的列, 控制上下文体积
_SUGGEST_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "change_pct",
    "ma5", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_14", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "vol_ratio_5d", "turnover_rate",
]

# 红线: 与项目 _SYSTEM_PROMPT 一致, 只描述技术面倾向, 不输出任何买卖/操作建议
_SUGGEST_SYSTEM = """你是一位严谨的 A 股技术分析师。基于提供的个股日 K 数据(含 OHLCV 与已计算的技术指标)和关键价位摘要,对该股当前技术面状态给出客观倾向判断。

严格要求:
- 只输出一个 JSON 对象,不要输出任何额外文字、Markdown 或代码块。
- JSON 结构: {"direction": "偏多" | "偏空" | "中性", "confidence": 0-100 的整数, "reason": "一句话技术面理由(引用具体指标数值,如 MACD 金叉/RSI 超买/站上 20 日线)"}
- direction 含义: 偏多=技术形态偏强势; 偏空=技术形态偏弱; 中性=方向不明朗。
- 绝对不输出任何买卖建议、操作指令或仓位建议,只做客观技术面倾向描述。
- confidence 表示你对该倾向判断的把握程度(0-100 整数)。
- reason 中引用的任何价格/指标数值必须与下方提供的数据逐字一致,以"最新交易日"那一行为锚点,禁止编造或凭印象估算数字。

现在请基于下方数据判断。"""

_SUGGEST_USER = """标的标准代码: {symbol}
最新交易日: {as_of}, 最新收盘价: {close}
关键价位概览: {summary}
最近 {n} 个交易日日 K 数据(JSON,升序,最后一行即最新交易日):
```json
{kline}
```
请输出 JSON。"""


def _suggest_rows(df: pl.DataFrame, cols: list[str], n: int) -> list[dict]:
    """取尾部 N 行并序列化为 JSON 安全的 dict 列表(date→str, NaN→None)。"""
    import datetime as _dt
    keep = [c for c in cols if c in df.columns]
    sub = df.select(keep).tail(n)
    if "date" in keep:
        sub = sub.with_columns(pl.col("date").cast(pl.Utf8).alias("date"))
    rows: list[dict] = []
    for rec in sub.to_dicts():
        clean: dict = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else round(v, 4)
            elif isinstance(v, (_dt.date, _dt.datetime)):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return rows


def _strip_think(text: str) -> str:
    """剥除思考链标签内容: 兼容 <think>/<thinking>, 闭合或未闭合(截断)。"""
    return re.sub(
        r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)",
        "",
        text or "",
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


def _parse_suggest_json(text: str) -> dict:
    """从 AI 返回中容错解析 {direction, confidence, reason}。"""
    s = _strip_think(text)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {"direction": "中性", "confidence": 0, "reason": "AI 未返回有效判断"}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {"direction": "中性", "confidence": 0, "reason": "AI 返回无法解析"}
    direction = str(obj.get("direction", "中性"))
    if direction not in ("偏多", "偏空", "中性"):
        direction = "中性"
    try:
        confidence = int(round(float(obj.get("confidence", 0))))
    except Exception:
        confidence = 0
    confidence = max(0, min(100, confidence))
    reason = str(obj.get("reason", "")).strip()
    return {"direction": direction, "confidence": confidence, "reason": reason}


@router.get("/suggest")
async def suggest_stock(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
):
    """轻量 AI 技术面倾向判断(非买卖建议)。

    取最近 120 天日 K → 计算关键价位摘要 + 尾部指标 → 构造"技术面倾向"提示词
    → 调 stream_ai_text 取全文 → 解析 JSON {direction, confidence, reason}。
    返回结构: {symbol, direction, confidence, reason}。
    AI 未配置或失败时返回中性 + 说明, 不抛 500。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=120)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        # 本地无日 K (港美股无缓存 / Free 用户未同步) → 实时拉取 + 算指标,
        # 与 /api/kline/daily 的回退逻辑保持一致 (港美股由 sync_daily_batch 走腾讯免费源)。
        try:
            from app.services import kline_sync
            from app.indicators.pipeline import compute_enriched
            raw = kline_sync.sync_daily_batch([symbol], count=150)
            if not raw.is_empty():
                df = compute_enriched(raw, factors=pl.DataFrame())
        except Exception as e:  # noqa: BLE001
            logger.warning("suggest 日K回退拉取失败 %s: %s", symbol, e)
    if df.is_empty():
        return {"symbol": symbol, "direction": "中性", "confidence": 0, "reason": "暂无日 K 数据"}

    levels = compute_levels(df)
    last_row = df.tail(1).to_dicts()[0]
    close = float(last_row["close"]) if last_row.get("close") is not None else None
    as_of = str(last_row.get("date", ""))[:10]
    summary = summarize_levels(levels, close)
    kline_tail = _suggest_rows(df, _SUGGEST_KEEP_COLS, 45)

    from app.services.ai_provider import ai_configured, stream_ai_text
    if not ai_configured():
        return {"symbol": symbol, "direction": "中性", "confidence": 0, "reason": "AI 未配置,无法生成建议", "as_of": as_of, "close": close}

    user_prompt = _SUGGEST_USER.format(
        symbol=symbol, as_of=as_of, close=close, summary=summary,
        n=len(kline_tail), kline=json.dumps(kline_tail, ensure_ascii=False),
    )
    try:
        parts: list[str] = []
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SUGGEST_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=4000,  # 思考类模型会先消耗大量 token 推理, 需预留 JSON 输出空间
        ):
            parts.append(delta)
        result = _parse_suggest_json("".join(parts))
    except Exception as e:  # noqa: BLE001
        logger.warning("suggest failed for %s: %s", symbol, e)
        result = {"direction": "中性", "confidence": 0, "reason": f"AI 建议生成失败: {e}"}

    result["symbol"] = symbol
    result["as_of"] = as_of
    result["close"] = close
    return result
