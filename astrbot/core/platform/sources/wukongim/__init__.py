"""WuKongIM (悟空IM) Platform adapter package.

Importing this package registers the WuKongIM platform adapter with AstrBot.
"""

from .wukongim_adapter import WuKongIMPlatformAdapter  # noqa: F401
from .wukongim_event import WuKongIMMessageEvent  # noqa: F401

__all__ = ["WuKongIMPlatformAdapter", "WuKongIMMessageEvent"]
