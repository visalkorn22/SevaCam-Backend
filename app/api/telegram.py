# backend/app/api/telegram.py
import uuid
import asyncio
import httpx
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.database import get_db
from app.core.auth import get_current_user
from app.core.config import settings

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

ALGORITHM = "HS256"
CONNECT_TOKEN_MINUTES = 15


def _make_connect_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=CONNECT_TOKEN_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.SECRET_KEY, algorithm=ALGORITHM)


def _decode_connect_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired connect token")


def _process_start_command(token: str, chat_id: int, db: Session) -> None:
    """Link a Telegram chat_id to a user account via a /start token."""
    try:
        user_id = _decode_connect_token(token)
    except HTTPException:
        return  # silently ignore bad tokens

    existing = db.execute(
        text("SELECT id FROM telegram_connections WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    if not existing:
        db.execute(
            text("""
            INSERT INTO telegram_connections (id, user_id, chat_id)
            VALUES (:id, :uid, :chat_id)
            ON CONFLICT (chat_id) DO UPDATE SET user_id = EXCLUDED.user_id
            """),
            {"id": str(uuid.uuid4()), "uid": user_id, "chat_id": chat_id},
        )
        db.commit()

    _send_telegram_message(
        chat_id,
        "✅ Connected! You can now receive location cards from the booking system.",
    )


def _handle_update(body: dict, db: Session) -> None:
    """Process a single Telegram update dict (shared by webhook and polling)."""
    message = body.get("message", {})
    text_msg: str = message.get("text", "")
    chat_id: int = message.get("chat", {}).get("id")

    if not chat_id:
        return

    if text_msg.startswith("/start"):
        parts = text_msg.split(" ", 1)
        if len(parts) >= 2:
            _process_start_command(parts[1].strip(), chat_id, db)


@router.get("/status")
def telegram_status(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("SELECT id FROM telegram_connections WHERE user_id = :uid"),
        {"uid": current_user["id"]},
    ).fetchone()
    return {"connected": row is not None}


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    """Receives updates from Telegram when a webhook URL is registered."""
    body = await request.json()
    _handle_update(body, db)
    return {"ok": True}


@router.post("/send-location")
def send_location(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a native Telegram location card + booking summary to the user's chat."""
    booking_id: str = payload.get("booking_id", "")
    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id required")

    # Check connection
    conn_row = db.execute(
        text("SELECT chat_id FROM telegram_connections WHERE user_id = :uid"),
        {"uid": current_user["id"]},
    ).fetchone()

    if not conn_row:
        connect_token = _make_connect_token(current_user["id"])
        return {
            "connected": False,
            "connect_token": connect_token,
            "bot_username": settings.TELEGRAM_BOT_USERNAME,
        }

    chat_id = conn_row._mapping["chat_id"]

    # Fetch booking + location
    booking = db.execute(
        text("""
        SELECT b.start_time_utc, b.customer_timezone,
               s.name AS service_name,
               l.name AS location_name, l.address, l.latitude, l.longitude
        FROM bookings b
        JOIN services s ON s.id = b.service_id
        LEFT JOIN locations l ON l.id = b.location_id
        WHERE b.id = :bid AND b.customer_id = (
            SELECT id FROM customers WHERE user_id = :uid LIMIT 1
        )
        """),
        {"bid": booking_id, "uid": current_user["id"]},
    ).fetchone()

    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    bm = booking._mapping
    if bm["latitude"] is None or bm["longitude"] is None:
        raise HTTPException(status_code=422, detail="This service has no location set")

    # Send native location card
    _send_telegram_location(chat_id, float(bm["latitude"]), float(bm["longitude"]))

    # Send text summary
    start_fmt = bm["start_time_utc"].strftime("%d %b %Y, %I:%M %p")
    msg = (
        f"📍 *{bm['location_name']}*\n"
        f"{bm['address']}\n\n"
        f"🗓 *{bm['service_name']}*\n"
        f"📅 {start_fmt} ({bm['customer_timezone']})"
    )
    _send_telegram_message(chat_id, msg, parse_mode="Markdown")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Long-polling task — used automatically when TELEGRAM_WEBHOOK_URL is not set
# ---------------------------------------------------------------------------

async def run_polling() -> None:
    """
    Async long-polling loop. Pulls updates from Telegram and processes them.
    Runs as a background task when no webhook URL is configured (e.g. localhost dev).
    """
    from app.core.database import SessionLocal  # avoid circular import at module level

    offset = 0
    print("[telegram] Starting long-polling (no webhook URL configured)")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                    timeout=40,
                )
                updates = resp.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    db = SessionLocal()
                    try:
                        _handle_update(update, db)
                    finally:
                        db.close()
            except asyncio.CancelledError:
                print("[telegram] Polling stopped")
                return
            except Exception as exc:
                print(f"[telegram] Polling error: {exc}")
                await asyncio.sleep(5)


def _send_telegram_location(chat_id: int, latitude: float, longitude: float) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendLocation"
    httpx.post(url, json={"chat_id": chat_id, "latitude": latitude, "longitude": longitude}, timeout=10)


def _send_telegram_message(chat_id: int, text_msg: str, parse_mode: str = "") -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    body: dict = {"chat_id": chat_id, "text": text_msg}
    if parse_mode:
        body["parse_mode"] = parse_mode
    httpx.post(url, json=body, timeout=10)
