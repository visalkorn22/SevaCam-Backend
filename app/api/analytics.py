from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, date
from decimal import Decimal
from app.core.database import get_db
from app.core.auth import require_roles
from app.models.schemas import BookingStats, ServiceStats, StaffStats, DailyStats

router = APIRouter()

@router.get("/bookings/stats", response_model=BookingStats)
async def get_booking_stats(
    start_date: datetime = None,
    end_date: datetime = None,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get overall booking statistics"""
    query = """
        SELECT 
            COUNT(*) as total_bookings,
            COUNT(CASE WHEN status = 'confirmed' THEN 1 END) as confirmed_bookings,
            COUNT(CASE WHEN status = 'cancelled' THEN 1 END) as cancelled_bookings,
            COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_bookings,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_bookings,
            COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0) as total_revenue,
            COALESCE(AVG(CASE WHEN b.payment_status = 'paid' THEN s.price END), 0) as average_booking_value
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        WHERE 1=1
    """
    params = {}
    
    if start_date:
        query += " AND b.created_at >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND b.created_at <= :end_date"
        params["end_date"] = end_date
    
    result = db.execute(query, params)
    stats = result.fetchone()
    
    return {
        "total_bookings": stats[0] or 0,
        "confirmed_bookings": stats[1] or 0,
        "cancelled_bookings": stats[2] or 0,
        "completed_bookings": stats[3] or 0,
        "pending_bookings": stats[4] or 0,
        "total_revenue": Decimal(str(stats[5] or 0)),
        "average_booking_value": Decimal(str(stats[6] or 0)),
    }

@router.get("/services/stats", response_model=List[ServiceStats])
async def get_service_stats(
    start_date: datetime = None,
    end_date: datetime = None,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get statistics by service"""
    query = """
        SELECT 
            s.id as service_id,
            s.name as service_name,
            COUNT(b.id) as total_bookings,
            COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0) as total_revenue,
            AVG(r.rating) as average_rating
        FROM services s
        LEFT JOIN bookings b ON s.id = b.service_id
        LEFT JOIN reviews r ON b.id = r.booking_id
        WHERE 1=1
    """
    params = {}
    
    if start_date:
        query += " AND b.created_at >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND b.created_at <= :end_date"
        params["end_date"] = end_date
    
    query += " GROUP BY s.id, s.name ORDER BY total_revenue DESC"
    
    result = db.execute(query, params)
    stats = result.fetchall()
    
    return [
        {
            "service_id": row[0],
            "service_name": row[1],
            "total_bookings": row[2] or 0,
            "total_revenue": Decimal(str(row[3] or 0)),
            "average_rating": float(row[4]) if row[4] else None,
        }
        for row in stats
    ]

@router.get("/staff/stats", response_model=List[StaffStats])
async def get_staff_stats(
    start_date: datetime = None,
    end_date: datetime = None,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get statistics by staff member"""
    query = """
        SELECT 
            u.id as staff_id,
            u.full_name as staff_name,
            COUNT(b.id) as total_bookings,
            COUNT(CASE WHEN b.status = 'completed' THEN 1 END) as completed_bookings,
            COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0) as total_revenue,
            AVG(r.rating) as average_rating
        FROM users u
        LEFT JOIN bookings b ON u.id = b.staff_id
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN reviews r ON b.id = r.booking_id
        WHERE u.role = 'staff'
    """
    params = {}
    
    if start_date:
        query += " AND b.created_at >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND b.created_at <= :end_date"
        params["end_date"] = end_date
    
    query += " GROUP BY u.id, u.full_name ORDER BY total_revenue DESC"
    
    result = db.execute(query, params)
    stats = result.fetchall()
    
    return [
        {
            "staff_id": row[0],
            "staff_name": row[1] or "Unknown",
            "total_bookings": row[2] or 0,
            "completed_bookings": row[3] or 0,
            "total_revenue": Decimal(str(row[4] or 0)),
            "average_rating": float(row[5]) if row[5] else None,
        }
        for row in stats
    ]

@router.get("/daily/stats", response_model=List[DailyStats])
async def get_daily_stats(
    start_date: date = None,
    end_date: date = None,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get daily booking statistics"""
    query = """
        SELECT 
            DATE(b.created_at) as date,
            COUNT(b.id) as total_bookings,
            COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0) as total_revenue
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        WHERE 1=1
    """
    params = {}
    
    if start_date:
        query += " AND DATE(b.created_at) >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND DATE(b.created_at) <= :end_date"
        params["end_date"] = end_date
    
    query += " GROUP BY DATE(b.created_at) ORDER BY date DESC"
    
    result = db.execute(query, params)
    stats = result.fetchall()
    
    return [
        {
            "date": row[0],
            "total_bookings": row[1] or 0,
            "total_revenue": Decimal(str(row[2] or 0)),
        }
        for row in stats
    ]

@router.get("/admin-stats")
async def get_admin_stats(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Admin overview stats"""
    booking_result = db.execute("SELECT COUNT(*) FROM bookings").fetchone()
    revenue_result = db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0)
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        """
    ).fetchone()
    user_result = db.execute(
        "SELECT COUNT(*) FROM users WHERE is_active = TRUE"
    ).fetchone()

    return {
        "totalBookings": int(booking_result[0] or 0),
        "totalRevenue": float(revenue_result[0] or 0),
        "totalUsers": int(user_result[0] or 0),
        "growthRate": 0,
    }

@router.get("/admin-dashboard")
async def get_admin_dashboard(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Admin dashboard metrics"""
    totals = db.execute(
        "SELECT COUNT(*) FROM bookings"
    ).fetchone()
    upcoming = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE start_time_utc >= NOW()"
    ).fetchone()
    revenue = db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0)
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        """
    ).fetchone()
    avg_rating = db.execute(
        "SELECT COALESCE(AVG(r.rating), 0) FROM reviews r"
    ).fetchone()
    cancellation_rate = db.execute(
        """
        SELECT COALESCE(
            CAST(COUNT(CASE WHEN status = 'cancelled' THEN 1 END) AS FLOAT)
            / NULLIF(COUNT(*), 0) * 100,
            0
        )
        FROM bookings
        """
    ).fetchone()
    total_reviews = db.execute("SELECT COUNT(*) FROM reviews").fetchone()
    active_users = db.execute(
        "SELECT COUNT(*) FROM users WHERE is_active = TRUE"
    ).fetchone()

    return {
        "totalBookings": int(totals[0] or 0),
        "upcomingBookings": int(upcoming[0] or 0),
        "totalRevenue": float(revenue[0] or 0),
        "avgRating": float(avg_rating[0] or 0),
        "cancellationRate": float(cancellation_rate[0] or 0),
        "totalReviews": int(total_reviews[0] or 0),
        "activeUsers": int(active_users[0] or 0),
    }
