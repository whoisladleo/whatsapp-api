import os
from celery import Celery

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

celery_app = Celery(
    "taskflow",
    broker=broker_url,
    backend=result_backend,
    include=[
        "app.tasks.order_reminders",
        "app.tasks.handoff_reminders",
        "app.tasks.webhook_processing",
        "app.tasks.instagram_comment_processing",
        "app.tasks.instagram_dm_processing",
        "app.tasks.location_capture",
        "app.tasks.quote_nudges",
        "app.tasks.number_billing",
        "app.tasks.product_subscription_billing",
    ]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
