import asyncio
from contextlib import asynccontextmanager, suppress

import logging

from fastapi import FastAPI

from app.ai.tts import cleanup_expired_media
from app.routes.routes import router
from app.routes.webhook import router as webhook_router

logging.getLogger("app").setLevel(logging.INFO)


async def _media_cleanup_loop() -> None:
    """Remove expired generated replies while this app process is running."""
    while True:
        await cleanup_expired_media()
        await asyncio.sleep(60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_media_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(webhook_router)


@app.get("/")
def read_root():
    return {"message": "Welcome to the API!"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


