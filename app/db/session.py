from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import Base

log = get_logger(__name__)

settings = get_settings()
database_url = settings.database_url

# SQLite needs check_same_thread for FastAPI threading
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(database_url, echo=False, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    # Import models to register with Base
    import app.db.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    log.info(f"Database initialized: {database_url.split('@')[-1] if '@' in database_url else database_url}")
