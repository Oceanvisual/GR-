"""Single error format of contracts/api.md: `{"error": {"code", "message", "details"}}`."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from gr.logging import get_logger

log = get_logger(__name__)

# code → (HTTP status, default Russian message); table «Формат ошибки» in api.md.
ERRORS: dict[str, tuple[int, str]] = {
    "validation_error": (400, "Некорректный запрос."),
    "unauthorized": (401, "Требуется вход: неверный логин или пароль."),
    "csrf_rejected": (403, "Запрос с чужого сайта отклонён."),
    "not_found": (404, "Не найдено."),
    "version_conflict": (409, "Данные изменились в другой вкладке: обновите страницу."),
    "run_in_progress": (409, "Анализ этого документа уже выполняется."),
    "ocr_in_progress": (409, "Документ ещё распознаётся (OCR)."),
    "file_too_large": (413, "Файл больше допустимого размера."),
    "document_too_long": (413, "Документ длиннее допустимого объёма."),
    "unsupported_format": (415, "Формат файла не поддерживается."),
    "corrupted_file": (422, "Файл повреждён и не читается."),
    "encrypted_file": (422, "Файл защищён паролем."),
    "no_text_layer": (422, "В PDF нет текстового слоя: требуется OCR."),
    "empty_document": (422, "В документе нет текста."),
    "encoding_undetected": (422, "Не удалось определить кодировку текста."),
    "document_not_analyzable": (422, "Документ нельзя анализировать: см. диагностику."),
    "llm_not_configured": (503, "LLM не настроена: задайте LLM_* в конфигурации."),
    "external_llm_not_allowed": (503, "Политика данных запрещает внешний LLM-сервис."),
    "internal_error": (500, "Внутренняя ошибка сервиса."),
}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    """The body of every error answer (api.md § Формат ошибки)."""

    error: ErrorBody


# OpenAPI of the API routers: errors in the common format. Declared as ranges, so FastAPI does
# not add its own 422: an invalid request is a 400 validation_error here.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "4XX": {"model": ErrorResponse, "description": "Ошибка запроса (таблица api.md)"},
    "5XX": {"model": ErrorResponse, "description": "LLM недоступна по политике или сбой"},
}


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message_ru: str | None = None,
        details: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if code not in ERRORS:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.status, default_message = ERRORS[code]
        self.message_ru = message_ru or default_message
        self.details = details or {}
        self.headers = headers
        super().__init__(f"{code}: {self.message_ru}")


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(error: AppError) -> JSONResponse:
    return JSONResponse(
        error_body(error.code, error.message_ru, error.details),
        status_code=error.status,
        headers=error.headers,
    )


_STATUS_CODES = {401: "unauthorized", 403: "csrf_rejected", 404: "not_found"}


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc)


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Location and type only: `input` may echo document text or credentials back.
    errors = [{"loc": list(e.get("loc", ())), "type": e.get("type")} for e in exc.errors()]
    return error_response(AppError("validation_error", details={"errors": errors}))


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code)
    if code is None:
        # 405 and the like: keep the status, use the common body shape.
        return JSONResponse(
            error_body(
                "validation_error" if exc.status_code < 500 else "internal_error", str(exc.detail)
            ),
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return error_response(AppError(code, headers=dict(exc.headers) if exc.headers else None))


async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
    log.error("http.unhandled", exc_type=type(exc).__name__, exc_info=exc)
    return error_response(AppError("internal_error"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
