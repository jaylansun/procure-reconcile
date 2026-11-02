"""验证命令记录的真实PG存储约束，不测试尚未实现的命令执行器。"""

import warnings
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, delete, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, SAWarning
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.database import Database
from app.persistence.models import CommandRecord
from app.persistence.schema import expected_schema_heads
from app.readiness import check_readiness
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.settings import Settings

pytestmark = pytest.mark.postgres


@pytest.fixture
def command_sessions(postgres_database: Database) -> Iterator[sessionmaker[Session]]:
    """复用专用测试库保护，在本模块每个用例前后清理命令记录。"""
    factory = postgres_database.sessions
    assert factory is not None
    with factory.begin() as session:
        session.execute(delete(CommandRecord))
    try:
        yield factory
    finally:
        with factory.begin() as session:
            session.execute(delete(CommandRecord))


def make_record(**overrides: object) -> CommandRecord:
    """由测试调用方提供命令ID，默认构造一条成功的JSON响应记录。"""
    values: dict[str, object] = {
        "command_id": uuid4(),
        "operation": "receipt.register",
        "request_fingerprint": "a" * 64,
        "response_status": 201,
        "response_body": {"message": "登记成功", "quantity": "1.000000"},
    }
    values.update(overrides)
    return CommandRecord(**values)


def test_supplied_id_and_original_operation_round_trip(
    command_sessions: sessionmaker[Session],
) -> None:
    """保存调用方提供的UUID，操作名不因两端空格被擅自修改。"""
    command_id = uuid4()
    operation = " receipt.register "
    with command_sessions.begin() as session:
        session.add(make_record(command_id=command_id, operation=operation))
    with command_sessions() as session:
        stored = session.get(CommandRecord, command_id)
        assert stored is not None
        assert stored.command_id == command_id
        assert stored.operation == operation


def test_missing_command_id_is_not_generated_automatically(
    command_sessions: sessionmaker[Session],
) -> None:
    """省略命令ID必须由数据库拒绝，不能在写入时悄悄生成新ID。"""
    record = CommandRecord(
        operation="receipt.register",
        request_fingerprint="a" * 64,
        response_status=204,
        response_body=None,
    )
    # 此处故意省略主键；验收数据库约束，不依赖ORM是否输出预警。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SAWarning)
        with pytest.raises(IntegrityError) as error, command_sessions.begin() as session:
            session.add(record)
    assert error.value.orig.sqlstate == "23502"
    assert error.value.orig.diag.column_name == "command_id"


def test_duplicate_id_is_rejected_without_replacing_original_result(
    command_sessions: sessionmaker[Session],
) -> None:
    """相同命令ID违反主键约束，失败写入不会覆盖已有成功结果。"""
    command_id = uuid4()
    with command_sessions.begin() as session:
        session.add(make_record(command_id=command_id))
    with pytest.raises(IntegrityError) as error, command_sessions.begin() as session:
        session.add(make_record(command_id=command_id, response_body={"message": "不应覆盖"}))
    assert error.value.orig.sqlstate == "23505"
    with command_sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 1
        stored = session.get(CommandRecord, command_id)
        assert stored is not None
        assert stored.response_body == {"message": "登记成功", "quantity": "1.000000"}


def test_different_ids_may_have_the_same_fingerprint(
    command_sessions: sessionmaker[Session],
) -> None:
    """两次不同操作可提交相同业务内容，指纹本身不应有唯一约束。"""
    first_id, second_id = uuid4(), uuid4()
    with command_sessions.begin() as session:
        session.add_all([make_record(command_id=first_id), make_record(command_id=second_id)])
    with command_sessions() as session:
        records = session.scalars(select(CommandRecord)).all()
        assert {record.command_id for record in records} == {first_id, second_id}
        assert {record.request_fingerprint for record in records} == {"a" * 64}


@pytest.mark.parametrize("operation", [None, "", " \t\n", "x" * 129])
def test_invalid_operations_are_rejected_by_postgresql(
    command_sessions: sessionmaker[Session], operation: object
) -> None:
    """操作名必须存在、包含非空白字符，且不能超出128字符。"""
    with pytest.raises(DBAPIError), command_sessions.begin() as session:
        session.add(make_record(operation=operation))


@pytest.mark.parametrize(
    "fingerprint",
    [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n"],
)
def test_invalid_fingerprints_are_rejected_by_postgresql(
    command_sessions: sessionmaker[Session], fingerprint: object
) -> None:
    """请求指纹必须是完整的64位小写十六进制字符串。"""
    with pytest.raises(DBAPIError), command_sessions.begin() as session:
        session.add(make_record(request_fingerprint=fingerprint))


@pytest.mark.parametrize("status", [None, 199, 300])
def test_missing_or_unsuccessful_response_status_is_rejected(
    command_sessions: sessionmaker[Session], status: object
) -> None:
    """未提供结果或非2xx响应不能被保存为成功命令记录。"""
    with pytest.raises(DBAPIError), command_sessions.begin() as session:
        session.add(make_record(response_status=status))


@pytest.mark.parametrize("status", [200, 299])
def test_success_status_boundaries_are_allowed(
    command_sessions: sessionmaker[Session], status: int
) -> None:
    """成功状态码的上下边界均可按约束存储。"""
    command_id = uuid4()
    with command_sessions.begin() as session:
        session.add(make_record(command_id=command_id, response_status=status))
    with command_sessions() as session:
        stored = session.get(CommandRecord, command_id)
        assert stored is not None
        assert stored.response_status == status


def test_chinese_nested_response_round_trip(command_sessions: sessionmaker[Session]) -> None:
    """嵌套JSON与中文往返保持内容，数量金额字符串不被改成浮点。"""
    command_id = uuid4()
    body = {
        "message": "收货登记成功",
        "result": {"quantity": "0.000001", "amount": "12.30"},
        "items": [{"name": "物料甲"}, True, 2, None],
    }
    with command_sessions.begin() as session:
        session.add(make_record(command_id=command_id, response_body=body))
    with command_sessions() as session:
        stored = session.get(CommandRecord, command_id)
        assert stored is not None
        assert stored.response_body == body


def test_no_content_and_json_null_are_distinct_in_postgresql(
    command_sessions: sessionmaker[Session],
) -> None:
    """204的Python None存成SQL NULL，200的JSON.NULL存成JSON空值。"""
    no_content_id, json_null_id = uuid4(), uuid4()
    with command_sessions.begin() as session:
        session.add_all(
            [
                make_record(command_id=no_content_id, response_status=204, response_body=None),
                make_record(command_id=json_null_id, response_status=200, response_body=JSON.NULL),
            ]
        )
    with command_sessions() as session:
        rows = session.execute(
            select(
                CommandRecord.command_id,
                CommandRecord.response_body,
                CommandRecord.response_body.is_(None).label("is_sql_null"),
                func.jsonb_typeof(CommandRecord.response_body).label("json_type"),
            )
        ).mappings()
        stored = {row["command_id"]: row for row in rows}
        assert stored[no_content_id]["response_body"] is None
        assert stored[no_content_id]["is_sql_null"] is True
        assert stored[no_content_id]["json_type"] is None
        assert stored[json_null_id]["response_body"] is None
        assert stored[json_null_id]["is_sql_null"] is False
        assert stored[json_null_id]["json_type"] == "null"


def test_created_at_has_a_database_default_and_timezone(
    command_sessions: sessionmaker[Session],
) -> None:
    """原始SQL省略创建时间仍可写入，验证默认值确实由PG生成且带时区。"""
    with command_sessions.begin() as session:
        created_at = session.scalar(
            text(
                "INSERT INTO command_records "
                "(command_id, operation, request_fingerprint, response_status, response_body) "
                "VALUES (:command_id, :operation, :fingerprint, 204, NULL) "
                "RETURNING created_at"
            ),
            {"command_id": uuid4(), "operation": "receipt.register", "fingerprint": "a" * 64},
        )
    assert isinstance(created_at, datetime)
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() is not None


def test_transaction_rollback_leaves_no_success_record(
    command_sessions: sessionmaker[Session],
) -> None:
    """即使已经flush，事务异常仍应撤销命令记录，不能留下半份成功结果。"""
    command_id = uuid4()
    with pytest.raises(RuntimeError, match="模拟事务失败"), command_sessions.begin() as session:
        session.add(make_record(command_id=command_id))
        session.flush()
        raise RuntimeError("模拟事务失败")
    with command_sessions() as session:
        assert session.get(CommandRecord, command_id) is None


def test_missing_command_table_is_not_ready_even_with_correct_version_and_admin(
    postgres_database: Database, command_sessions: sessionmaker[Session]
) -> None:
    """版本与管理员有效但命令表缺失时不能误报就绪，结束后恢复表名。"""
    engine = postgres_database.engine
    assert engine is not None
    initialize_administrator(postgres_database, "readiness-admin", new_session_token())
    settings = Settings(
        _env_file=None,
        environment="test",
        instance_id="command-table-readiness-test",
        database_url=engine.url.render_as_string(hide_password=False),
    )
    assert check_readiness(settings, postgres_database).ready is True
    # 名称只由固定前缀和UUID生成，不插入外部提供的SQL标识符。
    temporary_table = f"command_records_{uuid4().hex}"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f'ALTER TABLE public.command_records RENAME TO "{temporary_table}"'
        )
    try:
        status = check_readiness(settings, postgres_database)
        assert status.ready is False
        assert status.reason == "schema_not_ready"
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f'ALTER TABLE public."{temporary_table}" RENAME TO command_records'
            )
    assert check_readiness(settings, postgres_database).ready is True
    with command_sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 0


def test_command_migration_downgrade_and_upgrade_preserve_auth_tables(
    postgres_database: Database, command_sessions: sessionmaker[Session]
) -> None:
    """只在受保护测试库降级；不论断言成败，最终恢复当前head。"""
    engine = postgres_database.engine
    assert engine is not None
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    # 使用受保护fixture已选定的连接，不读取或推断开发环境配置。
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    try:
        command.downgrade(config, "0001_auth")
        inspector = inspect(engine)
        assert not inspector.has_table("command_records", schema="public")
        assert inspector.has_table("administrators", schema="public")
        assert inspector.has_table("auth_sessions", schema="public")
        with engine.connect() as connection:
            assert set(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            ) == {"0001_auth"}
    finally:
        command.upgrade(config, "head")
    assert inspect(engine).has_table("command_records", schema="public")
    with command_sessions() as session:
        assert set(session.scalars(text("SELECT version_num FROM public.alembic_version"))) == (
            expected_schema_heads()
        )
