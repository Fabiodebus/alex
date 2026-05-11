"""POST /callbacks — inbound ApprovalCallback from Slack/Teams."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ..schemas import ApprovalCallback
from ..services.approval_handler import (
    ApprovalHandler,
    ApprovalScopingError,
    TaskAlreadyActionedError,
    TaskNotFoundError,
)

router = APIRouter()


@router.post("/callbacks", status_code=status.HTTP_200_OK)
async def post_callbacks(callback: ApprovalCallback, request: Request) -> dict[str, object]:
    handler: ApprovalHandler = request.app.state.approval_handler
    try:
        result = await handler.handle(callback)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApprovalScopingError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except TaskAlreadyActionedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "task_id": result.task_id,
        "new_status": result.new_status,
        "outcome": result.outcome,
        "dispatched": result.dispatched,
    }
