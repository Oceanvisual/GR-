"""Liveness for Docker (no auth) and readiness for operators (with auth)."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

import gr
from gr.api.deps import StateDep, require_user
from gr.llm.base import LLMPolicyError
from gr.llm.policy import check_data_policy

liveness = APIRouter()
readiness = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


@liveness.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@readiness.get("/readyz")
async def readyz(state: StateDep) -> dict[str, Any]:
    try:
        async with state.db.read() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    try:
        check_data_policy(state.settings)
        llm_error = None
    except LLMPolicyError as exc:
        llm_error = exc.code
    return {
        "db": db_status,
        "llm": {
            "policy": state.settings.llm_data_policy,
            "configured": llm_error is None,
            "error": llm_error,
        },
        "ocr": {
            "available": state.ocr.available,
            "langs": state.ocr.langs,
            "reason": state.ocr.reason,
        },
        "version": gr.__version__,
    }
