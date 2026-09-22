from fastapi import FastAPI
from app.routes.routes import router
from app.routes.webhook import router as webhook_router

app = FastAPI()

app.include_router(router)
app.include_router(webhook_router)

@app.get("/")
def read_root():
    return {"message": "Welcome to the API!"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}


