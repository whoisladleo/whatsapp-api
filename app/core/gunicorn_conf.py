import multiprocessing
import os

# Gunicorn configuration
port = os.getenv("PORT", "8000")
bind = f"0.0.0.0:{port}"
workers_per_core = float(os.getenv("WORKERS_PER_CORE", "1"))
max_workers = int(os.getenv("MAX_WORKERS", "4"))
use_workers = int(workers_per_core * multiprocessing.cpu_count())
workers = max(min(use_workers, max_workers), 2)

worker_class = "uvicorn.workers.UvicornWorker"
keepalive = 120
errorlog = "-"
accesslog = "-"
loglevel = "info"
timeout = os.getenv("TIMEOUT", "120")
graceful_timeout = os.getenv("GRACEFUL_TIMEOUT", "120")
