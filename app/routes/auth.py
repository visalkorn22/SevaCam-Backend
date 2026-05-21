from fastapi import APIRouter, Header, HTTPException
from typing import Optional

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Replace this with your real DB/session logic
def get_user_from_session(token: str):
    """
    Validate session token and return user dict or None
    """
    # TODO: query your database sessions table
    # Example return:
    # return {
    #   "id": "uuid",
    #   "email": "user@example.com",
    #   "full_name": "John Doe",
    #   "role": "customer",
    #   "avatar_url": None,
    # }
    return None


@router.get("/me")
def get_me(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.replace("Bearer ", "")

    user = get_user_from_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")

    return user
