"""校验接口数据模型命名，以及实际接口与存档契约的一致性。"""

import json
from pathlib import Path

from app.config.settings import Settings
from app.main import create_app
from app.persistence.model_registry import BaseDO


def test_api_models_use_data_role_suffixes():
    """业务接口模型的名称直接表达查询、输入传输或返回展示职责。"""
    application = create_app(Settings(database_url=None))
    generated = application.openapi()["components"]["schemas"]
    framework_models = {"HTTPValidationError", "ValidationError"}
    for name in generated.keys() - framework_models:
        assert name.endswith(("QO", "DTO", "VO")), name


def test_saved_openapi_matches_application():
    """检验存档组件及其引用与真实路由保持一致。"""
    contract = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"
    expected = json.loads(contract.read_text(encoding="utf-8"))
    application = create_app(Settings(database_url=None))
    assert application.openapi() == expected


def test_database_models_use_do_suffix():
    """所有实际注册的数据库映射类统一使用DO后缀。"""
    for mapper in BaseDO.registry.mappers:
        assert mapper.class_.__name__.endswith("DO"), mapper.class_.__name__
