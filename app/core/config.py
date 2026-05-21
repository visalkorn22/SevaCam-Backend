from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from typing import Optional, List

class Settings(BaseSettings):
    # Allow extra env vars like ENV/DEBUG without crashing
    model_config = ConfigDict(env_file=".env", extra="ignore")

    # =========================
    # Application
    # =========================
    ENV: str = "development"
    DEBUG: bool = False
    APP_URL: str = "http://localhost:3000"
    FEATURE_SET: str = "core"

    # =========================
    # Database (REQUIRED)
    # =========================
    DATABASE_URL: str

    # =========================
    # CORS (comma-separated)
    # =========================
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # =========================
    # Auth / Security
    # =========================
    SECRET_KEY: str = "dev-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    MAGIC_LINK_TOKEN_MINUTES: int = 15

    # =========================
    # Supabase (disabled / optional)
    # =========================
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = None
    SUPABASE_SERVICE_ROLE_KEY: Optional[str] = None

    # =========================
    # Google OAuth 2.0
    # =========================
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    FRONTEND_URL: str = "http://localhost:3000"

    # =========================|
    # Session lifetime
    # =========================
    SESSION_DAYS: int = 30

    # =========================
    # Cookie security (env-driven per deployment topology)
    # =========================
    COOKIE_NAME: str = "auth_token"
    COOKIE_SECURE: bool = False       # Set True in production
    COOKIE_SAMESITE: str = "lax"      # "none" for cross-domain topology
    COOKIE_DOMAIN: Optional[str] = None  # ".yourdomain.com" for subdomain topology
    COOKIE_PATH: str = "/"

    # =========================
    # ABA Payway
    # =========================
    ABA_PAYWAY_MERCHANT_ID: str = "mock_merchant_id"
    ABA_PAYWAY_API_KEY: str = "mock_api_key"
    ABA_PAYWAY_PUBLIC_KEY: Optional[str] = None
    ABA_PAYWAY_API_URL: str = "https://checkout-sandbox.payway.com.kh/api/payment-gateway/v1"
    ABA_PAYWAY_CHECKOUT_PATH: str = "/payments/purchase"
    ABA_PAYWAY_QR_PATH: str = "/payments/generate-qr"
    ABA_PAYWAY_TRANSACTION_DETAIL_PATH: str = "/payments/transaction-detail"
    ABA_PAYWAY_WEBHOOK_PATH: str = "/api/payments/webhook/payway"
    ABA_PAYWAY_WEBHOOK_SECRET: Optional[str] = None
    ABA_PAYWAY_CALLBACK_URL: Optional[str] = None
    ABA_PAYWAY_RETURN_URL: Optional[str] = None
    ABA_PAYWAY_CANCEL_URL: Optional[str] = None
    ABA_PAYWAY_QR_LIFETIME_MINUTES: int = 6
    ABA_PAYWAY_QR_IMAGE_TEMPLATE: str = "template3_color"
    ABA_PAYWAY_TIMEOUT_SECONDS: int = 20
    ABA_PAYWAY_SYNC_GRACE_SECONDS: int = 60

    # =========================
    # Stripe
    # =========================
    STRIPE_API_KEY: Optional[str] = None
    STRIPE_API_URL: str = "https://api.stripe.com/v1"
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    STRIPE_WEBHOOK_PATH: str = "/api/payments/webhook/stripe"
    STRIPE_RETURN_URL: Optional[str] = None
    STRIPE_CANCEL_URL: Optional[str] = None
    STRIPE_TIMEOUT_SECONDS: int = 20

    # =========================
    # Booking Policies
    # =========================
    SLOT_GRANULARITY_MINUTES: int = 15
    MIN_NOTICE_MINUTES: int = 120
    MAX_BOOKING_DAYS: int = 90

    # =========================
    # Email (SMTP)
    # =========================
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: Optional[str] = None

    # =========================
    # Image Moderation
    # =========================
    IMAGE_MODERATION_ENABLED: bool = False
    IMAGE_MODERATION_PROVIDER: str = "webhook"  # webhook | google | aws | azure
    IMAGE_MODERATION_WEBHOOK_URL: Optional[str] = None
    IMAGE_MODERATION_TIMEOUT_SECONDS: int = 10
    IMAGE_MODERATION_FAIL_CLOSED: bool = True
    IMAGE_MODERATION_GOOGLE_THRESHOLD: str = "LIKELY"
    IMAGE_MODERATION_GOOGLE_BLOCK_CATEGORIES: str = "adult,violence,racy"
    IMAGE_MODERATION_AWS_MIN_CONFIDENCE: int = 70
    IMAGE_MODERATION_AWS_BLOCK_LABELS: str = "Explicit Nudity,Violence,Visually Disturbing"
    IMAGE_MODERATION_AWS_REGION: Optional[str] = None
    IMAGE_MODERATION_AZURE_SEVERITY_THRESHOLD: int = 4
    IMAGE_MODERATION_AZURE_CATEGORIES: str = "Sexual,Violence"
    AZURE_CONTENT_SAFETY_ENDPOINT: Optional[str] = None
    AZURE_CONTENT_SAFETY_KEY: Optional[str] = None

    # =========================
    # Bakong KHQR
    # =========================
    KHQR_JWT_TOKEN: Optional[str] = None
    KHQR_CHECK_ENDPOINT: str = "https://api-bakong.nbc.gov.kh/v1/check_transaction_by_md5"
    KHQR_BAKONG_ACCOUNT_ID: str = ""          # e.g. "yourname@yourbank"
    KHQR_ACCOUNT_INFORMATION: str = ""        # phone number or merchant account number
    KHQR_ACQUIRING_BANK: str = ""             # bank name shown in Bakong app
    KHQR_MERCHANT_NAME: str = ""              # max 25 chars
    KHQR_MERCHANT_CITY: str = ""              # max 15 chars
    KHQR_STORE_LABEL: str = ""                # optional, max 25 chars
    KHQR_TERMINAL_LABEL: str = ""             # optional, max 25 chars
    KHQR_MERCHANT_CATEGORY_CODE: int = 5999   # ISO 18245 MCC (5999 = misc retail)
    KHQR_DEFAULT_CURRENCY: str = "USD"        # "USD" or "KHR"
    KHQR_QR_LIFETIME_MINUTES: int = 10        # session TTL shown to customer

    # =========================
    # Telegram Bot
    # =========================
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""   # bot username without @
    TELEGRAM_WEBHOOK_URL: Optional[str] = None

    # =========================
    # Reminder Jobs
    # =========================
    REMINDER_LEAD_MINUTES: int = 60
    REMINDER_WINDOW_MINUTES: int = 5
    REMINDER_CRON_TOKEN: Optional[str] = None

    @property
    def cors_origins_list(self) -> List[str]:
        raw = self.CORS_ORIGINS.strip()
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]

        origins: List[str] = []
        for part in raw.split(","):
            cleaned = part.strip().strip('"').strip("'")
            if cleaned:
                origins.append(cleaned)
        return origins

settings = Settings()
