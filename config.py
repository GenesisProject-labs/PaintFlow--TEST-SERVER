import os
from pydantic import BaseModel

class Settings(BaseModel):
    # API Configuration - use environment variables if available
    API_HOST: str = os.getenv('API_HOST', '0.0.0.0')  # Changed to 0.0.0.0 for production
    API_PORT: int = int(os.getenv('PORT', 8001))
    API_DEBUG: bool = os.getenv('API_DEBUG', 'False').lower() == 'true'
    API_TITLE: str = "PaintFlow API"
    API_VERSION: str = "2.0.0"
    
    # Database Configuration
    DB_HOST: str = os.getenv('DB_HOST', 'dpg-d1b18u8dl3ps73e68v1g-a.oregon-postgres.render.com')
    DB_PORT: int = int(os.getenv('DB_PORT', 5432))
    DB_NAME: str = os.getenv('DB_NAME', 'labels_app_db')
    DB_USER: str = os.getenv('DB_USER', 'admin')
    DB_PASSWORD: str = os.getenv('DB_PASSWORD', 'KCFjzM4KYzSQx63ArufESIXq03EFXHz3')
    DB_POOL_MIN: int = int(os.getenv('DB_POOL_MIN', 1))
    DB_POOL_MAX: int = int(os.getenv('DB_POOL_MAX', 80))
    DB_POOL_GET_RETRIES: int = int(os.getenv('DB_POOL_GET_RETRIES', 3))
    DB_POOL_GET_BACKOFF_SEC: float = float(os.getenv('DB_POOL_GET_BACKOFF_SEC', 0.25))
    DB_CONNECT_TIMEOUT: int = int(os.getenv('DB_CONNECT_TIMEOUT', 5))
    
    # Frontend Configuration
    FRONTEND_URL: str = os.getenv('FRONTEND_URL', 'http://127.0.0.1:8001')
    
    # CORS Configuration - Allow both localhost and production URL
    CORS_ORIGINS: list = [
        'http://127.0.0.1:8001',
        'http://localhost:8001',
        os.getenv('FRONTEND_URL', 'http://127.0.0.1:8001')
    ]
    # Add Render URL if provided
    if os.getenv('RENDER_URL'):
        CORS_ORIGINS.append(os.getenv('RENDER_URL'))
    
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')

settings = Settings()
