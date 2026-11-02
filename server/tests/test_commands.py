"""验证业务请求指纹的公开契约，不连接数据库或启动服务。"""

import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from app.services.commands import command_fingerprint


@pytest.fixture
def business_payload() -> dict[str, object]:
    """为每个测试提供独立的业务内容，避免用例间共享可变数据。"""
    return {
        "source_id": "receipt-1",
        "quantity": "1.000000",
        "expected_version": 1,
        "lines": ["物料甲", "物料乙"],
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"中文": "供应商", "values": [{}, [], "", 2**80, -1, True, False, None]}],
)
def test_supported_payloads_have_stable_lowercase_sha256_fingerprints(
    payload: dict[str, object],
) -> None:
    """空字典、中文和允许的JSON类型均能产生稳定的64位小写指纹。"""
    fingerprint = command_fingerprint("收货登记", payload)
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert fingerprint == command_fingerprint("收货登记", payload)


def test_nested_dictionary_key_order_does_not_change_fingerprint() -> None:
    """根字典、列表内字典及更深字典的键顺序都不影响结果。"""
    first: dict[str, object] = {
        "supplier": "A",
        "lines": [{"quantity": "1.000000", "item": {"code": "B", "version": 1}}],
    }
    reordered: dict[str, object] = {
        "lines": [{"item": {"version": 1, "code": "B"}, "quantity": "1.000000"}],
        "supplier": "A",
    }
    assert command_fingerprint("receipt.register", first) == command_fingerprint(
        "receipt.register", reordered
    )


@pytest.mark.parametrize("operation", ["receipt.correct", " receipt.register", "receipt.register "])
def test_operation_is_included_without_trimming(
    business_payload: dict[str, object], operation: str
) -> None:
    """操作名称参与指纹，合法名称两端的空格也按原样参与计算。"""
    assert command_fingerprint(operation, business_payload) != command_fingerprint(
        "receipt.register", business_payload
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("source_id", "receipt-2"), ("quantity", "2.000000"), ("expected_version", 2)],
)
def test_changed_business_fields_change_fingerprint(
    business_payload: dict[str, object], field: str, value: object
) -> None:
    """来源、数量或预期版本变化后，旧请求指纹不能继续复用。"""
    changed = {**business_payload, field: value}
    assert command_fingerprint("receipt.register", business_payload) != command_fingerprint(
        "receipt.register", changed
    )


def test_list_order_is_preserved(business_payload: dict[str, object]) -> None:
    """列表元素不能为了指纹稳定而排序，明细顺序变化应改变指纹。"""
    reordered = {**business_payload, "lines": ["物料乙", "物料甲"]}
    assert command_fingerprint("receipt.register", business_payload) != command_fingerprint(
        "receipt.register", reordered
    )


def test_boolean_integer_string_and_none_remain_distinct() -> None:
    """布尔、整数、字符串和空值保持各自含义，不能通过隐式转换合并。"""
    values = (True, 1, "1", False, 0, "0", None, "null")
    fingerprints = {command_fingerprint("action", {"value": value}) for value in values}
    assert len(fingerprints) == len(values)


def test_no_field_is_silently_ignored_even_if_named_command_id() -> None:
    """调用方应将命令ID放在内容外，函数本身不擅自过滤已传入的字段。"""
    without_id = command_fingerprint("action", {"quantity": "1"})
    first_id = command_fingerprint("action", {"quantity": "1", "command_id": "first"})
    second_id = command_fingerprint("action", {"quantity": "1", "command_id": "second"})
    assert len({without_id, first_id, second_id}) == 3


@pytest.mark.parametrize("operation", ["", " \t\n", None, 1, False, []])
def test_blank_or_nonstring_operations_are_rejected(operation: object) -> None:
    """运行时收到空白或错误类型的操作名称时，统一拒绝为ValueError。"""
    with pytest.raises(ValueError):
        # cast只用于故意构造违反类型签名的输入，不会转换运行时的值。
        command_fingerprint(cast(str, operation), {})


@pytest.mark.parametrize("payload", [None, [], "text", 1, True, ("value",)])
def test_top_level_payload_must_be_a_dictionary(payload: object) -> None:
    """即使某个值允许出现在内部，根内容仍必须是字典。"""
    with pytest.raises(ValueError):
        command_fingerprint("action", cast(dict[str, object], payload))


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("1.000000"),
        date(2026, 11, 2),
        ("item",),
        {"item"},
        b"item",
        object(),
    ],
)
def test_unsupported_nested_values_are_rejected(value: object) -> None:
    """递归进入列表和字典后，浮点及其他非约定类型仍须被拒绝。"""
    with pytest.raises(ValueError):
        command_fingerprint("action", {"lines": [{"nested": value}]})


@pytest.mark.parametrize("key", [1, False, None, ("key",)])
@pytest.mark.parametrize("nested", [False, True])
def test_dictionary_keys_must_be_strings_at_every_depth(key: object, nested: bool) -> None:
    """根字典与嵌套字典都不能将非字符串键悄悄转换成字符串。"""
    invalid = {key: "value"}
    payload = {"children": [invalid]} if nested else invalid
    with pytest.raises(ValueError):
        command_fingerprint("action", cast(dict[str, object], payload))


@pytest.mark.parametrize("cycle_kind", ["dictionary", "list", "indirect"])
def test_circular_references_raise_value_error(cycle_kind: str) -> None:
    """直接字典循环、列表循环及间接循环都应得到可处理的输入错误。"""
    payload: dict[str, object] = {}
    if cycle_kind == "dictionary":
        payload["self"] = payload
    elif cycle_kind == "list":
        values: list[object] = []
        values.append(values)
        payload["values"] = values
    else:
        payload["child"] = {"parent": payload}
    with pytest.raises(ValueError):
        command_fingerprint("action", payload)


@pytest.mark.parametrize("shared", [{"quantity": "1"}, ["物料甲", {"quantity": "1"}]])
def test_shared_noncyclic_objects_are_allowed(shared: object) -> None:
    """同一个子对象出现在不同分支时，与两份内容相同的对象得到相同指纹。"""
    aliased: dict[str, object] = {"first": shared, "second": shared}
    duplicated: dict[str, object] = {"first": deepcopy(shared), "second": deepcopy(shared)}
    assert command_fingerprint("action", aliased) == command_fingerprint("action", duplicated)


def test_fingerprinting_does_not_mutate_input_or_nested_objects() -> None:
    """计算后内容、字典插入顺序及已有子对象引用均保持不变。"""
    item: dict[str, object] = {"z": 1, "a": "中文"}
    values: list[object] = [item, "B"]
    payload: dict[str, object] = {"z": values, "a": False}
    before = deepcopy(payload)
    outer_keys, inner_keys = list(payload), list(item)
    command_fingerprint("action", payload)
    assert payload == before
    assert list(payload) == outer_keys
    assert list(item) == inner_keys
    assert payload["z"] is values
    assert values[0] is item


@pytest.mark.parametrize("location", ["operation", "key", "value"])
def test_invalid_unicode_is_rejected(location: str) -> None:
    """无法编码成UTF-8的孤立代理字符不能产生看似有效的业务指纹。"""
    invalid = "\ud800"
    operation = invalid if location == "operation" else "action"
    payload = (
        {invalid: "value"}
        if location == "key"
        else {"value": invalid if location == "value" else "valid"}
    )
    with pytest.raises(ValueError):
        command_fingerprint(operation, payload)


def test_excessive_nesting_is_reported_as_value_error() -> None:
    """超出运行时递归能力的输入统一拒绝，不向调用方泄漏RecursionError。"""
    nested: list[object] = []
    for _ in range(sys.getrecursionlimit() + 100):
        nested = [nested]
    with pytest.raises(ValueError):
        command_fingerprint("action", {"nested": nested})


def test_fingerprint_is_stable_across_processes_and_hash_seeds() -> None:
    """不同Python进程和哈希随机种子不会改变同一份业务内容的指纹。"""
    payload: dict[str, object] = {
        "supplier": "供方甲",
        "quantity": "1.250000",
        "labels": [True, None, 3],
    }
    expected = command_fingerprint("收货.登记", payload)
    program = """
from app.services.commands import command_fingerprint
values = {"supplier": "供方甲", "quantity": "1.250000", "labels": [True, None, 3]}
payload = {key: values[key] for key in set(values)}
print(command_fingerprint("收货.登记", payload))
"""
    for seed in ("1", "77"):
        environment = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        )
        assert result.stdout.strip() == expected
