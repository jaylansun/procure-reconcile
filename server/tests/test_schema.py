import sys

import pytest

from app.persistence import schema


@pytest.fixture
def migration_tree(tmp_path, monkeypatch):
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    config = tmp_path / "alembic.ini"
    config.write_text(
        "[alembic]\nscript_location = %(here)s/migrations\nprepend_sys_path = %(here)s\n"
    )
    monkeypatch.setattr(schema, "ALEMBIC_CONFIG", config)
    return versions


def write_revision(versions, revision, parent=None, dependencies=None):
    (versions / f"{revision}.py").write_text(
        f"revision = {revision!r}\n"
        f"down_revision = {parent!r}\n"
        "branch_labels = None\n"
        f"depends_on = {dependencies!r}\n"
        "def upgrade():\n    pass\n"
        "def downgrade():\n    pass\n"
    )


def test_heads_follow_new_scripts_without_changing_cwd_or_python_path(
    migration_tree, tmp_path, monkeypatch
):
    write_revision(migration_tree, "initial")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    original_path = sys.path.copy()
    assert schema.expected_schema_heads() == {"initial"}
    write_revision(migration_tree, "next_revision", "initial")
    assert schema.expected_schema_heads() == {"next_revision"}
    assert sys.path == original_path


def test_heads_cover_branches_and_follow_a_merge(migration_tree):
    write_revision(migration_tree, "initial")
    write_revision(migration_tree, "left", "initial")
    write_revision(migration_tree, "right", "initial")
    assert schema.expected_schema_heads() == {"left", "right"}
    write_revision(migration_tree, "merged", ("left", "right"))
    assert schema.expected_schema_heads() == {"merged"}


def test_dependency_heads_do_not_require_extra_database_version_rows(migration_tree):
    write_revision(migration_tree, "initial")
    write_revision(migration_tree, "left", "initial")
    write_revision(migration_tree, "right", "initial", dependencies="left")
    assert schema.expected_schema_heads() == {"right"}


@pytest.mark.parametrize(
    "failure", ["missing_config", "missing_directory", "no_heads", "broken_script"]
)
def test_unavailable_migration_metadata_is_rejected(migration_tree, monkeypatch, failure):
    if failure == "missing_config":
        monkeypatch.setattr(schema, "ALEMBIC_CONFIG", migration_tree / "missing.ini")
    elif failure == "missing_directory":
        migration_tree.rmdir()
        migration_tree.parent.rmdir()
    elif failure == "broken_script":
        (migration_tree / "broken.py").write_text("this is not valid Python !")
    with pytest.raises(schema.MigrationMetadataError):
        schema.expected_schema_heads()
