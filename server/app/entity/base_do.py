"""数据库映射对象共用的声明式基类。"""

from sqlalchemy.orm import DeclarativeBase


class BaseDO(DeclarativeBase):
    """数据库持久化对象的公共映射基类。"""

    pass
