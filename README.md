# Appointment Booking API

FastAPI backend for the appointment booking system.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

3. Run the server:

```bash
uvicorn app.main:app --reload
```

## API Documentation

Visit `http://localhost:8000/docs` for interactive API documentation (Swagger UI).

## Schema Profiles (Core vs Full)

- **Core** (`FEATURE_SET=core`): Auth, users, services, staff, availability, locations, audit logs.
  Uses `scripts/001_create_tables_core.sql`.
- **Full** (`FEATURE_SET=full`): Everything above + bookings, payments, notifications, analytics, reviews, customers, etc.
  Uses `scripts/001_create_tables.sql`.

If you switch profiles, drop the DB volume first:

```
docker compose down -v
```

## Endpoints

### Services

- `GET /api/services` - Get all services
- `GET /api/services/{service_id}` - Get service by ID
- `POST /api/services` - Create service (Admin)
- `PUT /api/services/{service_id}` - Update service (Admin)
- `DELETE /api/services/{service_id}` - Delete service (Admin)

### Staff

- `POST /api/staff/services` - Assign staff to service
- `GET /api/staff/services/{staff_id}` - Get staff services
- `DELETE /api/staff/services/{assignment_id}` - Remove staff from service
- `GET /api/staff/{service_id}/staff` - Get service staff

### Availability

- `POST /api/availability/rules` - Create availability rule
- `GET /api/availability/rules/{staff_id}` - Get staff availability rules
- `DELETE /api/availability/rules/{rule_id}` - Delete rule
- `POST /api/availability/exceptions` - Create availability exception
- `GET /api/availability/exceptions/{staff_id}` - Get staff exceptions
- `GET /api/availability/slots` - Get available time slots

### Bookings

- `POST /api/bookings` - Create booking
- `GET /api/bookings` - Get bookings (with filters)
- `GET /api/bookings/{booking_id}` - Get booking by ID
- `PUT /api/bookings/{booking_id}` - Update booking
- `DELETE /api/bookings/{booking_id}` - Cancel booking

### Payments

- `POST /api/payments/create-intent` - Create payment intent
- `POST /api/payments/{payment_id}/confirm` - Confirm payment
- `GET /api/payments/{payment_id}` - Get payment
- `GET /api/payments/booking/{booking_id}` - Get booking payments
- `POST /api/payments/{payment_id}/refund` - Refund payment

### Analytics

- `GET /api/analytics/bookings/stats` - Get booking statistics
- `GET /api/analytics/services/stats` - Get service statistics
- `GET /api/analytics/staff/stats` - Get staff statistics
- `GET /api/analytics/daily/stats` - Get daily statistics

## Mock Integrations

- **ABA Payway**: Payment processing is mocked. Real integration would require actual API credentials.
- **Email/SMS**: Notifications are logged to database. Real integration would require email/SMS provider.

# Image Moderation (Optional)

Service image uploads can be moderated via `IMAGE_MODERATION_PROVIDER`:

```
IMAGE_MODERATION_ENABLED=true
IMAGE_MODERATION_PROVIDER=webhook
```

## Webhook Provider

```
IMAGE_MODERATION_PROVIDER=webhook
IMAGE_MODERATION_WEBHOOK_URL=https://your-moderation-endpoint
```

The webhook should accept a multipart `file` upload and return JSON:

```
{"allowed": true/false, "reason": "...", "categories": ["sexual", "violence"]}
```

## Google Vision SafeSearch

```
IMAGE_MODERATION_PROVIDER=google
IMAGE_MODERATION_GOOGLE_THRESHOLD=LIKELY
IMAGE_MODERATION_GOOGLE_BLOCK_CATEGORIES=adult,violence,racy
```

Requires `GOOGLE_APPLICATION_CREDENTIALS` to point at a Google service account JSON file.

## AWS Rekognition

```
IMAGE_MODERATION_PROVIDER=aws
IMAGE_MODERATION_AWS_MIN_CONFIDENCE=70
IMAGE_MODERATION_AWS_BLOCK_LABELS=Explicit Nudity,Violence,Visually Disturbing
IMAGE_MODERATION_AWS_REGION=ap-southeast-1
```

Requires standard AWS credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) and optionally `AWS_REGION`.

## Azure Content Safety

```
IMAGE_MODERATION_PROVIDER=azure
IMAGE_MODERATION_AZURE_SEVERITY_THRESHOLD=4
IMAGE_MODERATION_AZURE_CATEGORIES=Sexual,Violence
AZURE_CONTENT_SAFETY_ENDPOINT=https://<resource-name>.cognitiveservices.azure.com
AZURE_CONTENT_SAFETY_KEY=your-key
```

If moderation blocks the upload, the API returns a 400 with the reason.

# Passwordless Email Login

The API supports passwordless login via magic link (SMTP required):

- `POST /api/auth/magic-link/request` - Send login link to email
- `POST /api/auth/magic-link/confirm` - Exchange token for a session

Ensure `APP_URL` and SMTP settings are configured in `backend/.env`.

# How to run for backend

docker compose up --build

docker compose down -v

# Re-run migrations and seed roles/permissions (no demo users):

docker compose up -d

docker compose exec backend python -m app.seed

docker compose exec backend python -m app.seed_admin_user

docker compose restart

# Bootstrap an initial admin/superadmin (one-time):

docker compose exec backend python -m app.bootstrap_admin --email you@example.com --password "ChangeMe123!" --role superadmin --full-name "Initial Admin"

# Remove legacy seeded users by email (if they exist):

docker compose exec backend python -m app.purge_seeded_users --confirm

# How to run for frontend

& C:/Personal/Y4T1/Internship/Dev/.venv/Scripts/Activate.ps1

npm install

npm run build

npm run dev

# refresh database

docker compose restart

# Webhook testing stripe

stripe listen --forward-to localhost:8000/api/payments/webhook/stripe
