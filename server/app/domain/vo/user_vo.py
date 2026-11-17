"""接口返回的管理员资料。"""

from pydantic import BaseModel


class UserVO(BaseModel):
    """用户展示对象，作为接口响应中的用户资料。"""

    username: str
