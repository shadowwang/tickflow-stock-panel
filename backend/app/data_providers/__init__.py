"""Market data provider abstraction.

Providers normalize external data sources into the internal parquet schema.
"""
from app.data_providers.base import AssetType, MarketDataProvider, ProviderCapabilities
from app.data_providers.registry import get_provider

__all__ = ["AssetType", "MarketDataProvider", "ProviderCapabilities", "get_provider", "resolve_provider"]


def resolve_provider(name: str | None):
    """统一分发层: 优先内置 registry (tickflow/tencent), 否则 custom 插件, 否则 None。

    让内置非 tickflow 源 (如 tencent) 与 custom 插件走同一入口, 各 dispatch 点不再
    硬编码 `custom_sources.get_provider`。
    """
    if not name:
        return None
    name = str(name).lower()
    # 1) 内置 registry
    try:
        return get_provider(name)
    except Exception:  # noqa: BLE001
        pass
    # 2) custom 插件
    try:
        from app.data_providers import custom as custom_sources
        if custom_sources.is_custom_provider(name):
            return custom_sources.get_provider(name)
    except Exception:  # noqa: BLE001
        pass
    return None
