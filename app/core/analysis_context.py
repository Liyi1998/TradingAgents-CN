"""分析任务级上下文，用于把 LLM 用量精确归属到 task_id。"""

from contextvars import ContextVar


analysis_task_id_var: ContextVar[str] = ContextVar(
    "analysis_task_id", default=""
)
