import httpx
from app.core.config import settings

class SupabaseClient:
    def __init__(self):
        self.url = settings.SUPABASE_URL
        self.anon_key = settings.SUPABASE_ANON_KEY
        self.service_key = settings.SUPABASE_SERVICE_ROLE_KEY
    
    def get_headers(self, use_service_key: bool = False):
        key = self.service_key if use_service_key else self.anon_key
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    
    async def verify_user(self, token: str):
        """Verify user token with Supabase Auth"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.url}/auth/v1/user",
                headers={
                    "apikey": self.anon_key,
                    "Authorization": f"Bearer {token}"
                }
            )
            if response.status_code == 200:
                return response.json()
            return None

supabase_client = SupabaseClient()
