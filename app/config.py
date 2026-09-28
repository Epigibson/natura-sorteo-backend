"""Application settings for sorteo-natura backend."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "sorteo-natura-api"
    env: str = "dev"

    # MongoDB (replica set opcional; sin transacciones multi-doc)
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "sorteo_natura"

    # JWT
    jwt_secret: str = "dev-only-secret-change-me-32bytes-min!!"
    jwt_alg: str = "HS256"
    access_ttl_min: int = 60
    refresh_ttl_days: int = 14

    # Seed admin (dev bootstrap — cambiar en producción)
    seed_admin_phone: str = "4461445984"
    seed_admin_password: str = "Yuri183c97abril"
    seed_admin_name: str = "Yuri"

    # CORS — Angular dev server
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"

    # Cloudinary (imágenes de productos)
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""

    # Enlace de videollamada reutilizable (la misma sala para todos los sorteos)
    default_meet_url: str = "https://meet.google.com/hea-kbvn-hja"


@lru_cache
def get_settings() -> Settings:
    return Settings()
