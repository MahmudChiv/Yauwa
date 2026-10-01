"""Credential-free application status routes."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def read_root():
    return {"message": "Welcome to the API!"}


@router.get("/health")
def health_check():
    return {"status": "healthy"}
