"""服务入口：`uvicorn app.main:app` 或 `python -m app.main`。"""

from __future__ import annotations

from app.api import create_app
from app.config import Settings

app = create_app(Settings.from_env())


if __name__ == "__main__":
    import uvicorn

    settings = Settings.from_env()
    # 直接传 app 对象：传导入字符串会以 app.main 再导入一次，创建第二个应用实例
    uvicorn.run(app, host=settings.host, port=settings.port)
