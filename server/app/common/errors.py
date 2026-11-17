from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
    return error_response(error.status, error.code, error.message)


async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    # Do not echo request values: login bodies contain passwords.
    return error_response(422, "validation_error", "请求字段缺失或格式不正确")


async def http_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    codes = {404: "not_found", 405: "method_not_allowed"}
    return error_response(
        error.status_code, codes.get(error.status_code, "request_error"), "请求不可用"
    )
