"""TencentProvider 离线单测 (mock GBK 实时 / JSON 日K / smartbox 搜索)。

覆盖: symbol 双向转换、实时字段映射、volume 单位分市场、日K 解析、
搜索解析、港美股强制路由判定、capabilities。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from app.data_providers.tencent_provider import (
    TencentProvider,
    _parse_timestamp,
    _smartbox_to_symbol,
    is_overseas,
)


# ============ symbol 双向转换 ============
def test_to_gtimg_ashare():
    assert TencentProvider.to_gtimg("600519.SH") == "sh600519"
    assert TencentProvider.to_gtimg("000001.SZ") == "sz000001"
    assert TencentProvider.to_gtimg("830799.BJ") == "bj830799"


def test_to_gtimg_overseas():
    assert TencentProvider.to_gtimg("00700.HK") == "hk00700"
    assert TencentProvider.to_gtimg("AAPL.US") == "usAAPL"
    # 带交易所后缀的美股代码也统一当美股
    assert TencentProvider.to_gtimg("AAPL.OQ") == "usAAPL"
    # .B 不在已知后缀, 返回 None
    assert TencentProvider.to_gtimg("BRK.B") is None


def test_to_gtimg_invalid():
    assert TencentProvider.to_gtimg("600519") is None
    assert TencentProvider.to_gtimg("") is None
    assert TencentProvider.to_gtimg("FOO.BAR") is None


def test_from_gtimg():
    assert TencentProvider.from_gtimg("sh600519") == "600519.SH"
    assert TencentProvider.from_gtimg("sz000001") == "000001.SZ"
    assert TencentProvider.from_gtimg("hk00700") == "00700.HK"
    assert TencentProvider.from_gtimg("usAAPL") == "AAPL.US"


def test_to_from_roundtrip():
    for sym in ["600519.SH", "000001.SZ", "830799.BJ", "00700.HK", "AAPL.US"]:
        assert TencentProvider.from_gtimg(TencentProvider.to_gtimg(sym)) == sym


# ============ 港美股强制路由判定 ============
def test_is_overseas():
    assert is_overseas("00700.HK")
    assert is_overseas("AAPL.US")
    assert not is_overseas("600519.SH")
    assert not is_overseas("000001.SZ")
    assert not is_overseas("")


# ============ capabilities ============
def test_capabilities():
    caps = TencentProvider.capabilities
    assert caps.daily and caps.realtime and caps.adj_factor
    assert not caps.minute and not caps.financial and not caps.instruments


# ============ 实时字段解析 ============
def _rt_fields(**overrides) -> list[str]:
    """构造一个长度 ≥41 的腾讯实时字段列表 (索引对齐 _RT)。"""
    f = [""] * 45
    f[1] = "贵州茅台"        # name
    f[2] = "600519"          # code
    f[3] = "1680.0"          # last_price
    f[4] = "1700.0"          # prev_close
    f[5] = "1690.0"          # open
    f[30] = "2026-07-17 15:00:00"  # time
    f[33] = "1710.0"         # high
    f[34] = "1670.0"         # low
    f[36] = "42400"          # volume (手, A股)
    f[37] = "7130000000"     # amount
    f[38] = "0.34"           # turnover_rate
    f[40] = "0.0235"         # amplitude
    for k, v in overrides.items():
        f[int(k)] = v
    return f


def test_parse_realtime_ashare_fields_and_volume_unit():
    rec = TencentProvider._parse_realtime_fields("600519.SH", _rt_fields())
    assert rec["symbol"] == "600519.SH"
    assert rec["name"] == "贵州茅台"
    assert rec["last_price"] == 1680.0
    assert rec["prev_close"] == 1700.0
    # change_amount = 1680 - 1700 = -20; pct = -20/1700
    assert rec["change_amount"] == pytest.approx(-20.0)
    assert rec["change_pct"] == pytest.approx(-20 / 1700)
    # amplitude = (1710 - 1670)/1700 = 40/1700
    assert rec["amplitude"] == pytest.approx(40 / 1700)
    # 换手率透传原始值
    assert rec["turnover_rate"] == 0.34
    # A股 volume 手×100 → 股
    assert rec["volume"] == 4_240_000
    # 时间戳解析为毫秒 epoch
    assert rec["timestamp"] is not None


def test_parse_realtime_hk_volume_unit():
    rec = TencentProvider._parse_realtime_fields("00700.HK", _rt_fields())
    # 港股 volume 已为股, ×1
    assert rec["volume"] == 42400


def test_parse_realtime_us_volume_unit():
    rec = TencentProvider._parse_realtime_fields("AAPL.US", _rt_fields())
    assert rec["volume"] == 42400


def test_parse_realtime_short_fields_returns_none():
    assert TencentProvider._parse_realtime_fields("600519.SH", [""] * 10) is None


def test_parse_timestamp_compact_format():
    ts = _parse_timestamp("20260717161459")
    assert ts is not None
    assert datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") == "2026-07-17 16:14:59"


def test_parse_timestamp_slash_format():
    ts = _parse_timestamp("2026/07/17 16:08:19")
    assert ts is not None


def test_parse_timestamp_invalid():
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None


# ============ 实时 HTTP (GBK) ============
def _mock_client(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = text.encode("gbk")
    resp.text = text
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


def _rt_text(gtimg: str, f: list[str]) -> str:
    return f'v_{gtimg}="{"~".join(f)}"'


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_realtime_parses_gbk(mock_cls):
    text = _rt_text("sh600519", _rt_fields())
    mock_cls.return_value = _mock_client(text)
    recs = TencentProvider().get_realtime(symbols=["600519.SH"])
    assert len(recs) == 1
    assert recs[0]["symbol"] == "600519.SH"
    assert recs[0]["volume"] == 4_240_000


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_realtime_skips_unknown_symbol(mock_cls):
    text = _rt_text("sh600519", _rt_fields())
    mock_cls.return_value = _mock_client(text)
    # 请求的是 AAPL, 但返回的是茅台 → 应被过滤
    recs = TencentProvider().get_realtime(symbols=["AAPL.US"])
    assert recs == []


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_realtime_empty_on_error(mock_cls):
    mock_cls.side_effect = RuntimeError("network down")
    assert TencentProvider().get_realtime(symbols=["600519.SH"]) == []


# ============ 日 K 解析 ============
def _mock_json_client(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_daily_parses_qfq(mock_cls):
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-07-15", "1680.0", "1700.0", "1710.0", "1670.0", "42400", "7130000000"],
                    ["2026-07-16", "1700.0", "1690.0", "1720.0", "1680.0", "38000", "6500000000"],
                ]
            }
        }
    }
    mock_cls.return_value = _mock_json_client(payload)
    df = TencentProvider().get_daily(["600519.SH"], None, None)
    assert isinstance(df, pl.DataFrame)
    assert df.height == 2
    for col in ("symbol", "date", "open", "high", "low", "close", "volume", "amount"):
        assert col in df.columns
    # A股 volume 手×100
    assert df["volume"][0] == 4_240_000
    assert df["close"][0] == 1700.0
    assert df["amount"][0] == 7_130_000_000


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_daily_hk_volume_unit_and_missing_amount(mock_cls):
    payload = {
        "data": {
            "hk00700": {
                "qfqday": [["2026-07-15", "450.0", "455.0", "460.0", "448.0", "25540000", ""]]
            }
        }
    }
    mock_cls.return_value = _mock_json_client(payload)
    df = TencentProvider().get_daily(["00700.HK"], None, None)
    assert df.height == 1
    # 港股 volume 已为股, ×1
    assert df["volume"][0] == 25_540_000
    # 空字符串 amount → None
    assert df["amount"][0] is None


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_daily_falls_back_to_day_key(mock_cls):
    payload = {
        "data": {
            "sh600519": {
                "day": [["2026-07-15", "1680.0", "1700.0", "1710.0", "1670.0", "42400", "7130000000"]]
            }
        }
    }
    mock_cls.return_value = _mock_json_client(payload)
    df = TencentProvider().get_daily(["600519.SH"], None, None)
    assert df.height == 1


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_get_daily_empty_on_no_data(mock_cls):
    mock_cls.return_value = _mock_json_client({"data": {}})
    df = TencentProvider().get_daily(["600519.SH"], None, None)
    assert df.height == 0


def test_get_adj_factors_empty():
    assert TencentProvider().get_adj_factors(["600519.SH"], None, None).height == 0


def test_get_instruments_empty():
    assert TencentProvider().get_instruments("stock").height == 0


# ============ 搜索解析 ============
def test_smartbox_to_symbol():
    assert _smartbox_to_symbol("sh", "600519") == "600519.SH"
    assert _smartbox_to_symbol("sz", "000001") == "000001.SZ"
    assert _smartbox_to_symbol("bj", "830799") == "830799.BJ"
    assert _smartbox_to_symbol("hk", "00700") == "00700.HK"
    assert _smartbox_to_symbol("us", "aapl.oq") == "AAPL.US"
    assert _smartbox_to_symbol("us", "AAPL") == "AAPL.US"
    assert _smartbox_to_symbol("", "") is None


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_parses(mock_cls):
    text = (
        'v_hint="us~aapl.oq~Apple Inc.~AAPL~GP'
        '^hk~00700~腾讯控股~腾讯~GP'
        '^sh~600519~贵州茅台~茅台~GP"'
    )
    mock_cls.return_value = _mock_client(text)
    out = TencentProvider().search_instruments("a", limit=20)
    by_sym = {r["symbol"]: r for r in out}
    assert "AAPL.US" in by_sym
    assert "00700.HK" in by_sym
    assert "600519.SH" in by_sym
    # asset_type 按市场区分
    assert by_sym["AAPL.US"]["asset_type"] == "stock_us"
    assert by_sym["00700.HK"]["asset_type"] == "stock_hk"
    assert by_sym["600519.SH"]["asset_type"] == "stock"
    # code 为去后缀纯代码
    assert by_sym["AAPL.US"]["code"] == "AAPL"


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_empty_query(mock_cls):
    assert TencentProvider().search_instruments("", limit=10) == []


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_empty_on_error(mock_cls):
    mock_cls.side_effect = RuntimeError("network down")
    assert TencentProvider().search_instruments("茅台", limit=10) == []


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_dedups_same_symbol(mock_cls):
    """smartbox 可能返回同一股票的多个条目 (模糊匹配), 应按 symbol 去重。"""
    text = (
        'v_hint="hk~00700~腾讯控股~腾讯~GP'
        '^hk~00700~腾讯控股(备)~腾讯~GP'  # 同 symbol, 应去重
        '^sh~600519~贵州茅台~茅台~GP"'
    )
    mock_cls.return_value = _mock_client(text)
    out = TencentProvider().search_instruments("腾讯", limit=20)
    symbols = [r["symbol"] for r in out]
    assert symbols.count("00700.HK") == 1  # 去重
    assert "600519.SH" in symbols


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_chinese_name_decoded(mock_cls):
    """smartbox 返回的中文名称应正确解码为汉字而非 \\uXXXX 转义。"""
    text = 'v_hint="hk~00700~腾讯控股~腾讯~GP"'
    mock_cls.return_value = _mock_client(text)
    out = TencentProvider().search_instruments("腾讯", limit=10)
    assert len(out) == 1
    assert out[0]["name"] == "腾讯控股"  # 不是 \u817e\u8baf...


@patch("app.data_providers.tencent_provider.httpx.Client")
def test_search_instruments_unicode_escape_decoded(mock_cls):
    """smartbox 实际返回字面量 \\uXXXX (如 \\u817e\\u8baf), 需 json.loads 解码为中文。"""
    # 模拟真实 smartbox 响应: 名称字段是 ASCII 的 \u 转义序列
    text = 'v_hint="hk~00700~\\u817e\\u8baf\\u63a7\\u80a1~txkg~GP^sh~000847~\\u817e\\u8baf\\u6d4e\\u5b89~txja~ZS"'
    mock_cls.return_value = _mock_client(text)
    out = TencentProvider().search_instruments("腾讯", limit=10)
    by_sym = {r["symbol"]: r["name"] for r in out}
    assert by_sym["00700.HK"] == "腾讯控股"
    assert by_sym["000847.SH"] == "腾讯济安"
