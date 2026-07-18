"""自选股 API。"""
from __future__ import annotations

import logging
import math
import time
from datetime import date

import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from app.services import watchlist
from app.services.watchlist_ocr import import_watchlist_image
from app.services.watchlist_ocr.provider import get_ocr_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_MAX_IMPORT_IMAGE_BYTES = 12 * 1024 * 1024  # 12MB
_IMPORT_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
}


class AddRequest(BaseModel):
    symbol: str
    note: str = ""


class BatchAddRequest(BaseModel):
    symbols: list[str]
    note: str = ""


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        # 股票 + ETF 名称统一由 repo.get_name_map 解析, 自选列表可混合持有
        name_by_symbol = request.app.state.repo.get_name_map([r.get("symbol") for r in rows])
        if not name_by_symbol:
            return rows
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


@router.get("")
def list_all(request: Request):
    return {"symbols": _with_names(watchlist.list_symbols(), request)}


@router.post("")
def add_one(req: AddRequest, request: Request):
    rows = watchlist.add(req.symbol, req.note)
    return {"symbols": _with_names(rows, request)}


@router.post("/batch")
def add_batch(req: BatchAddRequest, request: Request):
    for sym in req.symbols:
        watchlist.add(sym, req.note)
    return {"symbols": _with_names(watchlist.list_symbols(), request), "added": len(req.symbols)}


@router.get("/ocr-status")
def ocr_status():
    """当前 OCR 引擎是否可用（前端可据此提示安装依赖）。"""
    provider = get_ocr_provider()
    return {"provider": provider.name, "available": provider.available()}


@router.post("/import-image")
async def import_from_image(request: Request, file: UploadFile = File(...)):
    """从自选截图识别股票代码，返回候选列表（不自动写入自选）。"""
    import anyio

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = (file.filename or "").lower()
    # 严格白名单：不接受任意 image/*（如 image/svg+xml）
    ok_type = content_type in _IMPORT_IMAGE_TYPES
    ok_ext = filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"))
    if not ok_type and not ok_ext:
        raise HTTPException(400, "仅支持 JPG / PNG / WebP / BMP / GIF 图片")

    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > _MAX_IMPORT_IMAGE_BYTES:
        raise HTTPException(400, "图片过大（上限 12MB）")

    existing = {r["symbol"] for r in watchlist.list_symbols()}
    data_dir = request.app.state.repo.store.data_dir
    try:
        # OCR 为同步 CPU/子进程；丢进线程池，避免卡住事件循环（行情 SSE 等）
        result = await anyio.to_thread.run_sync(
            lambda: import_watchlist_image(data, data_dir, existing_symbols=existing),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("watchlist import-image failed")
        raise HTTPException(500, f"识别失败: {e}") from e

    # 响应不回传整段 raw_text（可能很长）；调试时可开 query，这里默认省略
    result.pop("raw_text", None)
    return result


@router.post("/{symbol}/top")
def move_one_to_top(symbol: str, request: Request):
    rows = watchlist.move_to_top(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("/{symbol}")
def remove_one(symbol: str, request: Request):
    rows = watchlist.remove(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("")
def clear_all():
    """清空自选列表。"""
    count = watchlist.clear()
    return {"removed": count}


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol", "close", "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


# ── 港美股日 K 缓存 (进程内 TTL, 避免每次请求都网络拉取) ──
_OVERSEAS_DAILY_CACHE: dict[str, tuple[float, "pl.DataFrame"]] = {}
_OVERSEAS_DAILY_TTL = 600.0  # 秒


def _get_overseas_daily(symbols: list[str]) -> "pl.DataFrame":
    """拉取港美股日 K (腾讯免费源) 并带进程内 TTL 缓存。

    返回规范化日 K (symbol/date/open/high/low/close/volume/amount);
    空 DataFrame 表示拉取失败或无数据。
    """
    if not symbols:
        return pl.DataFrame()
    cache_key = ",".join(sorted(symbols))
    now = time.perf_counter()
    cached = _OVERSEAS_DAILY_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _OVERSEAS_DAILY_TTL:
        return cached[1]
    from datetime import datetime, timedelta
    from app.data_providers.tencent_provider import TencentProvider
    end = datetime.now()
    start = end - timedelta(days=400)  # 覆盖 ~250 交易日 + 余量
    try:
        df = TencentProvider().get_daily(symbols, start_time=start, end_time=end)
    except Exception:  # noqa: BLE001
        logger.warning("港美股日K拉取失败, 降级无指标", exc_info=True)
        df = pl.DataFrame()
    _OVERSEAS_DAILY_CACHE[cache_key] = (now, df)
    return df


@router.get("/enriched")
def watchlist_enriched(
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """自选股 enriched 数据 — 直接从 enriched 最新日读取, 无即时计算。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。
    """
    t0 = time.perf_counter()

    repo = request.app.state.repo
    symbols = [r["symbol"] for r in watchlist.list_symbols()]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    # 按资产拆分自选 symbol; ETF enriched 是独立缓存, 仅自选真的含 ETF 才去加载
    # (避免无 ETF 用户在缓存冷启动时触发 ETF 全量懒加载)
    from app.data_providers.tencent_provider import is_overseas as _is_overseas

    etf_set = repo.get_etf_symbol_set()
    stock_symbols = [s for s in symbols if s not in etf_set and not _is_overseas(s)]
    etf_symbols = [s for s in symbols if s in etf_set]
    overseas_symbols = [s for s in symbols if _is_overseas(s) and s not in etf_set]

    df_e, cache_date = repo.get_enriched_latest()

    # 以自选列表为主表 LEFT JOIN enriched, 保证自选的每一只都返回一行;
    # 不在 enriched 缓存里的标的 (新股/冷门股/新用户未同步) 指标为 null, 前端渲染为 "—".
    # 旧实现是 df_e.filter(is_in(stock_symbols)), 方向反了 (以 enriched 为主),
    # 会把不在缓存 universe 里的自选股静默丢弃.
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(df_e, on="symbol", how="left")
    else:
        df = pl.DataFrame()

    # ETF 行合并; 缺失列 (换手率/涨跌停信号等) 为 null
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            # ETF 同样以自选为主表 LEFT JOIN, 缺失标的指标为 null
            df_etf = etf_watchlist_df.join(df_etf_all, on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")

    # 海外标的 (HK/US): enriched 缓存不含此类标的。
    # 用腾讯日 K 计算技术指标/信号 (涨跌停信号因港美股无涨跌停机制不计算),
    # 再用实时接口补实时价 + 名称。
    if overseas_symbols:
        try:
            from app.data_providers.tencent_provider import TencentProvider
            from app.indicators.pipeline import compute_indicators, compute_signals

            # 1) 日 K → 指标 → 纯价格信号, 取每个 symbol 最新一天
            daily_ov = _get_overseas_daily(overseas_symbols)
            df_ind: pl.DataFrame | None = None
            if not daily_ov.is_empty():
                df_ind = compute_indicators(daily_ov)
                df_ind = compute_signals(df_ind)
                # 仅保留 symbol + 指标/信号列, 丢弃原始 OHLCV (避免与实时快照列冲突)
                _drop = {"date", "open", "high", "low", "close", "volume", "amount"}
                _ind_cols = [c for c in df_ind.columns if c not in _drop and c != "symbol"]
                df_ind = (
                    df_ind.sort(["symbol", "date"])
                    .group_by("symbol", maintain_order=True)
                    .last()
                    .select(["symbol"] + _ind_cols)
                )

            # 2) 实时快照补实时价 + 名称
            df_rt: pl.DataFrame | None = None
            rt_records = TencentProvider().get_realtime(symbols=overseas_symbols)
            if rt_records:
                _rt_rows = []
                for r in rt_records:
                    _rt_rows.append({
                        "symbol": r["symbol"],
                        "name": r.get("name"),
                        "close": r.get("last_price"),
                        "change_pct": r.get("change_pct"),
                        "change_amount": r.get("change_amount"),
                        "amount": r.get("amount"),
                        "turnover_rate": r.get("turnover_rate"),
                        "amplitude": r.get("amplitude"),
                        "volume": r.get("volume"),
                    })
                df_rt = pl.DataFrame(_rt_rows)

            # 3) 合并: 以自选列表为主表保证每行; 实时价优先, 指标/信号从日 K 并入
            df_ov = pl.DataFrame({"symbol": overseas_symbols})
            if df_rt is not None and not df_rt.is_empty():
                df_ov = df_ov.join(df_rt, on="symbol", how="left")
            if df_ind is not None and not df_ind.is_empty():
                df_ov = df_ov.join(df_ind, on="symbol", how="left")
            df = df_ov if df.is_empty() else pl.concat([df, df_ov], how="diagonal_relaxed")
        except Exception:  # noqa: BLE001
            logger.debug("海外标的指标计算失败, 降级为空行", exc_info=True)

    # as_of 取各类缓存中较旧者, 避免把旧的 ETF 行标成股票缓存日期
    dates = [d for d in (cache_date if stock_symbols else None, etf_date) if d is not None]
    as_of = min(dates) if dates else None
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None, "elapsed_ms": 0}

    # JOIN float_shares (仅股票有) + 名称 (股票/ETF 统一走 get_name_map)
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        df = df.join(df_i.select(["symbol", "float_shares"]), on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    # 名称优先用本地 instruments 名称; 海外标的 (HK/US) 无本地名称, 保留实时接口已带回的名称
    if "name" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("name"))
    df = df.with_columns(
        pl.col("name").fill_null(
            pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8)
        ).alias("name")
    )

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares"] if c in df.columns]
    df = df.select(keep)

    # 动态 JOIN 扩展数据表
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore
        from app.api.ext_data import _read_ext_dataframe

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                # 扩展时序数据必须只取最新分区；否则一个 symbol 会按历史分区数被 JOIN 放大。
                cfg = configs.get(config_id)
                if cfg:
                    ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                else:
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, \"{field_name}\" FROM {view_name}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df
                        .select(["symbol", field_name])
                        .unique(subset=["symbol"], keep="last")
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                # view 不存在或字段不存在，尝试直接读 parquet
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    # 按自选添加顺序（新加的在前）重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order"))
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None, "elapsed_ms": elapsed}


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if config_id and field_name:
            result.append((config_id, field_name))
    return result
