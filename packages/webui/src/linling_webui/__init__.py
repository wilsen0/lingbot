"""linling-webui — 狐妖情缘主题 Web UI 后端。

提供 FastAPI app factory `create_app(config)`，可由 `linling serve webui`
或独立进程挂起。前端 SPA 产物位于 `linling_webui/static/`，生产环境随
wheel 一起发行；开发环境通过 Vite 代理。
"""

from linling_webui.version import __version__

__all__ = ["__version__"]
