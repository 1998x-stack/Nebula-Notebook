from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import settings


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginSession(Base):
    __tablename__ = 'login_sessions'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Folder(Base):
    """A flat, owner-scoped container for notebooks (one level, like the sidebar of
    most notebook tools). Folders are personal: they are never shared directly, but
    the notebooks inside keep their own per-notebook permissions."""
    __tablename__ = 'folders'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint('owner_id', 'name'),)


class Notebook(Base):
    __tablename__ = 'notebooks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    title: Mapped[str] = mapped_column(String(200))
    # NULL = root level. Kept nullable so existing notebooks need no data migration.
    folder_id: Mapped[str | None] = mapped_column(ForeignKey('folders.id'), nullable=True, index=True)
    # Serialised document size, maintained on write so the dashboard can sort by
    # size without re-serialising every notebook on each list request.
    document_bytes: Mapped[int] = mapped_column(Integer, default=0)
    document: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Source lineage is informational; forks never share runtime/workspace or grants.
    forked_from_notebook_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    forked_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class NotebookRevision(Base):
    """Immutable snapshot of a committed notebook document version."""
    __tablename__ = 'notebook_revisions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notebook_id: Mapped[str] = mapped_column(ForeignKey('notebooks.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    document: Mapped[dict] = mapped_column(JSON)
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(String(20), default='save')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('notebook_id', 'version', name='uq_notebook_revision'),)


class NotebookPermission(Base):
    __tablename__ = 'notebook_permissions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notebook_id: Mapped[str] = mapped_column(ForeignKey('notebooks.id'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    role: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint('notebook_id', 'user_id'),)


class Runtime(Base):
    __tablename__ = 'runtimes'
    notebook_id: Mapped[str] = mapped_column(ForeignKey('notebooks.id'), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    mode: Mapped[str] = mapped_column(String(8))
    container_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kernel_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(12), default='starting')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class ExecutionRun(Base):
    """At-most-once submission record. A crash never silently replays Python code."""
    __tablename__ = 'execution_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notebook_id: Mapped[str] = mapped_column(ForeignKey('notebooks.id'), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    cell_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(8), default='cpu')
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default='queued')
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outputs: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint('notebook_id', 'actor_id', 'idempotency_key',
                                      name='uq_execution_run_idempotency'),)


class CellOperation(Base):
    """One immutable operation result per Notebook/op ID, independent of Agent."""
    __tablename__ = 'cell_operations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notebook_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    operation_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    expected_version: Mapped[int] = mapped_column(Integer)
    committed_version: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('notebook_id', 'operation_id', name='uq_notebook_operation'),)


class NotebookEvent(Base):
    """Durable, globally monotonic SSE cursor. Records are scoped by notebook ACL."""
    __tablename__ = 'notebook_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notebook_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    document_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cell_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Audit(Base):
    __tablename__ = 'audits'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str] = mapped_column(String(36))
    detail: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


url = settings.database_url or f"sqlite+aiosqlite:///{(settings.data_dir / 'nebula.db').resolve()}"
engine = create_async_engine(url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as session:
        yield session


def log_event(db, actor_id, event, target_id, detail=''):
    db.add(Audit(actor_id=actor_id, event=event, target_id=target_id, detail=detail[:500]))
