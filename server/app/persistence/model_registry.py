"""显式注册所有 DO，为迁移和数据库元数据检查提供完整表集合。"""

from app.entity.administrator_do import AdministratorDO
from app.entity.audit_event_do import AuditEventDO
from app.entity.auth_session_do import AuthSessionDO
from app.entity.base_do import BaseDO
from app.entity.command_record_do import CommandRecordDO
from app.entity.stored_file_do import StoredFileDO
from app.entity.supplier_do import SupplierDO
from app.entity.unit_do import UnitDO

# 新增 DO 后在此导入，使迁移读取的元数据包含全部表及外键目标。
metadata = BaseDO.metadata

__all__ = [
    "AdministratorDO",
    "AuditEventDO",
    "AuthSessionDO",
    "BaseDO",
    "CommandRecordDO",
    "StoredFileDO",
    "SupplierDO",
    "UnitDO",
    "metadata",
]
