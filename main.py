from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import auth, whatsapp, instagram, knowledge, tunzaa_webhook, accounts, products, orders, pool, stats
from app.core.audit_middleware import SupportAuditMiddleware
from app.core.config import settings

from fastapi.staticfiles import StaticFiles
import os

# Ensure the upload directory exists
os.makedirs("static/uploads/products", exist_ok=True)

app = FastAPI(
    title=settings.PROJECT_NAME, openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS Middleware
# Credentialed requests require an explicit origin allowlist per the CORS spec
# (browsers reject "allow-origin: *" combined with "allow-credentials: true"),
# so only enable credentials when explicit origins are configured.
_cors_origins = [
    str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if _cors_origins else ["*"],
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Support-impersonation audit (#75): one WRITE row per mutating API call
# made with an impersonated token. Never breaks requests.
app.add_middleware(SupportAuditMiddleware)


app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(
    whatsapp.router, prefix=f"{settings.API_V1_STR}/whatsapp", tags=["whatsapp"]
)
app.include_router(
    instagram.router, prefix=f"{settings.API_V1_STR}/instagram", tags=["instagram"]
)
app.include_router(
    knowledge.router, prefix=f"{settings.API_V1_STR}/knowledge", tags=["knowledge"]
)
app.include_router(
    tunzaa_webhook.router, prefix=f"{settings.API_V1_STR}/tunzaa", tags=["tunzaa"]
)
app.include_router(
    accounts.router, prefix=f"{settings.API_V1_STR}/accounts", tags=["accounts"]
)
app.include_router(
    products.router, prefix=f"{settings.API_V1_STR}/products", tags=["products"]
)
app.include_router(
    orders.router, prefix=f"{settings.API_V1_STR}/orders", tags=["orders"]
)
app.include_router(
    pool.router, prefix=f"{settings.API_V1_STR}/pool", tags=["pool"]
)
app.include_router(
    stats.router, prefix=f"{settings.API_V1_STR}/stats", tags=["stats"]
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
