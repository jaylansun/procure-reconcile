import hashlib
import json


def _validate_json(value:object,ancestors: set[int]) -> None:
    """递归检查数据类型，并检测循环引用。

        Args:
            value: 当前需要检查的数据。
            ancestors: 当前递归路径中的容器对象标识。

        Raises:
            ValueError: 数据类型、字典键或引用关系不符合要求。
    """
    # 基础类型可以直接参与 JSON 序列化。
    if value is None or isinstance(value,(str,int,bool)):
        return

    # 数量、金额使用十进制字符串，拒绝浮点数等类型。
    if not isinstance(value,(dict,list)):
        raise ValueError(
            "请求内容仅支持字典、列表、字符串、整数、布尔值和None"
        )

    # id() 用于识别当前容器，仅参与循环检查，不参与指纹计算。
    identity = id(value)
    if identity in ancestors:
        raise ValueError("请求内容不能包含循环引用")

    ancestors.add(identity)

    try:
        if isinstance(value,dict):
            for key,item in value.items():
                if not isinstance(key,str):
                    raise ValueError("请求内容的字典键必须是字符串")

                _validate_json(item,ancestors)
        else:
            for item in value:
                _validate_json(item,ancestors)
    finally:
        ancestors.remove(identity)

def command_fingerprint(
        operation : str,
        payload : dict[str,object],
)-> str:
    """根据业务操作名称和请求内容生成 SHA-256 指纹。

        字典字段顺序不影响结果，列表元素顺序参与计算。
        函数不会修改传入的数据。

        Args:
            operation: 非空白的业务操作名称，原样参与计算。
            payload: 已校验的业务请求字典，数量和金额使用规范字符串。

        Returns:
            64个字符的小写十六进制指纹。

        Raises:
            ValueError: 输入类型不支持、存在循环引用，或无法序列化。

        Note:
            command_id 由调用方单独保存，不放入 payload。
            本函数会计算所有传入字段，不会自动忽略字段。
            指纹计算还需要结合数据库记录和事务，才能实现业务幂等。
        """
    # strip() 只用于判断是否为空白，不改变实际操作名称。
    if not isinstance(operation,str) or not operation.strip():
        raise ValueError("业务操作名称必须是非空白字符串")

    if not isinstance(payload,dict):
        raise ValueError("业务请求内容必须是字典")

    try:
        _validate_json(payload,set())

        # 固定字段顺序和分隔格式，生成稳定的 JSON 字符串。
        json_string = json.dumps(
            {
                "operation":operation,
                "payload":payload
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=None,
            separators=(',',':'),
        )

        return hashlib.sha256(json_string.encode('utf-8')).hexdigest()
    except(RecursionError,UnicodeError) as error:
        raise ValueError(
            "请求内容无法转换为有效的UTF-8 JSON"
        ) from error
