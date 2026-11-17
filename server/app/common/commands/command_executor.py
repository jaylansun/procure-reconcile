"""在独立PG事务内执行命令，统一处理幂等、来源锁和审计。"""

import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, SessionTransaction

from app.common.audit.audit import append_audit
from app.common.commands.command_context import CommandContext
from app.common.commands.fingerprint import _validate_json, command_fingerprint
from app.common.commands.locks import lock_command, lock_sources
from app.common.errors import AppError
from app.common.maintenance.maintenance_gate import MaintenanceGate
from app.domain.dto.audit_change_dto import AuditChangeDTO
from app.domain.dto.command_result_dto import CommandResultDTO
from app.domain.dto.source_key_dto import SourceKeyDTO
from app.entity.administrator_do import AdministratorDO
from app.entity.command_record_do import CommandRecordDO
from app.persistence.database import DatabaseManager


class _CommandSession(Session):
    """由执行器拥有提交权，业务回调只能进行同一事务内的数据库操作。"""

    _handler_active = False
    _finalizing = False
    _transaction_violation = False

    def deny_transaction_control(self) -> None:
        # 锁存违规：业务即使捕获异常后继续返回，也不能变成一次成功提交。
        self._transaction_violation = True
        raise AppError(500, "transaction_control_forbidden", "业务回调不能结束执行器事务")

    def commit(self) -> None:
        self.deny_transaction_control()

    def rollback(self) -> None:
        if self._handler_active:
            self.deny_transaction_control()
        super().rollback()

    def close(self) -> None:
        if self._handler_active:
            self.deny_transaction_control()
        super().close()

    def reset(self) -> None:
        if self._handler_active:
            self.deny_transaction_control()
        super().reset()

    def invalidate(self) -> None:
        if self._handler_active:
            self.deny_transaction_control()
        super().invalidate()

    def begin(self, nested: bool = False) -> SessionTransaction:
        if self._handler_active:
            self.deny_transaction_control()
        return super().begin(nested=nested)

    def connection(self, *args: Any, **kwargs: Any) -> Connection:
        if self._handler_active:
            self.deny_transaction_control()
        return super().connection(*args, **kwargs)


@event.listens_for(_CommandSession, "before_commit")
def _guard_commit(session: _CommandSession) -> None:
    """同时阻止绕过Session.commit直接提交SessionTransaction的普通误用。"""
    if not session._finalizing:
        session.deny_transaction_control()


@event.listens_for(_CommandSession, "after_transaction_end")
def _notice_ended_transaction(session: _CommandSession, transaction: SessionTransaction) -> None:
    """回调直接关闭或回滚根事务后锁存失败；清理期间不抛异常遮盖原错误。"""
    if transaction.parent is None and not session._finalizing:
        session._transaction_violation = True


def _normalized_result(result: CommandResultDTO) -> CommandResultDTO:
    """在提交前校验响应，并复制JSON数据，避免返回业务持有的可变对象。"""
    if not isinstance(result, CommandResultDTO):
        raise ValueError("业务回调必须返回CommandResultDTO")
    if type(result.status_code) is not int or not 200 <= result.status_code < 300:
        raise ValueError("只允许保存2xx成功结果")
    if result.status_code in (204, 205) and result.body is not None:
        raise ValueError("204和205不能包含响应体")
    try:
        # 与现有请求内容约定一致：数量、金额使用字符串，拒绝float和Decimal。
        _validate_json(result.body, set())
        encoded = json.dumps(result.body, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        body = json.loads(encoded)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("命令结果无法转换为有效的UTF-8 JSON") from error
    return CommandResultDTO(status_code=result.status_code, body=body, replayed=False)


class CommandExecutor:
    """每次调用自开事务；仅对PG明确要求重试的事务失败重新执行回调。

    handler必须同步且只通过context.session操作数据库，不得使用外部连接、
    原始SQL事务控制、网络或文件副作用。此类副作用不属于PG可回滚范围。
    """

    def __init__(
        self,
        database: DatabaseManager,
        lock_timeout_ms: int = 3000,
        max_attempts: int = 3,
        gate: MaintenanceGate | None = None,
    ):
        if type(lock_timeout_ms) is not int or lock_timeout_ms <= 0:
            raise ValueError("锁等待时间必须是正整数毫秒")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("事务尝试次数必须在1至3之间")
        self.database = database
        self.lock_timeout_ms = lock_timeout_ms
        self.max_attempts = max_attempts
        self.gate = gate

    def execute(
        self,
        *,
        command_id: UUID,
        operation: str,
        payload: dict[str, object],
        actor_id: int,
        handler: Callable[[CommandContext], CommandResultDTO],
        sources: Sequence[SourceKeyDTO] = (),
        reason: str | None = None,
    ) -> CommandResultDTO:
        """同ID同内容重放，同ID不同内容冲突，失败不保存成功记录。"""
        if not isinstance(command_id, UUID):
            raise ValueError("命令ID必须是UUID")
        if type(actor_id) is not int or actor_id <= 0:
            raise ValueError("操作者ID必须是正整数")
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是字典")
        # 公共原因也是本次操作的依据。先按审计契约验证，再和业务内容共同参与指纹。
        checked_reason = AuditChangeDTO("command", str(command_id), reason=reason).reason
        fingerprint = command_fingerprint(operation, {"payload": payload, "reason": checked_reason})
        if len(operation) > 128:
            raise ValueError("业务操作名称不能超过128字符")
        if not callable(handler):
            raise ValueError("必须提供同步业务回调")
        selected_sources = tuple(sources)
        with self.gate.writer() if self.gate is not None else nullcontext():
            for attempt in range(self.max_attempts):
                try:
                    return self._attempt(
                        command_id,
                        operation,
                        fingerprint,
                        actor_id,
                        handler,
                        selected_sources,
                        checked_reason,
                    )
                except DBAPIError as error:
                    state = getattr(error.orig, "sqlstate", None)
                    if state == "55P03":
                        raise AppError(
                            409, "resource_busy", "相关记录正在处理，请稍后重试"
                        ) from error
                    if state not in {"40001", "40P01"} or attempt + 1 >= self.max_attempts:
                        raise
        raise AssertionError("事务尝试次数已耗尽")

    def _attempt(
        self,
        command_id: UUID,
        operation: str,
        fingerprint: str,
        actor_id: int,
        handler: Callable[[CommandContext], CommandResultDTO],
        sources: Sequence[SourceKeyDTO],
        reason: str | None,
    ) -> CommandResultDTO:
        engine = self.database.engine
        if engine is None:
            raise AppError(503, "database_not_configured", "数据库尚未配置")
        # 每次尝试使用新Session；等待命令锁后，后续语句能看到前次已提交结果。
        bind = engine.execution_options(isolation_level="READ COMMITTED")
        with _CommandSession(bind=bind, expire_on_commit=False) as session:
            with session.begin() as transaction:
                lock_command(session, command_id, timeout_ms=self.lock_timeout_ms)
                if session.get(AdministratorDO, actor_id) is None:
                    raise AppError(403, "actor_not_found", "操作者不存在")
                stored = session.get(CommandRecordDO, command_id)
                if stored is not None:
                    if stored.operation != operation or stored.request_fingerprint != fingerprint:
                        raise AppError(409, "command_conflict", "同一命令ID不能用于不同请求")
                    result = CommandResultDTO(
                        stored.response_status, stored.response_body, replayed=True
                    )
                else:
                    lock_sources(session, sources, timeout_ms=self.lock_timeout_ms)
                    context = CommandContext(session, command_id, actor_id, operation)
                    session._handler_active = True
                    try:
                        returned = handler(context)
                    finally:
                        session._handler_active = False
                    if session._transaction_violation or not transaction.is_active:
                        session.deny_transaction_control()
                    result = _normalized_result(returned)
                    body = result.body
                    if body is None and result.status_code not in (204, 205):
                        body = JSON.NULL
                    session.add(
                        CommandRecordDO(
                            command_id=command_id,
                            operation=operation,
                            request_fingerprint=fingerprint,
                            response_status=result.status_code,
                            response_body=body,
                        )
                    )
                    append_audit(
                        session,
                        command_id=command_id,
                        actor_id=actor_id,
                        operation=operation,
                        change=AuditChangeDTO(
                            target_type="command",
                            target_id=str(command_id),
                            reason=reason,
                            details={
                                "request_fingerprint": fingerprint,
                                "response_status": result.status_code,
                            },
                        ),
                    )
                    for change in context._changes:
                        append_audit(
                            session,
                            command_id=command_id,
                            actor_id=actor_id,
                            operation=operation,
                            change=change,
                        )
                # 仅这个受控位置允许根事务提交；业务不能自行提前开启此标记。
                session._finalizing = True
            return result
