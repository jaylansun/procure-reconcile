from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_CONFIG = Path(__file__).resolve().parents[2] / "alembic.ini"


class MigrationMetadataError(ValueError):
    """The deployed migration scripts cannot establish an expected schema version."""


def expected_schema_heads() -> frozenset[str]:
    """Read migration heads without connecting to a database or running migrations."""
    try:
        config = Config(str(ALEMBIC_CONFIG))
        # This inspection already runs inside the application. Avoid repeatedly
        # prepending the server directory to sys.path on every readiness check.
        config.set_main_option("prepend_sys_path", "")
        scripts = ScriptDirectory.from_config(config)
        heads = set(scripts.get_heads())
        # A head referenced through depends_on is already covered by its
        # dependent revision, so it has no separate alembic_version row.
        # Use public APIs supported by the declared Alembic >=1.14 range.
        for revision in scripts.walk_revisions():
            dependencies = revision.dependencies
            if dependencies:
                if isinstance(dependencies, str):
                    dependencies = (dependencies,)
                for dependency in scripts.get_revisions(dependencies):
                    heads.discard(dependency.revision)
    except Exception as error:
        # Treat missing/broken migration metadata as an incomplete deployment.
        raise MigrationMetadataError("无法读取数据库迁移脚本，请检查部署文件是否完整") from error
    if not heads:
        raise MigrationMetadataError("数据库迁移脚本中没有可用的head版本")
    return frozenset(heads)
