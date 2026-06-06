"""linling-tools-stdlib — part of linling.

Importing this module auto-registers all standard tools into the global
registry. Just do ``import linling_tools_stdlib`` to activate.

The stdlib is designed to override core tool names where semantics
differ (e.g. ``替换`` — core's ``replace_str`` vs stdlib's QRDic-style
``replace_sep``). We explicitly import :mod:`linling_core.tools_builtin`
first so the core tools register, and *then* our own decorators run —
making the import order deterministic regardless of who imports what
first.
"""

import linling_core.tools_builtin

from linling_tools_stdlib import (
    adapter_rpc,
    codec,
    fishing_game,
    fishing_image,
    format_ops,
    gacha_image,
    globals_ops,
    image_text,
    json_ops,
    legacy_stubs,
    random_ops,
    scheduler_ops,
    str_ops,
    trade_ops,
)
from linling_tools_stdlib.version import __version__

__all__ = [
    "__version__",
    "adapter_rpc",
    "codec",
    "fishing_game",
    "fishing_image",
    "format_ops",
    "gacha_image",
    "globals_ops",
    "image_text",
    "json_ops",
    "legacy_stubs",
    "random_ops",
    "scheduler_ops",
    "str_ops",
    "trade_ops",
]
