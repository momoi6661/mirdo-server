"""外出 GM Agent 的公共入口。

实现复用统一的 PydanticAI/OpenAI-compatible runtime，但通过独立模块名明确：
外出叙事不使用 Mirdo 的人格 Agent。
"""

from .mirdo_agent import build_expedition_agent

__all__ = ["build_expedition_agent"]
