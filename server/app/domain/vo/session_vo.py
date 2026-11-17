"""接口返回的当前登录会话。"""

from datetime import datetime

from pydantic import BaseModel


class SessionVO(BaseModel):
    """会话展示对象，返回当前登录用户和到期时间。"""

    username: str
    expires_at: datetime
