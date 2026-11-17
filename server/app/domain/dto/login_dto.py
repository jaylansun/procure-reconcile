"""管理员登录请求的数据传输对象。"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class LoginDTO(BaseModel):
    """登录请求传输对象，接收用户名、密码和设备名称。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=128)

    @field_validator("username", "device_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Value cannot be empty or contain control characters")
        return value
