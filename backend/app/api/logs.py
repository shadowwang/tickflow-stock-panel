"""日志查看器 API —— 给前端"日志"弹窗用, 读取内存环形缓冲。

无权限门控: 弹窗是诊断工具, 用户点开就应看到; 与 capabilities 解耦。
生产环境如需限制, 在网关层加 IP 白名单或鉴权中间件 (与 settings 等接口同等待遇)。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.log_buffer import log_buffer

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def get_logs(
    offset: int = Query(0, ge=0, description="从最新端开始的偏移, 0=最新一页"),
    limit: int = Query(200, ge=1, le=500, description="本页条数, 上限 500"),
):
    """返回最新优先的日志分页。"""
    return log_buffer.snapshot(offset=offset, limit=limit)


@router.post("/clear")
def clear_logs():
    """清空缓冲 (例如排查完后重置, 重新观察新一轮日志)。"""
    removed = log_buffer.clear()
    return {"ok": True, "removed": removed}
