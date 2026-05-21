import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api import auth, avatar, google_oauth, users, services, staff, availability, admin, locations, telegram
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Telegram long-polling when no webhook URL is configured
    polling_task = None
    if settings.TELEGRAM_BOT_TOKEN and not settings.TELEGRAM_WEBHOOK_URL:
        polling_task = asyncio.create_task(telegram.run_polling())

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Appointment Booking API", lifespan=lifespan)

uploads_dir = Path(__file__).resolve().parent.parent / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(avatar.router)
app.include_router(google_oauth.router)
app.include_router(users.router, prefix="/api", tags=["users"])
app.include_router(services.router, prefix="/api/services")
app.include_router(staff.router, prefix="/api/staff")
app.include_router(availability.router, prefix="/api/availability")
app.include_router(admin.router)
app.include_router(locations.router)
app.include_router(telegram.router)

if settings.FEATURE_SET == "full":
    from app.api import bookings, payments, notifications, analytics, customers, waitlist, reviews

    app.include_router(bookings.router, prefix="/api/bookings")
    app.include_router(payments.router, prefix="/api/payments")
    app.include_router(notifications.router, prefix="/api/notifications")
    app.include_router(analytics.router, prefix="/api/analytics")
    app.include_router(customers.router)
    app.include_router(waitlist.router, prefix="/api/waitlist")
    app.include_router(reviews.router, prefix="/api/reviews")


@app.get("/health")
def health():
    return {"status": "ok"}
