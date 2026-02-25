from __future__ import annotations

import logging
import os

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    host = (os.getenv("APP_HOST") or "0.0.0.0").strip()
    port = int((os.getenv("APP_PORT") or "8001").strip())
    uvicorn.run(
        "app.api.fastapi_app:app",
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
