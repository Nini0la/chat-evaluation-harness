import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base, get_db
from app.main import create_app


@pytest.fixture
def app():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url="sqlite://",
        tester_access_code="tester-secret",
        admin_access_code="admin-secret",
        cookie_secure=False,
        model_provider="mock",
        model_api_key="model-secret",
    )
    application = create_app(settings)

    def override_db():
        with TestingSession() as db:
            yield db

    application.dependency_overrides[get_db] = override_db
    application.state.test_session_factory = TestingSession
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers():
    return {"X-Access-Code": "tester-secret"}


@pytest.fixture
def admin_headers():
    return {"X-Admin-Code": "admin-secret"}
