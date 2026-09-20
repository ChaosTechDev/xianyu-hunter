"""
AI 分析服务
封装 AI 分析相关的业务逻辑
"""
from typing import Dict, List, Optional
from src.infrastructure.external.ai_client import AIClient


class AIAnalysisService:
    """AI 分析服务"""

    def __init__(self, ai_client: AIClient):
        self.ai_client = ai_client

    async def analyze_product(
        self,
        product_data: Dict,
        image_paths: List[str],
        prompt_text: str
    ) -> Optional[Dict]:
        """
        分析商品

        Args:
            product_data: 商品数据
            image_paths: 图片路径列表
            prompt_text: 分析提示词

        Returns:
            分析结果
        """
        if not self.ai_client.is_available():
            print("AI 客户端不可用，跳过分析")
            return None

        try:
            result = await self.ai_client.analyze(product_data, image_paths, prompt_text)

            if result and self._validate_result(result):
                return result
            else:
                print("AI 分析结果验证失败")
                return None
        except Exception as e:
            print(f"AI 分析服务出错: {e}")
            return None

    def _validate_result(self, result: Dict) -> bool:
        """验证 AI 分析结果的格式"""
        required_fields = [
            "prompt_version",
            "is_recommended",
            "reason",
            "risk_tags",
            "criteria_analysis"
        ]

        # 检查必需字段
        for field in required_fields:
            if field not in result:
                print(f"AI 响应缺少必需字段: {field}")
                return False

        # 检查数据类型
        if not isinstance(result.get("is_recommended"), bool):
            print("is_recommended 字段不是布尔类型")
            return False

        if not isinstance(result.get("risk_tags"), list):
            print("risk_tags 字段不是列表类型")
            return False

        criteria_analysis = result.get("criteria_analysis", {})
        if not isinstance(criteria_analysis, dict) or not criteria_analysis:
            print("criteria_analysis 必须是非空字典")
            return False

        return True
