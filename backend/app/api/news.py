"""个股新闻/公告 API — 东方财富数据源（内存缓存）。

路由前缀: /api/insights
端点:
  GET /news?symbol=&name=&limit=   个股相关新闻(东财资讯) + 公告(东财公告)

数据来源(参照 PanWatch news_collector, 本项目不依赖其数据库模型):
  - 个股新闻: search-api-web.eastmoney.com 搜索接口(按股票名称搜索效果最好)
  - 个股公告: np-anotice-stock.eastmoney.com 批量接口(按 6 位代码)
两项均直连 CN 源(绕过 env 代理 + 关闭证书校验), 结果 5 分钟内存缓存。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

# 关掉 httpx 在 verify=False 时的 InsecureRequestWarning 刷屏
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # noqa: BLE001
    pass

router = APIRouter(prefix="/api/insights", tags=["insights"])

_SOURCE_LABELS = {
    "eastmoney_news": "东财资讯",
    "eastmoney": "东财公告",
}

_cache: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(minutes=5)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://so.eastmoney.com/",
    "Accept": "*/*",
}


def _cache_get(key: str) -> list | None:
    if key in _cache:
        ts, data = _cache[key]
        if datetime.now() - ts < _CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: list) -> None:
    _cache[key] = (datetime.now(), data)


def _clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _parse_em_time(s: str) -> datetime:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if " " in s else s[:10], fmt)
        except (ValueError, TypeError):
            continue
    return datetime.now()


def _importance_news(title: str) -> int:
    if any(k in title for k in ["重磅", "突发", "紧急", "重大", "独家"]):
        return 2
    if any(k in title for k in ["快讯", "消息", "公告", "研报"]):
        return 1
    return 0


def _importance_ann(title: str, item: dict) -> int:
    if any(k in title for k in ["重大", "业绩预告", "业绩快报", "年报", "半年报"]):
        return 3
    if any(k in title for k in ["季报", "分红", "增持", "减持"]):
        return 2
    cols = [c.get("column_name", "") for c in (item.get("columns") or [])]
    if "临时" in " ".join(cols):
        return 1
    return 0


async def _fetch_stock_news(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    """东方财富搜索接口(个股新闻, 按名称搜索)。"""
    if not keyword:
        return []
    param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": 15,
                    "preTag": "",
                    "postTag": "",
                }
            },
    }
    params = {"cb": "jQuery", "param": json.dumps(param, separators=(",", ":"))}
    try:
        resp = await client.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params=params,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        text = resp.text
        if text.startswith("jQuery(") and text.endswith(")"):
            data = json.loads(text[7:-1])
        else:
            return []
        if data.get("code") != 0:
            return []
        items = data.get("result", {}).get("cmsArticleWebOld", [])
        out: list[dict] = []
        for it in items:
            ext_id = str(it.get("code", ""))
            if not ext_id:
                continue
            title = _clean_html(it.get("title", ""))
            if not title:
                continue
            pub = _parse_em_time(it.get("date", ""))
            out.append({
                "source": "eastmoney_news",
                "external_id": ext_id,
                "title": title,
                "content": _clean_html(it.get("content", "")),
                "publish_time": pub.strftime("%Y-%m-%d %H:%M"),
                "symbols": [],
                "importance": _importance_news(title),
                "url": it.get("url") or f"https://finance.eastmoney.com/a/{ext_id}.html",
            })
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("东财资讯采集失败 (%s): %s", keyword, e)
        return []


async def _fetch_announcements(client: httpx.AsyncClient, codes: list[str]) -> list[dict]:
    """东方财富公告(批量, 按 6 位代码)。"""
    if not codes:
        return []
    params = {
        "sr": -1,
        "page_size": 50,
        "page_index": 1,
        "ann_type": "A",
        "stock_list": ",".join(codes),
        "f_node": 0,
        "s_node": 0,
    }
    try:
        resp = await client.get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params=params,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return []
        items = data.get("data", {}).get("list", [])
        out: list[dict] = []
        for it in items:
            ext_id = str(it.get("art_code", ""))
            if not ext_id:
                continue
            title = it.get("title", "")
            if not title:
                continue
            pub = _parse_em_time(it.get("notice_date", ""))
            sym_codes = [c.get("stock_code", "") for c in (it.get("codes") or []) if c.get("stock_code")]
            out.append({
                "source": "eastmoney",
                "external_id": ext_id,
                "title": title,
                "content": "",
                "publish_time": pub.strftime("%Y-%m-%d %H:%M"),
                "symbols": sym_codes or codes[:1],
                "importance": _importance_ann(title, it),
                "url": f"https://data.eastmoney.com/notices/detail/{codes[0]}/{ext_id}.html",
            })
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("东财公告采集失败: %s", e)
        return []


@router.get("/news")
async def get_news(
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
    name: str = Query(default="", description="股票名称(优先用于搜索, 比代码更准)"),
    limit: int = Query(default=20, ge=1, le=100, description="返回条数"),
):
    """获取个股相关新闻 + 公告(东方财富, 5 分钟内存缓存)。

    组合: 按名称搜索的个股新闻(东财资讯) + 按代码批量拉取的公告(东财公告),
    去重后按时间倒序返回。无数据时返回空列表(不报错), 前端自行展示空态。
    """
    m = re.match(r"(\d{6})", symbol)
    code = m.group(1) if m else symbol
    keywords = [name] if name else ([code] if code else [])

    cache_key = f"news:{symbol}:{name}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"items": cached}

    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(timeout=8, verify=False, headers=_HEADERS, trust_env=False) as client:
        async def _news(kw: str) -> list[dict]:
            async with sem:
                return await _fetch_stock_news(client, kw)
        news_tasks = [_news(kw) for kw in keywords]
        async with sem:
            ann = await _fetch_announcements(client, [code]) if code else []
        results = await asyncio.gather(*news_tasks, return_exceptions=True)

    items: list[dict] = []
    for r in results:
        if isinstance(r, list):
            items.extend(r)
    items.extend(ann)

    # 去重(同 source + external_id) + 按时间倒序
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for it in items:
        k = (it["source"], it["external_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    uniq.sort(key=lambda x: x["publish_time"], reverse=True)
    uniq = uniq[:limit]

    _cache_set(cache_key, uniq)
    return {"items": uniq}
