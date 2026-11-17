"""检查实例配置、数据库迁移和初始化状态。"""

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import Settings
from app.domain.dto.readiness_dto import ReadinessDTO
from app.entity.administrator_do import AdministratorDO
from app.entity.auth_session_do import AuthSessionDO
from app.entity.command_record_do import CommandRecordDO
from app.entity.supplier_do import SupplierDO
from app.entity.unit_do import UnitDO
from app.persistence.database import DatabaseManager
from app.persistence.schema import MigrationMetadataError, expected_schema_heads


class ReadinessService:
    """通过显式只读检查报告后端是否可以接受业务请求。"""

    @staticmethod
    def check(settings: Settings, database: DatabaseManager) -> ReadinessDTO:
        """检查当前实例数据库和管理员是否准备就绪。"""
        if not settings.instance_id:
            return ReadinessDTO(False, "instance_not_configured")
        if database.engine is None or database.sessions is None:
            return ReadinessDTO(False, "database_not_configured")
        try:
            with database.sessions() as session:
                session.execute(text("SELECT 1"))
                has_version = session.scalar(text("SELECT to_regclass('public.alembic_version')"))
                if not has_version:
                    return ReadinessDTO(False, "schema_not_ready")
                revisions = set(
                    session.scalars(text("SELECT version_num FROM public.alembic_version"))
                )
                if revisions != expected_schema_heads():
                    return ReadinessDTO(False, "schema_not_ready")
                # 迁移版本与实际必需表同时校验，避免缺表时仍宣布服务就绪。
                for table_name in (
                    "administrators",
                    "auth_sessions",
                    "command_records",
                    "audit_events",
                    "stored_files",
                    "suppliers",
                    "units",
                ):
                    existing_table = session.scalar(
                        text("SELECT to_regclass(:table_name)"),
                        {"table_name": f"public.{table_name}"},
                    )
                    if existing_table is None:
                        return ReadinessDTO(False, "schema_not_ready")
                if not session.get(AdministratorDO, 1):
                    return ReadinessDTO(False, "administrator_not_initialized")
                session.execute(select(AuthSessionDO.id).limit(1))
                session.execute(select(CommandRecordDO.command_id).limit(1))
                session.execute(select(SupplierDO.id).limit(1))
                session.execute(select(UnitDO.id).limit(1))
                return ReadinessDTO(True)
        except MigrationMetadataError:
            return ReadinessDTO(False, "schema_not_ready")
        except SQLAlchemyError:
            # 对外握手仅返回固定原因，隐藏连接地址、凭据和 SQL 细节。
            return ReadinessDTO(False, "database_unavailable")
