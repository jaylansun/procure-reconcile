"""当前业务命令的事务上下文。"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.domain.dto.audit_change_dto import AuditChangeDTO


@dataclass
class CommandContext:
    """业务回调使用本次事务的Session，不能自行结束事务或访问其他连接。"""

    session: Session
    command_id: UUID
    actor_id: int
    operation: str
    _changes: list[AuditChangeDTO] = field(default_factory=list, init=False, repr=False)

    def audit(self, change: AuditChangeDTO) -> None:
        """累计目标变更，成功提交时与命令记录一起保存。"""
        if not isinstance(change, AuditChangeDTO):
            raise ValueError("审计变更必须是AuditChangeDTO")
        self._changes.append(change)

    def expect_version(self, actual: int, expected: int) -> None:
        """版本已变化时中止本次事务，避免旧页面覆盖新事实。"""
        if type(actual) is not int or type(expected) is not int:
            raise ValueError("版本必须是整数")
        if actual != expected:
            raise AppError(409, "version_conflict", "记录版本已变化，请刷新后重试")
