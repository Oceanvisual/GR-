"""HTML screens (contracts/ui.md). Each screen uses the same services as /api/v1."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from gr.api.deps import StateDep, require_user

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)  # autoescape is on for .html
router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def documents_page(request: Request, state: StateDep) -> HTMLResponse:
    documents = await state.repos.documents.list()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"documents": documents, "llm_policy": state.settings.llm_data_policy},
    )
