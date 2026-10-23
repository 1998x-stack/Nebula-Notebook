import os
import tempfile
from pathlib import Path

# Configure before application modules are imported.
_test_root = tempfile.mkdtemp(prefix='nebula-test-')
os.environ['DATA_DIR'] = _test_root
os.environ['ADMIN_EMAIL'] = 'admin@test.local'
os.environ['ADMIN_PASSWORD'] = 'integrationTestPassword123!'
os.environ['ALLOWED_ORIGINS'] = 'http://testserver'

import pytest
# The HTTP permission and document tests do not require Docker; allow running them
# on machines without the Docker Python SDK installed.
try:
    import docker
except ImportError:
    import sys
    from types import ModuleType, SimpleNamespace
    shim = ModuleType('docker')
    types_shim = ModuleType('docker.types')
    types_shim.DeviceRequest = type('DeviceRequest', (), {'__init__': lambda self, *a, **kw: None})
    shim.errors = SimpleNamespace(NotFound=type('NotFound',(Exception,),{}))
    shim.from_env = lambda **kwargs: (_ for _ in ()).throw(RuntimeError('Docker not installed'))
    sys.modules['docker'] = shim
    sys.modules['docker.types'] = types_shim
from httpx import ASGITransport, AsyncClient
from app.db import Base, engine, SessionLocal, User
from app.security import hash_password
from app.main import app
from uuid import uuid4


@pytest.fixture(autouse=True)
async def clean_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        db.add(User(id=str(uuid4()), email='admin@test.local', password_hash=hash_password('integrationTestPassword123!'), is_admin=True))
        await db.commit()
    yield


@pytest.fixture
async def clients():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        yield client


async def login(client, email='admin@test.local', password='integrationTestPassword123!'):
    resp = await client.post('/api/auth/login',json={'email':email,'password':password},headers={'Origin':'http://testserver'})
    assert resp.status_code == 200, resp.text
    return {'X-CSRF-Token':resp.json()['csrf'], 'Origin':'http://testserver'}
