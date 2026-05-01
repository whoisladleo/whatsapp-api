from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import auth, whatsapp
from app.core.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME, openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        [str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS]
        if settings.BACKEND_CORS_ORIGINS
        else ["*"]
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(
    whatsapp.router, prefix=f"{settings.API_V1_STR}/whatsapp", tags=["whatsapp"]
)


@app.get("/")
def root():
    return {"message": "WhatsApp Medium infrastructure is ready"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
