"""把审计事件加入调用方的数据库事务。"""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.common.audit.validation import _MAX_VERSION, _text
from app.domain.dto.audit_change_dto import AuditChangeDTO
from app.entity.audit_event_do import AuditEventDO


def append_audit(
    session: Session, *, command_id: UUID, actor_id: int, operation: str, change: AuditChangeDTO
) -> AuditEventDO:
    """只加入当前Session，不flush/commit；由执行器与业务和命令记录原子提交。

    command_id外键由数据库延迟检查，允许执行器在同一事务稍后保存成功命令记录。
    每次调用追加新事件，不按command_id或对象标识查询并覆盖既有记录。
    """
    if not isinstance(command_id, UUID):
        raise ValueError("审计命令标识必须为UUID")
    if type(actor_id) is not int or not 1 <= actor_id <= _MAX_VERSION:
        raise ValueError("审计操作者必须为正整数")
    _text(operation, name="审计操作", maximum=128)
    if not isinstance(change, AuditChangeDTO):
        raise ValueError("审计变更必须为AuditChangeDTO")
    # details仍可能被外部显式修改；入Session前重新检查并脱离调用方容器。
    checked = AuditChangeDTO(
        target_type=change.target_type,
        target_id=change.target_id,
        before_version=change.before_version,
        after_version=change.after_version,
        reason=change.reason,
        details=change.details,
    )
    event = AuditEventDO(
        id=uuid4(),
        command_id=command_id,
        actor_id=actor_id,
        action=operation,
        target_type=checked.target_type,
        target_id=checked.target_id,
        before_version=checked.before_version,
        after_version=checked.after_version,
        reason=checked.reason,
        details=checked.details,
    )
    session.add(event)
    return event
