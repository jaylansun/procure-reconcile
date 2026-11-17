"""接口返回的登录结果。"""

from datetime import datetime

from pydantic import BaseModel

from app.domain.vo.user_vo import UserVO


class LoginVO(BaseModel):
    """登录结果展示对象，返回会话令牌、有效期和用户资料。"""

    token: str
    expires_at: datetime
    user: UserVO
