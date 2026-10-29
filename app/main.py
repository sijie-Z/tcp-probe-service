"""服务入口：`uvicorn app.main:app` 或 `python -m app.main`。"""

from __future__ import annotations

from app.api import create_app
from app.config import Settings

app = create_app(Settings.from_env())


if __name__ == "__main__":
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
