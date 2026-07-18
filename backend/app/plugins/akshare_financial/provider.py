"""AKShare 免费财务数据源插件。

基于 AKShare 抓取东方财富公开的财务数据,无需 API Key,用于绕过 TickFlow Expert
套餐的财务数据门槛。注册为 builtin 插件后,在「设置 → 财务数据源」选为本插件即可。

实现要点:
- akshare 懒加载: 后端启动不依赖 akshare, 仅同步时才 import; 未安装时插件标记为
  "不可用", 不影响主流程。
- 范围控制: 默认仅同步自选股(避免对全市场 ~5000 只爬取被东方财富风控),
  可用环境变量 AKSHARE_FINANCIAL_SCOPE=all 改为全量。
- 字段映射: 把 AKShare 的中文列名映射到项目内部规范字段(与前端 FIELD_DEFS 一致)。
  每个内部字段配置多个候选中文列名, 兼容 akshare 不同小版本的命名差异。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

# 本插件声明支持的数据集(供 custom loader 的 provider_has_dataset 识别)
_FINANCIAL_DATASETS = ("financial",)

# table 参数 → AKShare 东方财富财务函数 (akshare 1.x 已拆分为独立函数)
# metrics 需要 indicator 参数; 其余三表只需 symbol。
_TABLE_FUNC: dict[str, tuple[str, str | None]] = {
    "metrics": ("stock_financial_analysis_indicator_em", "按报告期"),
    "income": ("stock_profit_sheet_by_report_em", None),
    "balance_sheet": ("stock_balance_sheet_by_report_em", None),
    "cash_flow": ("stock_cash_flow_sheet_by_report_em", None),
}

# 内部规范字段 → 候选中文列名(按优先级, 取第一个命中)。
# 兼容 akshare 不同版本/不同报表口径的列名差异; 未命中的字段前端显示 "—"。
_FIELD_CANDIDATES: dict[str, dict[str, list[str]]] = {
    "metrics": {
        "eps_basic": ["基本每股收益", "每股收益"],
        "eps_diluted": ["稀释每股收益", "每股收益(摊薄)"],
        "bps": ["每股净资产", "每股净资产(元)"],
        "ocfps": ["每股经营现金流量", "每股经营活动产生的现金流量净额"],
        "roe": ["净资产收益率", "加权净资产收益率", "净资产收益率(加权)"],
        "roe_diluted": ["净资产收益率-摊薄", "净资产收益率(摊薄)"],
        "roa": ["总资产报酬率", "总资产净利润率", "总资产净利率"],
        "gross_margin": ["销售毛利率"],
        "net_margin": ["销售净利率"],
        "debt_to_asset_ratio": ["资产负债率"],
        "revenue_yoy": ["营业收入同比增长率", "营业总收入同比增长率"],
        "net_income_yoy": ["净利润同比增长率", "净利润同比"],
        "operating_cash_to_revenue": ["经营现金流量净额与营业总收入之比"],
        "inventory_turnover": ["存货周转率"],
    },
    "income": {
        "revenue": ["营业收入", "营业总收入"],
        "operating_cost": ["营业成本"],
        "operating_profit": ["营业利润"],
        "selling_expense": ["销售费用"],
        "admin_expense": ["管理费用"],
        "rd_expense": ["研发费用"],
        "financial_expense": ["财务费用"],
        "non_operating_income": ["营业外收入"],
        "non_operating_expense": ["营业外支出"],
        "total_profit": ["利润总额"],
        "income_tax": ["所得税费用"],
        "net_income": ["净利润"],
        "net_income_attributable": ["归属于母公司所有者的净利润"],
        "net_income_deducted": ["扣非净利润", "扣除非经常性损益后的净利润"],
        "basic_eps": ["基本每股收益"],
        "diluted_eps": ["稀释每股收益"],
    },
    "balance_sheet": {
        "total_assets": ["资产总计"],
        "total_current_assets": ["流动资产合计"],
        "total_non_current_assets": ["非流动资产合计"],
        "cash_and_equivalents": ["货币资金"],
        "accounts_receivable": ["应收账款", "应收账款(元)"],
        "inventory": ["存货"],
        "fixed_assets": ["固定资产"],
        "intangible_assets": ["无形资产"],
        "goodwill": ["商誉"],
        "total_liabilities": ["负债合计"],
        "total_current_liabilities": ["流动负债合计"],
        "total_non_current_liabilities": ["非流动负债合计"],
        "short_term_borrowing": ["短期借款"],
        "long_term_borrowing": ["长期借款"],
        "accounts_payable": ["应付账款"],
        "total_equity": ["所有者权益合计", "股东权益合计"],
        "equity_attributable": ["归属于母公司所有者权益合计", "归属于母公司股东权益合计"],
        "retained_earnings": ["未分配利润"],
        "minority_interest": ["少数股东权益"],
    },
    "cash_flow": {
        "net_operating_cash_flow": ["经营活动产生的现金流量净额", "经营活动现金流入小计"],
        "net_investing_cash_flow": ["投资活动产生的现金流量净额"],
        "net_financing_cash_flow": ["筹资活动产生的现金流量净额"],
        "capex": ["购建固定资产、无形资产和其他长期资产支付的现金"],
        "net_cash_change": ["现金及现金等价物净增加额"],
    },
}


@dataclass
class _AKShareFinancialConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本插件。"""

    name: str = "akshare_financial"
    display_name: str = "AKShare 免费财务(东方财富)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_FINANCIAL_DATASETS))
    path: None = None
    builtin: bool = True


def _to_ak_dotted(symbol: str) -> str | None:
    """项目代码 600519.SH → AKShare 点分代码 600519.SH (指标表函数使用)。"""
    if "." not in symbol:
        return None
    code, exch = symbol.split(".", 1)
    return f"{code}.{exch.upper()}"


def _to_ak_prefixed(symbol: str) -> str | None:
    """项目代码 600519.SH → AKShare 前缀代码 SH600519 (三张报表函数使用)。"""
    if "." not in symbol:
        return None
    code, exch = symbol.split(".", 1)
    exch = exch.upper()
    if exch == "SH":
        return "SH" + code
    if exch == "SZ":
        return "SZ" + code
    if exch == "BJ":
        return "BJ" + code
    return None


def _fetch_with_retry(ak_func: Any, *, retries: int = 3, base_delay: float = 0.8, **kwargs: Any):
    """调用 akshare 函数, 对瞬时网络/SSL 错误做指数退避重试。

    东方财富对高频访问会偶发断连(SSL: UNEXPECTED_EOF / connection reset /
    read timeout 等)。这类错误重试通常能成功, 故在此吞掉并退避重试;
    最后一次仍失败则向上抛出, 由调用方记录并跳过该股。
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return ak_func(**kwargs)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            msg = str(e).lower()
            transient = any(
                k in msg
                for k in (
                    "ssl", "eof", "timed out", "timeout", "connection",
                    "max retries", "reset", "temporarily", "remotedisconnected",
                )
            )
            if not transient or attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.info(
                "akshare_financial: 瞬时错误, %.1fs 后重试(%d/%d): %s",
                delay, attempt + 1, retries, e,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    return None


def _coerce_num(v: Any) -> float | None:
    """把 AKShare 的数值(可能带千分位逗号/字符串/NaN)转成 float。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f  # NaN → None
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _period_col(df: pl.DataFrame) -> str | None:
    for c in ("报告期", "日期"):
        if c in df.columns:
            return c
    return None


def availability() -> tuple[bool, str]:
    """插件可用性检测: akshare 是否已安装。"""
    try:
        import akshare  # noqa: F401

        return True, "akshare 已安装"
    except Exception as e:  # noqa: BLE001
        return False, f"akshare 未安装: {e}"


class AKShareFinancialProvider:
    """基于 AKShare 的免费财务数据源。"""

    name = "akshare_financial"
    builtin = True

    def __init__(self) -> None:
        self.config = _AKShareFinancialConfig()

    def close(self) -> None:
        pass

    # ---- 核心: 拉取财务数据 ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,  # noqa: ARG002
    ) -> pl.DataFrame:
        try:
            import akshare as ak
        except Exception as e:  # noqa: BLE001
            logger.warning("akshare_financial: akshare 未安装, 无法拉取财务数据: %s", e)
            return pl.DataFrame()

        if table not in _TABLE_FUNC:
            logger.warning("akshare_financial: 不支持的表 %s", table)
            return pl.DataFrame()

        # 范围控制: 默认仅自选股, 避免对全市场爬取被东方财富风控
        scope = (os.getenv("AKSHARE_FINANCIAL_SCOPE") or "watchlist").lower()
        if scope == "watchlist":
            try:
                from app.services import watchlist as wl

                wl_syms = {row["symbol"] for row in wl.list_symbols()}
                if wl_syms:
                    symbols = [s for s in symbols if s in wl_syms]
                else:
                    # 自选股为空: 退化为同步全部传入标的(即 instruments 宇宙)。
                    # 否则"全部同步"会因过滤后为空而静默写入 0 行, 用户看到页面无数据却不知原因。
                    logger.warning(
                        "akshare_financial: 自选股为空, 退化为同步全部 %d 只标的(把股票加入自选后可只同步关注标的)",
                        len(symbols),
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare_financial: 读取自选股失败, 退化为全量: %s", e)

        func_name, indicator = _TABLE_FUNC[table]
        ak_func = getattr(ak, func_name, None)
        if ak_func is None:
            logger.warning("akshare_financial: akshare 缺少函数 %s (版本不匹配?)", func_name)
            return pl.DataFrame()

        # 指标表优先点分代码, 三张报表优先前缀代码; 二者互为回退以兼容不同版本。
        primary, alternate = (
            (_to_ak_dotted, _to_ak_prefixed)
            if table == "metrics"
            else (_to_ak_prefixed, _to_ak_dotted)
        )
        candidates = _FIELD_CANDIDATES[table]
        rows: list[dict] = []

        # 每股请求后的友好限速(秒), 可用环境变量调节以规避风控。
        try:
            throttle = float(os.getenv("AKSHARE_FINANCIAL_THROTTLE") or "0.15")
        except ValueError:
            throttle = 0.15

        for sym in symbols:
            df = None
            for to_code in (primary, alternate):
                ak_code = to_code(sym)
                if not ak_code:
                    continue
                kwargs = {"symbol": ak_code}
                if indicator is not None:
                    kwargs["indicator"] = indicator
                try:
                    df = _fetch_with_retry(ak_func, **kwargs)
                    if df is not None and len(df) > 0:
                        break
                except Exception as e:  # noqa: BLE001
                    logger.warning("akshare_financial: %s 拉取失败(code=%s): %s", sym, ak_code, e)
                    df = None
            if df is None or len(df) == 0:
                continue
            try:
                pdf = pl.from_pandas(df)
            except Exception:  # noqa: BLE001
                continue
            pcol = _period_col(pdf)
            if pcol is None:
                continue
            pdf = pdf.with_columns(pl.col(pcol).cast(pl.Utf8, strict=False))
            pdf = pdf.sort(pcol, descending=True).head(1)
            if pdf.is_empty():
                continue
            rec = pdf.to_dicts()[0]
            period = str(rec.get(pcol, "") or "").strip()
            if not period:
                continue
            out: dict[str, Any] = {"symbol": sym, "period_end": period}
            for internal_field, names in candidates.items():
                for cn in names:
                    if cn in rec and rec[cn] not in (None, ""):
                        out[internal_field] = _coerce_num(rec[cn])
                        break
            rows.append(out)
            # 友好限速, 降低触发东方财富风控的概率
            if throttle > 0:
                time.sleep(throttle)

        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows)

    # ---- 设置页试拉测试 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = symbols or ["600519.SH"]
        table = "metrics" if dataset in (None, "financial") else dataset
        df = self.get_financials(table, syms, latest_only=True)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": df.head(5).to_dicts() if not df.is_empty() else [],
        }
