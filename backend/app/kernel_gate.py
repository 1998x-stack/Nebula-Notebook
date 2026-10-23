"""Notebook-wide execution arbitration. Unknown outcomes require Runtime stop."""
import asyncio
import time
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from .db import KernelGate, SessionLocal

async def claim(notebook_id: str, kind: str) -> str:
    token = f'{kind}:{uuid4()}'
    async with SessionLocal() as db:
        db.add(KernelGate(notebook_id=notebook_id, token=token, kind=kind, state='active'))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, 'Kernel busy or uncertain; wait or stop Runtime')
    return token

async def release(notebook_id: str, token: str) -> bool:
    async with SessionLocal() as db:
        result = await db.execute(delete(KernelGate).where(
            KernelGate.notebook_id == notebook_id, KernelGate.token == token,
            KernelGate.state == 'active'))
        await db.commit()
        return result.rowcount == 1

async def uncertain(notebook_id: str, token: str):
    async with SessionLocal() as db:
        await db.execute(update(KernelGate).where(
            KernelGate.notebook_id == notebook_id, KernelGate.token == token)
            .values(state='uncertain'))
        await db.commit()

async def recover_gates():
    async with SessionLocal() as db:
        await db.execute(update(KernelGate).where(KernelGate.state == 'active')
                         .values(state='uncertain'))
        await db.commit()

async def reset_after_runtime_stop(notebook_id: str):
    """Must be called only after Runtime has been safely stopped."""
    async with SessionLocal() as db:
        await db.execute(delete(KernelGate).where(KernelGate.notebook_id == notebook_id))
        await db.commit()

async def wait_for_gate(notebook_id: str, seconds: float = 60) -> str:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return await claim(notebook_id, 'run')
        except HTTPException:
            async with SessionLocal() as db:
                gate = await db.get(KernelGate, notebook_id)
                if gate and gate.state == 'uncertain':
                    raise RuntimeError('Kernel state uncertain: stop Runtime to recover')
            if time.monotonic() >= deadline:
                raise TimeoutError('Kernel execution admission timed out')
            await asyncio.sleep(0.2)
