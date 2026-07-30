"""
api/db/database.py
------------------
SQLAlchemy engine + session factory.
La URL se lee de la variable de entorno DATABASE_URL.
Si no está definida, usa SQLite local como fallback de desarrollo.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./logs/paper_trade.db",
)

# psycopg2 usa postgresql://, SQLAlchemy 2.x también acepta postgresql+psycopg2://
# Si viene de Docker Compose con postgres://, normalizar:
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # SQLite necesita check_same_thread=False para uso en threads
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def create_tables():
    """Crea todas las tablas si no existen."""
    from api.db import models  # noqa: F401 — importar para que Base las registre
    Base.metadata.create_all(bind=engine)


def get_db():
    """Dependency de FastAPI para inyectar sesión DB."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
