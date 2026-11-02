"""验证命令HTTP输入与响应的公开契约，不连接数据库。"""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services.command_executor import CommandResult
from app.services.http import (
    CommandExecutorDep,
    CommandInput,
    VersionedCommandInput,
    command_response,
    get_command_executor,
)


class ExampleInput(VersionedCommandInput):
    """用真实业务字段形状验证根命令编号和嵌套内容的边界。"""

    name: str
    quantity: str
    related_id: UUID
    lines: list[dict[str, object]]


def test_payload_removes_only_root_command_id_and_serializes_json_values() -> None:
    """只移除根命令ID，版本、空格和嵌套同名字段仍必须参与指纹。"""
    command_id, related_id = uuid4(), uuid4()
    body = ExampleInput(
        command_id=command_id,
        expected_version=0,
        name=" 中文原值 ",
        quantity="1.000000",
        related_id=related_id,
        lines=[{"command_id": "nested-id", "name": "行一", "empty": None}],
    )
    payload = body.command_payload()
    assert payload == {
        "expected_version": 0,
        "name": " 中文原值 ",
        "quantity": "1.000000",
        "related_id": str(related_id),
        "lines": [{"command_id": "nested-id", "name": "行一", "empty": None}],
    }
    payload["lines"][0]["command_id"] = "mutated"
    assert body.lines[0]["command_id"] == "nested-id"
    assert body.command_id == command_id


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1])
def test_nonnegative_integer_versions_are_accepted(value: int) -> None:
    """版本0及正常正整数保留整数类型，不转换为字符串或浮点数。"""
    parsed = VersionedCommandInput(command_id=str(uuid4()), expected_version=value)
    assert type(parsed.expected_version) is int
    assert parsed.command_payload() == {"expected_version": value}


@pytest.mark.parametrize("value", [-1, True, False, "0", "1", 0.0, 1.0, None])
def test_version_is_strict_and_cannot_coerce_other_types(value: object) -> None:
    """负数、布尔、文本和浮点数不能被宽松转换成合法版本。"""
    with pytest.raises(ValidationError):
        VersionedCommandInput(command_id=uuid4(), expected_version=value)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"command_id": "not-a-uuid"},
        {"command_id": None},
        {"command_id": str(uuid4()), "unexpected": "hidden"},
    ],
)
def test_command_id_is_required_uuid_and_extra_fields_are_forbidden(values: dict) -> None:
    """命令身份不能省略，未声明字段不能被静默丢弃而逃过指纹。"""
    with pytest.raises(ValidationError):
        CommandInput.model_validate(values)


def test_required_version_cannot_be_omitted() -> None:
    """变更类输入必须显式携带预期版本，不能默认猜测。"""
    with pytest.raises(ValidationError):
        VersionedCommandInput(command_id=uuid4())


@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize(
    ("status", "body", "encoded"),
    [(200, None, b"null"), (201, {"name": "中文", "quantity": "1.000000"}, None)],
)
def test_json_responses_preserve_status_body_and_replay_header(
    replayed: bool, status: int, body: object, encoded: bytes | None
) -> None:
    """首次与重放状态和JSON相同，200空值必须是JSON null。"""
    app = FastAPI()

    @app.get("/")
    def endpoint():
        """模拟任意业务接口复用统一响应转换器。"""
        return command_response(CommandResult(status, body, replayed=replayed))

    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == status
    assert response.json() == body
    if encoded is not None:
        assert response.content == encoded
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-command-replayed"] == str(replayed).lower()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("status", [204, 205])
@pytest.mark.parametrize("replayed", [False, True])
def test_no_content_statuses_emit_no_json_null(status: int, replayed: bool) -> None:
    """204和205没有响应体，重放仍保留相同状态和小写标记。"""
    app = FastAPI()

    @app.post("/")
    def endpoint():
        """模拟只返回成功状态的幂等命令。"""
        return command_response(CommandResult(status, replayed=replayed))

    with TestClient(app) as client:
        response = client.post("/")
    assert response.status_code == status
    assert response.content == b""
    assert "content-type" not in response.headers
    assert response.headers["x-command-replayed"] == str(replayed).lower()
    assert response.headers["cache-control"] == "no-store"


def test_executor_dependency_uses_current_app_state_without_constructing_another() -> None:
    """同一应用依赖直接复用当前执行器，不创建连接或切换配置。"""
    app = FastAPI()
    selected = object()
    app.state.command_executor = selected

    @app.get("/")
    def endpoint(request: Request, executor: CommandExecutorDep):
        """同时检查原始依赖函数与FastAPI注入的对象身份。"""
        return {
            "same": executor is selected,
            "direct": get_command_executor(request) is selected,
        }

    with TestClient(app) as client:
        assert client.get("/").json() == {"same": True, "direct": True}
