"""腾讯财经免费数据源 (多市场: A股 / 港股 / 美股)。

接口 (均无需 API Key):
  - 实时快照: http://qt.gtimg.cn/q={gtimg1},{gtimg2},...  返回 GBK 文本, v_{gtimg}="~" 分隔字段
  - 日 K(前复权): https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={gtimg},day,{start},{end},{count},qfq
  - 标的信息联想: https://smartbox.gtimg.cn/s3/?v=2&t=all&q={keyword}  返回 v_hint="市场~代码~名称~匹配词~GP^..."

项目 symbol 约定: A股 600519.SH / 000001.SZ / xxx.BJ; 港股 00700.HK; 美股 AAPL.US。
gtimg 代码约定: sh600519 / sz000001 / bjxxx / hk00700 / usAAPL。

volume 单位按市场不同 (最高风险点, 待真实请求校准):
  - A股/北交所: 腾讯返回「手」, ×100 转股。
  - 港股/美股: 腾讯返回「股」, ×1。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities

logger = logging.getLogger(__name__)

# gtimg 前缀 → 项目后缀 / 市场判定
_PREFIX_TO_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ", "hk": "HK", "us": "US"}
_SUFFIX_TO_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk", "US": "us"}
# 美股 smartbox 代码可能带交易所后缀 (.OQ/.N/.O/.SQ...), 统一当美股处理
_US_SUFFIXES = {"US", "OQ", "N", "O", "SQ", "NMS", "NGS"}

# volume 单位换算: 腾讯 A股/北交所返回「手」, 港/美返回「股」
_VOLUME_UNIT = {"sh": 100, "sz": 100, "bj": 100, "hk": 1, "us": 1}

_REALTIME_URL = "http://qt.gtimg.cn/q="
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_SEARCH_URL = "https://smartbox.gtimg.cn/s3/"

# 实时字段索引 (A/港/美同结构, 与通用腾讯格式一致)
_RT = {
    "name": 1,
    "code": 2,
    "last_price": 3,
    "prev_close": 4,
    "open": 5,
    "high": 33,
    "low": 34,
    "volume": 36,
    "amount": 37,
    "turnover_rate": 38,
    "amplitude": 40,
    "time": 30,
}

_GTIMG_KEY_RE = re.compile(r"v_([a-zA-Z0-9]+)=\"([^\"]*)\"")
_HINT_RE = re.compile(r"v_hint=\"([^\"]*)\"")


def _market_of(gtimg: str) -> str:
    """返回 gtimg 代码的市场前缀 (sh/sz/bj/hk/us)。"""
    prefix = gtimg[:2].lower()
    return prefix if prefix in _VOLUME_UNIT else gtimg[:2]


def is_overseas(symbol: str) -> bool:
    """是否为港股/美股 (数据强制走腾讯免费源)。"""
    return bool(symbol) and (symbol.endswith(".HK") or symbol.endswith(".US"))


class TencentProvider:
    name = "tencent"
    capabilities = ProviderCapabilities(
        instruments=False,
        daily=True,
        adj_factor=True,  # qfq 日K 已含复权, 实际因子由 same_as_daily 派生
        minute=False,
        realtime=True,
        financial=False,
    )

    # ---------- symbol 双向转换 ----------
    @staticmethod
    def to_gtimg(symbol: str) -> str | None:
        """项目 symbol → gtimg 代码。无法识别返回 None。"""
        if not symbol or "." not in symbol:
            return None
        code, exch = symbol.rsplit(".", 1)
        exch = exch.upper()
        if exch in _SUFFIX_TO_PREFIX:
            return f"{_SUFFIX_TO_PREFIX[exch]}{code}"
        if exch in _US_SUFFIXES:  # 形如 AAPL.OQ
            return f"us{code.upper()}"
        return None

    @staticmethod
    def from_gtimg(gtimg: str) -> str | None:
        """gtimg 代码 → 项目 symbol。无法识别返回 None。"""
        if not gtimg:
            return None
        prefix = gtimg[:2].lower()
        if prefix in _PREFIX_TO_SUFFIX:
            return f"{gtimg[2:]}.{_PREFIX_TO_SUFFIX[prefix]}"
        return None

    # ---------- 实时行情 ----------
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """返回 list[dict], 字段对齐 quote_service._process_full_market_records。"""
        syms = symbols or []
        if not syms:
            return []
        gtimg_map = {}
        gtimg_codes = []
        for s in syms:
            g = self.to_gtimg(s)
            if g:
                # key 用小写, 与下方正则 group(1).lower() 对齐 (美股代码含大写字母,
                # 如 usAAPL → usaapl, 否则大小写不匹配导致整条被丢弃)
                gtimg_map[g.lower()] = s
                gtimg_codes.append(g)
        if not gtimg_codes:
            return []

        url = _REALTIME_URL + ",".join(gtimg_codes)
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                resp.raise_for_status()
                text = resp.content.decode("gbk", errors="ignore")
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯实时行情拉取失败: %s", e)
            return []

        records: list[dict] = []
        for m in _GTIMG_KEY_RE.finditer(text):
            gtimg = m.group(1).lower()
            symbol = gtimg_map.get(gtimg)
            if not symbol:
                continue
            fields = m.group(2).split("~")
            rec = self._parse_realtime_fields(symbol, fields)
            if rec:
                records.append(rec)
        return records

    @staticmethod
    def _parse_realtime_fields(symbol: str, f: list[str]) -> dict | None:
        if len(f) < 41:
            return None

        def num(i: int):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return None

        last_price = num(_RT["last_price"])
        prev_close = num(_RT["prev_close"])
        open_ = num(_RT["open"])
        high = num(_RT["high"])
        low = num(_RT["low"])
        volume_raw = num(_RT["volume"])
        amount = num(_RT["amount"])
        # 换手率: A股/北交所腾讯实时接口返回的是百分比值 (可直接展示);
        # 港美股该字段不可靠 (港股恒为 0, 美股格式错位), 且无流通股本无法自行计算,
        # 故对海外标的置 None, 前端展示 "—" 而非错误数字。
        turnover_rate = None if is_overseas(symbol) else num(_RT["turnover_rate"])

        # 实时传入的是项目 symbol (600519.SH / 00700.HK / AAPL.US), 需按后缀取市场,
        # 不能复用 _market_of (它吃 gtimg 代码前缀 sh/sz/...)。
        suffix = symbol.rsplit(".", 1)[-1].lower() if "." in symbol else ""
        market = suffix if suffix in _VOLUME_UNIT else "sh"
        volume = (volume_raw * _VOLUME_UNIT.get(market, 1)) if volume_raw is not None else None

        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = last_price - prev_close
            change_pct = change_amount / prev_close  # 小数制 (0.0366 = 3.66%)

        amplitude = None
        if high is not None and low is not None and prev_close not in (None, 0):
            amplitude = (high - low) / prev_close  # 小数制

        ts = _parse_timestamp(f[_RT["time"]]) if len(f) > _RT["time"] else None

        return {
            "symbol": symbol,
            "name": f[_RT["name"]] if len(f) > _RT["name"] else None,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "amplitude": amplitude,
            "turnover_rate": turnover_rate,  # 透传原始值, 单位待校准
            "timestamp": ts,
            "session": None,
        }

    # ---------- 日 K ----------
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        **kwargs,  # 吸收 on_chunk_done 等调用方透传参数
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()

        start = (start_time or datetime.now()).strftime("%Y-%m-%d")
        end = (end_time or datetime.now()).strftime("%Y-%m-%d")
        count = 800

        rows: list[dict] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                for s in symbols:
                    g = self.to_gtimg(s)
                    if not g:
                        continue
                    url = f"{_KLINE_URL}?param={g},day,{start},{end},{count},qfq"
                    try:
                        resp = client.get(url)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("腾讯日K拉取失败 %s: %s", s, e)
                        continue
                    klines = self._extract_klines(data, g)
                    market = _market_of(g)
                    unit = _VOLUME_UNIT.get(market, 1)
                    for row in klines:
                        rec = self._parse_kline_row(s, row, unit)
                        if rec:
                            rows.append(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯日K批量拉取异常: %s", e)
            return pl.DataFrame(rows) if rows else pl.DataFrame()

        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        if "date" in df.columns:
            df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
        keep = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        return df.select(keep)

    @staticmethod
    def _extract_klines(data: dict, gtimg: str) -> list:
        node = (data.get("data") or {}).get(gtimg) or {}
        for key in ("qfqday", "day", "qfq", "kline"):
            if key in node and isinstance(node[key], list):
                return node[key]
        return []

    @staticmethod
    def _parse_kline_row(symbol: str, row: list, unit: int) -> dict | None:
        if not isinstance(row, list) or len(row) < 6:
            return None
        try:
            date = str(row[0])
            open_ = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5]) * unit
        except (ValueError, TypeError):
            return None
        # amount 在港/美常缺失(第6位是附加信息 dict), A股多为数值; K线值为字符串需强转
        amount = None
        for idx in (6, 7):
            if len(row) > idx:
                try:
                    amount = float(row[idx])
                    break
                except (ValueError, TypeError):
                    continue
        return {
            "symbol": symbol,
            "date": date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }

    # ---------- 复权因子 (qfq 已含, 返回空) ----------
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        **kwargs,
    ) -> pl.DataFrame:
        return pl.DataFrame()

    # ---------- 全市场标的清单 (腾讯无, 返回空) ----------
    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        return pl.DataFrame()

    # ---------- 标的信息联想 (smartbox) ----------
    def search_instruments(self, q: str, limit: int = 20) -> list[dict]:
        """腾讯联想搜索, 返回 [{symbol, name, code, asset_type}]。"""
        q = (q or "").strip()
        if not q:
            return []
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(_SEARCH_URL, params={"v": "2", "t": "all", "q": q})
                resp.raise_for_status()
                # smartbox 返回 ASCII 文本, 中文名为字面量 \uXXXX 转义 (如 \u817e\u8baf=腾讯),
                # 需用 json.loads 解码为真正汉字。
                import json as _json
                text = resp.text
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯搜索失败: %s", e)
            return []

        m = _HINT_RE.search(text)
        if not m:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for part in m.group(1).split("^"):
            fields = part.split("~")
            if len(fields) < 3:
                continue
            market, code, name_raw = fields[0], fields[1], fields[2]
            # 将 \uXXXX 转义序列解码为实际汉字
            try:
                name = _json.loads(f'"{name_raw}"')
            except Exception:  # noqa: BLE001
                name = name_raw
            symbol = _smartbox_to_symbol(market, code)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            asset_type = "stock_hk" if symbol.endswith(".HK") else ("stock_us" if symbol.endswith(".US") else "stock")
            out.append({"symbol": symbol, "name": name, "code": symbol.split(".")[0], "asset_type": asset_type})
            if len(out) >= limit:
                break
        return out


def _smartbox_to_symbol(market: str, code: str) -> str | None:
    market = (market or "").lower()
    code = (code or "").strip()
    if not code:
        return None
    if market in ("sh", "sz", "bj"):
        return f"{code}.{market.upper()}"
    if market == "hk":
        return f"{code}.HK"
    if market == "us" or "." in code:  # 美股代码可能带 .OQ/.N 后缀
        base = code.split(".")[0].upper()
        return f"{base}.US"
    return None


def _parse_timestamp(value: str | None) -> int | None:
    """腾讯时间字符串 → 毫秒级 epoch (Int64)。支持 2026-07-17 15:00:00 / 2026/07/17 16:08:19。"""
    if not value:
        return None
    s = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None
