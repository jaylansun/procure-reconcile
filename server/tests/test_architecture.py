"""检查后端分层、文件命名和数据库模型注册约定。"""

import ast
import importlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SERVER_ROOT / "app"


def _module_name(class_name: str) -> str:
    """将类名转换成文件名，连续大写的 DO、DTO 等后缀作为一个单词。"""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words).lower()


def _modules(directory: str) -> list[Path]:
    """获取指定分层下的实现文件，包初始化文件单独组织。"""
    return sorted(
        path for path in (APP_ROOT / directory).rglob("*.py") if path.name != "__init__.py"
    )


def _public_classes(path: Path) -> list[ast.ClassDef]:
    """读取文件中公开的顶层类，允许实现细节使用私有辅助类。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]


@pytest.mark.parametrize(
    ("directory", "suffix"),
    [
        ("entity", "DO"),
        ("domain/dto", "DTO"),
        ("domain/qo", "QO"),
        ("domain/vo", "VO"),
        ("service", "Service"),
        ("repository", "Repository"),
    ],
)
def test_layer_classes_have_matching_names_and_individual_files(
    directory: str, suffix: str
) -> None:
    """每个数据对象、业务服务及数据访问类的文件都直接表达其职责。"""
    modules = _modules(directory)
    assert modules, f"分层目录应包含实现文件：{directory}"
    for path in modules:
        classes = _public_classes(path)
        assert len(classes) == 1, f"每个实现文件应包含一个公共类：{path.relative_to(SERVER_ROOT)}"
        name = classes[0].name
        assert name.endswith(suffix), f"{name} 应使用 {suffix} 后缀"
        assert path.stem == _module_name(name), f"{name} 的文件名应为 {_module_name(name)}.py"


def _imported_modules(path: Path) -> Iterator[str]:
    """展开绝对及相对导入，供分层依赖检查使用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.relative_to(SERVER_ROOT).parent.parts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*parts, node.module)) if node.module else ".".join(parts)
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)


@pytest.mark.parametrize("directory", ["service", "repository"])
def test_service_and_repository_are_independent_of_http_layer(directory: str) -> None:
    """业务和数据访问层可脱离 FastAPI 接口执行和测试。"""
    for path in _modules(directory):
        for module in _imported_modules(path):
            for forbidden in ("fastapi", "app.controller"):
                assert module != forbidden and not module.startswith(f"{forbidden}."), (
                    f"{path.relative_to(SERVER_ROOT)} 引用了 HTTP 层：{module}"
                )


def test_entities_share_one_base_and_are_explicitly_registered() -> None:
    """统一模型基类、映射注册和迁移元数据包含所有拆分的数据库对象。"""
    from app.persistence import model_registry
    from app.persistence.model_registry import BaseDO

    tree = ast.parse((APP_ROOT / "persistence/model_registry.py").read_text(encoding="utf-8"))
    explicit_imports = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    entities = set()
    for path in _modules("entity"):
        name = _public_classes(path)[0].name
        module_name = ".".join(path.relative_to(SERVER_ROOT).with_suffix("").parts)
        assert (module_name, name) in explicit_imports, f"模型注册入口应显式导入 {name}"
        entity = getattr(importlib.import_module(module_name), name)
        assert issubclass(entity, BaseDO), name
        assert entity.metadata is BaseDO.metadata, name
        assert entity.registry is BaseDO.registry, name
        if entity is not BaseDO:
            entities.add(entity)

    assert model_registry.metadata is BaseDO.metadata
    assert {mapper.class_ for mapper in BaseDO.registry.mappers} == entities
    assert set(model_registry.metadata.tables) == {entity.__tablename__ for entity in entities}
    assert {
        "administrators",
        "auth_sessions",
        "command_records",
        "audit_events",
        "stored_files",
        "suppliers",
    } <= model_registry.metadata.tables.keys()
    for table in model_registry.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.metadata is model_registry.metadata


def test_database_entrypoint_registers_all_models_in_a_fresh_process() -> None:
    """独立导入数据库入口也会加载完整元数据，避免依赖其他接口的导入顺序。"""
    from app.persistence import model_registry

    script = dedent(
        """
        import json
        import sys

        from app.persistence.database import DatabaseManager

        database = DatabaseManager(None)
        try:
            assert "app.persistence.model_registry" in sys.modules
            base = sys.modules["app.entity.base_do"].BaseDO
            print(json.dumps(sorted(base.metadata.tables)))
        finally:
            database.close()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=SERVER_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == sorted(model_registry.metadata.tables)
