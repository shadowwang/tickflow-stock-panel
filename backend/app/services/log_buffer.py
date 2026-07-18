"""内存日志环形缓冲 —— 供前端"日志"弹窗实时查看服务端运行日志。

设计要点:
- 用 deque(maxlen=N) 做进程内环形缓冲 (重启清空, 这是 live tail 工具的标准行为)。
- 注册到 root logger 与 uvicorn 日志器, 捕获应用/调度/uvicorn 的全部记录。
- snapshot(offset, limit) 按"最新优先"分页返回, 与前端"加载更多"一致。
- 线程安全: emit 与 snapshot 均在锁内访问 deque。
- 容量默认 2000: 排查问题时能看到足够上下文, 又不会无限增长占内存。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class LogEntry:
    """单条日志记录, 序列化后直接喂给前端表格。"""
    ts: str       # 本地时区, 形如 "2026-07-17 00:00:12" (与控制台基本一致)
    level: str    # DEBUG/INFO/WARNING/ERROR/CRITICAL
    logger: str   # logger 名 (如 "uvicorn.access", "app.services.financial_sync")
    path: str     # 链路: "module:lineno" 或 "–" (无来源信息时)
    message: str  # 已格式化消息; 异常时附 traceback


class _RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._buf: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
            level = record.levelname
            logger_name = record.name
            # 链路 = module:lineno: 给排查时一个"在哪一行打出来的"线索。
            # uvicorn 等没有业务模块名的, 留 "–"。
            module = getattr(record, "module", "") or ""
            lineno = getattr(record, "lineno", 0) or 0
            path = f"{module}:{lineno}" if module else "–"
            msg = record.getMessage()
            # 异常: 把 traceback 拼到消息后面, 前端能直接看到堆栈。
            if record.exc_info:
                msg = msg + "\n" + "".join(traceback.format_exception(*record.exc_info))
            entry = LogEntry(ts=ts, level=level, logger=logger_name, path=path, message=msg)
            with self._lock:
                self._buf.append(entry)
        except Exception:  # noqa: BLE001
            # 日志缓冲自身绝不能因异常把进程崩了 —— 静默吞掉即可。
            self.handleError(record)

    def snapshot(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int, bool]:
        """返回 (entries_newest_first, total, has_more)。

        offset=0 表示从最新一条开始; offset 越大越往旧。limit 为本页大小。
        """
        with self._lock:
            total = len(self._buf)
            # newest-first: 倒序切片
            data = list(self._buf)[::-1]
        sliced = data[offset: offset + limit]
        return [asdict(e) for e in sliced], total, (offset + len(sliced)) < total

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


class LogBuffer:
    """对外门面, 持有唯一的 Handler 实例, 供 lifespan / API 复用。"""

    def __init__(self, capacity: int = 2000) -> None:
        self.handler = _RingBufferHandler(capacity)

    def snapshot(self, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        entries, total, has_more = self.handler.snapshot(offset, limit)
        return {"entries": entries, "total": total, "has_more": has_more}

    def clear(self) -> int:
        """清空缓冲, 返回清空前的条数 (供前端确认)。"""
        before = len(self.handler._buf)  # noqa: SLF001
        self.handler.clear()
        return before


# 全局单例 —— 由 main.py lifespan 注册到 root + uvicorn 日志器。
log_buffer = LogBuffer(capacity=2000)
