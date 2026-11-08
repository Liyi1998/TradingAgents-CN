"""新浪财经 A 股免费数据源。"""

from .adapter import SinaCNAdapter
from .provider import SinaCNProvider

__all__ = ["SinaCNAdapter", "SinaCNProvider"]
