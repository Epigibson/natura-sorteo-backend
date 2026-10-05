"""Application settings for sorteo-natura backend."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "sorteo-natura-api"
    env: str = "dev"

    # MongoDB (replica set opcional; sin transacciones multi-doc)
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "sorteo_natura"

    # JWT
    # Obligatorio vía variable de entorno / .env (sin default a propósito)
    jwt_secret: str = Field(min_length=32)
    jwt_alg: str = "HS256"
    access_ttl_min: int = 60
    refresh_ttl_days: int = 14

    # Seed admin: solo se usa si no existe el usuario. Obligatorios vía entorno.
    seed_admin_phone: str
    seed_admin_password: str = Field(min_length=8)
    seed_admin_name: str = "Yuri"

    # Zona horaria del sorteo y proxies de confianza (Vercel -> Render = 2)
    timezone: str = "America/Mexico_City"
    proxy_hops: int = 2

    # /docs, /redoc y /openapi.json: apagados salvo que se activen a propósito (dev)
    enable_docs: bool = False

    # Horas sin pagar antes de liberar un boleto registrado/raspado
    auto_release_hours: int = 48

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
