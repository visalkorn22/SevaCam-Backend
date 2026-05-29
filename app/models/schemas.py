from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime, date, time
from decimal import Decimal

# Service Schemas
class ServiceBase(BaseModel):
    name: str
    public_name: Optional[str] = None
    internal_name: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    description: Optional[str] = None
    inclusions: Optional[str] = None
    prep_notes: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    duration_minutes: int = 60
    price: Decimal
    deposit_amount: Decimal = Decimal("0")
    buffer_minutes: int = 0
    max_capacity: int = 1
    is_active: bool = True

class ServiceCreate(ServiceBase):
    pass

class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    public_name: Optional[str] = None
    internal_name: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    description: Optional[str] = None
    inclusions: Optional[str] = None
    prep_notes: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    duration_minutes: Optional[int] = None
    price: Optional[Decimal] = None
    deposit_amount: Optional[Decimal] = None
    buffer_minutes: Optional[int] = None
    max_capacity: Optional[int] = None
    is_active: Optional[bool] = None
    paused_from: Optional[datetime] = None
    paused_until: Optional[datetime] = None

class EmbeddedLocation(BaseModel):
    id: str
    name: str
    address: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    timezone: str

class ServiceResponse(ServiceBase):
    id: str
    admin_id: Optional[str]
    created_at: datetime
    is_archived: bool = False
    archived_at: Optional[datetime] = None
    paused_from: Optional[datetime] = None
    paused_until: Optional[datetime] = None
    locations: List["EmbeddedLocation"] = []

    class Config:
        from_attributes = True

# Service Operating Schedule Schemas
class ServiceOperatingScheduleBase(BaseModel):
    timezone: str
    rule_type: str  # daily | weekly | monthly
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: bool = True

class ServiceOperatingScheduleCreate(ServiceOperatingScheduleBase):
    pass

class ServiceOperatingScheduleUpdate(BaseModel):
    timezone: Optional[str] = None
    rule_type: Optional[str] = None
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: Optional[bool] = None

class ServiceOperatingScheduleResponse(ServiceOperatingScheduleBase):
    id: str
    service_id: str
    created_at: datetime

    class Config:
        from_attributes = True

class ServiceOperatingRuleCreate(BaseModel):
    rule_type: str  # weekly | monthly_day | monthly_nth_weekday
    weekday: Optional[int] = None
    month_day: Optional[int] = None
    nth: Optional[int] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None

class ServiceOperatingRuleResponse(ServiceOperatingRuleCreate):
    id: str
    schedule_id: str
    created_at: datetime

    class Config:
        from_attributes = True

class ServiceOperatingExceptionCreate(BaseModel):
    date: date
    is_open: bool = False
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    reason: Optional[str] = None

class ServiceOperatingExceptionResponse(ServiceOperatingExceptionCreate):
    id: str
    service_id: str
    created_at: datetime

    class Config:
        from_attributes = True

# Staff Schemas
class StaffServiceCreate(BaseModel):
    staff_id: str
    service_id: str
    price_override: Optional[Decimal] = None
    deposit_override: Optional[Decimal] = None
    duration_override: Optional[int] = None
    buffer_override: Optional[int] = None
    capacity_override: Optional[int] = None
    is_bookable: Optional[bool] = True
    is_temporarily_unavailable: Optional[bool] = False
    admin_only: Optional[bool] = False
    skills: List[str] = Field(default_factory=list)
    bio: Optional[str] = None


class StaffServiceUpdate(BaseModel):
    price_override: Optional[Decimal] = None
    deposit_override: Optional[Decimal] = None
    duration_override: Optional[int] = None
    buffer_override: Optional[int] = None
    capacity_override: Optional[int] = None
    is_bookable: Optional[bool] = None
    is_temporarily_unavailable: Optional[bool] = None
    admin_only: Optional[bool] = None
    skills: Optional[List[str]] = None
    bio: Optional[str] = None

class StaffServiceResponse(BaseModel):
    id: str
    staff_id: str
    service_id: str
    created_at: datetime
    price_override: Optional[Decimal] = None
    deposit_override: Optional[Decimal] = None
    duration_override: Optional[int] = None
    buffer_override: Optional[int] = None
    capacity_override: Optional[int] = None
    is_bookable: bool = True
    is_temporarily_unavailable: bool = False
    admin_only: bool = False
    skills: List[str] = Field(default_factory=list)
    bio: Optional[str] = None
    average_rating: Optional[float] = None
    review_count: int = 0
    completed_bookings: int = 0
    experience_level: str = "Beginner"

# Location Schemas
class LocationCreate(BaseModel):
    name: str
    timezone: str = "Asia/Phnom_Penh"
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_active: bool = True

class LocationUpdate(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_active: Optional[bool] = None

class LocationResponse(BaseModel):
    id: str
    name: str
    timezone: str
    address: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    is_active: bool
    created_at: datetime

# Availability Schemas
class AvailabilityRuleCreate(BaseModel):
    staff_id: str
    service_id: Optional[str] = None
    day_of_week: int  # 0 = Sunday, 6 = Saturday
    start_time: time
    end_time: time
    timezone: str = "Asia/Phnom_Penh"

class AvailabilityRuleResponse(BaseModel):
    id: str
    staff_id: str
    service_id: Optional[str]
    day_of_week: int
    start_time: time
    end_time: time
    timezone: str
    created_at: datetime

class AvailabilityExceptionCreate(BaseModel):
    staff_id: str
    service_id: Optional[str] = None
    date: date
    is_available: bool = False
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    reason: Optional[str] = None

class AvailabilityExceptionResponse(BaseModel):
    id: str
    staff_id: str
    service_id: Optional[str]
    date: date
    is_available: bool
    start_time: Optional[time]
    end_time: Optional[time]
    reason: Optional[str]
    created_at: datetime

# Weekly Schedule Schemas
class StaffWeeklyScheduleCreate(BaseModel):
    staff_id: str
    timezone: str = "Asia/Phnom_Penh"
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_default: bool = False
    location_id: Optional[str] = None
    max_slots_per_day: Optional[int] = None
    max_bookings_per_day: Optional[int] = None
    max_bookings_per_customer: Optional[int] = None

class StaffWeeklyScheduleResponse(BaseModel):
    id: str
    staff_id: str
    timezone: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    is_default: bool
    location_id: Optional[str]
    max_slots_per_day: Optional[int]
    max_bookings_per_day: Optional[int]
    max_bookings_per_customer: Optional[int]
    created_at: datetime

class StaffWeeklyScheduleUpdate(BaseModel):
    timezone: Optional[str] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_default: Optional[bool] = None
    location_id: Optional[str] = None
    max_slots_per_day: Optional[int] = None
    max_bookings_per_day: Optional[int] = None
    max_bookings_per_customer: Optional[int] = None

class StaffWorkBlockCreate(BaseModel):
    schedule_id: str
    weekday: int  # 0 = Sunday, 6 = Saturday
    start_time_local: time
    end_time_local: time

class StaffWorkBlockResponse(BaseModel):
    id: str
    schedule_id: str
    weekday: int
    start_time_local: time
    end_time_local: time

class StaffBreakBlockCreate(BaseModel):
    schedule_id: str
    weekday: int  # 0 = Sunday, 6 = Saturday
    start_time_local: time
    end_time_local: time

class StaffBreakBlockResponse(BaseModel):
    id: str
    schedule_id: str
    weekday: int
    start_time_local: time
    end_time_local: time

class StaffExceptionCreate(BaseModel):
    staff_id: str
    location_id: Optional[str] = None
    type: str  # time_off | blocked_time | extra_availability | override_day
    start_utc: datetime
    end_utc: datetime
    is_all_day: bool = False
    recurring_rule: Optional[str] = None
    reason: Optional[str] = None

class StaffExceptionBulkCreate(BaseModel):
    staff_ids: Optional[List[str]] = None
    location_id: Optional[str] = None
    type: str  # time_off | blocked_time | extra_availability | override_day
    start_utc: datetime
    end_utc: datetime
    is_all_day: bool = False
    recurring_rule: Optional[str] = None
    reason: Optional[str] = None

class StaffExceptionResponse(BaseModel):
    id: str
    staff_id: str
    location_id: Optional[str]
    type: str
    start_utc: datetime
    end_utc: datetime
    is_all_day: bool
    recurring_rule: Optional[str]
    reason: Optional[str]
    created_by: Optional[str]
    created_at: datetime

class BookingHoldCreate(BaseModel):
    staff_id: str
    service_id: str
    location_id: Optional[str] = None
    start_utc: datetime
    end_utc: datetime
    expires_at_utc: datetime

class BookingHoldResponse(BaseModel):
    id: str
    staff_id: str
    service_id: str
    location_id: Optional[str]
    start_utc: datetime
    end_utc: datetime
    expires_at_utc: datetime
    created_by: Optional[str]
    created_at: datetime

class StaffServiceOverrideCreate(BaseModel):
    staff_id: str
    service_id: str
    price_override: Optional[Decimal] = None
    deposit_override: Optional[Decimal] = None
    duration_override: Optional[int] = None
    buffer_override: Optional[int] = None
    capacity_override: Optional[int] = None
    is_bookable: Optional[bool] = True

class StaffServiceOverrideUpdate(BaseModel):
    price_override: Optional[Decimal] = None
    deposit_override: Optional[Decimal] = None
    duration_override: Optional[int] = None
    buffer_override: Optional[int] = None
    capacity_override: Optional[int] = None
    is_bookable: Optional[bool] = None

class StaffServiceOverrideResponse(BaseModel):
    id: str
    staff_id: str
    service_id: str
    price_override: Optional[Decimal]
    deposit_override: Optional[Decimal]
    duration_override: Optional[int]
    buffer_override: Optional[int]
    capacity_override: Optional[int]
    is_bookable: bool
    created_at: datetime

class AuditLogResponse(BaseModel):
    id: str
    actor_id: Optional[str]
    action: str
    entity_type: str
    entity_id: Optional[str]
    changes: Optional[dict]
    created_at: datetime

# Schedule Change Request Schemas
class ScheduleChangeRequestCreate(BaseModel):
    staff_id: str
    payload: dict
    reason: Optional[str] = None

class ScheduleChangeRequestReview(BaseModel):
    review_note: Optional[str] = None

class ScheduleChangeRequestResponse(BaseModel):
    id: str
    staff_id: str
    requested_by: Optional[str]
    status: str
    payload: dict
    reason: Optional[str]
    review_note: Optional[str]
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    created_at: datetime

class AvailableSlot(BaseModel):
    start_time: datetime
    end_time: datetime
    staff_id: str
    staff_name: Optional[str] = None

# Customer Schemas
class CustomerCreate(BaseModel):
    user_id: Optional[str] = None
    full_name: str
    email: EmailStr
    phone: Optional[str] = None
    timezone: str = "Asia/Phnom_Penh"
    notes: Optional[str] = None

class CustomerResponse(BaseModel):
    id: str
    user_id: Optional[str]
    full_name: str
    email: str
    phone: Optional[str]
    timezone: str
    notes: Optional[str]
    is_blocked: bool
    created_at: datetime

# Booking Schemas
class BookingCreate(BaseModel):
    service_id: str
    staff_id: str
    customer_id: str
    start_time_utc: datetime
    booking_source: str = "web"
    customer_timezone: str = "Asia/Phnom_Penh"
    location_id: Optional[str] = None

class BookingUpdate(BaseModel):
    start_time_utc: Optional[datetime] = None
    status: Optional[str] = None
    payment_status: Optional[str] = None

class BookingResponse(BaseModel):
    id: str
    service_id: str
    staff_id: str
    customer_id: str
    start_time_utc: datetime
    end_time_utc: datetime
    status: str
    payment_status: str
    booking_source: str
    customer_timezone: str
    location_id: Optional[str] = None
    created_at: datetime

class BookingReviewSummary(BaseModel):
    id: str
    rating: int

class BookingWithDetails(BookingResponse):
    service_name: Optional[str] = None
    staff_name: Optional[str] = None
    customer_name: Optional[str] = None
    service_price: Optional[Decimal] = None
    location: Optional[EmbeddedLocation] = None
    review: Optional[BookingReviewSummary] = None

class BookingChangeResponse(BaseModel):
    id: str
    booking_id: str
    old_start_time: Optional[datetime]
    new_start_time: Optional[datetime]
    change_type: str
    changed_by: Optional[str]
    reason: Optional[str]
    created_at: datetime

class BookingLogResponse(BaseModel):
    id: str
    booking_id: str
    action: str
    performed_by: Optional[str]
    details: Optional[dict]
    created_at: datetime

class WaitlistCreate(BaseModel):
    service_id: str
    customer_id: Optional[str] = None
    preferred_date: Optional[date] = None

class WaitlistUpdate(BaseModel):
    status: Optional[str] = None
    preferred_date: Optional[date] = None

class WaitlistResponse(BaseModel):
    id: str
    service_id: str
    customer_id: str
    preferred_date: Optional[date]
    status: str
    created_at: datetime

# Payment Schemas
class PaymentCreate(BaseModel):
    booking_id: str
    amount: Decimal
    currency: str = "USD"
    provider: str = "aba_payway"

class PaymentResponse(BaseModel):
    id: str
    booking_id: str
    provider: str
    provider_reference: Optional[str]
    amount: Decimal
    currency: str
    status: str
    created_at: datetime

class PaymentIntent(BaseModel):
    provider: str
    payment_url: Optional[str] = None
    payment_id: str
    transaction_id: str
    merchant_id: Optional[str] = None
    gateway_mode: Optional[str] = None
    settlement_destination: Optional[str] = None
    qr_image: Optional[str] = None
    qr_string: Optional[str] = None
    deeplink: Optional[str] = None
    app_store: Optional[str] = None
    play_store: Optional[str] = None
    payment_status: Optional[str] = None
    expires_at: Optional[datetime] = None

# Review Schemas
class ReviewCreate(BaseModel):
    booking_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None

class ReviewResponse(BaseModel):
    id: str
    booking_id: str
    rating: int
    comment: Optional[str]
    is_approved: bool
    created_at: datetime

# Notification Schemas
class NotificationCreate(BaseModel):
    booking_id: str
    channel: str  # "email" or "sms"
    type: str  # "confirmation", "reminder", "cancellation", "feedback"
    recipient: str

class NotificationResponse(BaseModel):
    id: str
    booking_id: str
    channel: str
    type: str
    recipient: str
    status: str
    sent_at: Optional[datetime]
    created_at: datetime

# Analytics Schemas
class BookingStats(BaseModel):
    total_bookings: int
    confirmed_bookings: int
    cancelled_bookings: int
    completed_bookings: int
    pending_bookings: int
    total_revenue: Decimal
    average_booking_value: Decimal

class ServiceStats(BaseModel):
    service_id: str
    service_name: str
    total_bookings: int
    total_revenue: Decimal
    average_rating: Optional[float]

class StaffStats(BaseModel):
    staff_id: str
    staff_name: str
    total_bookings: int
    completed_bookings: int
    total_revenue: Decimal
    average_rating: Optional[float]

class DailyStats(BaseModel):
    date: date
    total_bookings: int
    total_revenue: Decimal
