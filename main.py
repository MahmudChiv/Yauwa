from fastapi import FastAPI
from app.routes.routes import router

app = FastAPI()

app.include_router(router)

@app.get("/")
def read_root():
    return {"message": "Welcome to the API!"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}