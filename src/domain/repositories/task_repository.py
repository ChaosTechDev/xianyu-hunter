"""
任务仓储层
负责任务数据的持久化操作
"""
from typing import List, Optional
from abc import ABC, abstractmethod
import json
import aiofiles
from src.domain.models.task import Task


class TaskRepository(ABC):
    """任务仓储接口"""

    @abstractmethod
    async def find_all(self) -> List[Task]:
        """获取所有任务"""
        pass

    @abstractmethod
    async def find_by_id(self, task_id: int) -> Optional[Task]:
        """根据ID获取任务"""
        pass

    @abstractmethod
    async def save(self, task: Task) -> Task:
        """保存任务（创建或更新）"""
        pass

    @abstractmethod
    async def delete(self, task_id: int) -> bool:
        """删除任务"""
        pass
