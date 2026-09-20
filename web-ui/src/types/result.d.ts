// Based on the data structure from web_server.py and scraper.py

export interface ProductInfo {
  "商品标题": string;
  "当前售价": string;
  "商品原价"?: string;
  "“想要”人数"?: string | number;
  "商品标签"?: string[];
  "发货地区"?: string;
  "卖家昵称"?: string;
  "商品链接": string;
  "发布时间"?: string;
  "商品ID": string;
  "商品图片列表"?: string[];
  "商品主图链接"?: string;
  "浏览量"?: string | number;
}

export interface SellerInfo {
  "卖家昵称"?: string;
  "卖家头像链接"?: string;
  "卖家个性签名"?: string;
  "卖家在售/已售商品数"?: string;
  "卖家收到的评价总数"?: string;
  "卖家信用等级"?: string;
  "买家信用等级"?: string;
  "卖家芝麻信用"?: string;
  "卖家注册时长"?: string;
  "作为卖家的好评数"?: string;
  "作为卖家的好评率"?: string;
  "作为买家的好评数"?: string;
  "作为买家的好评率"?: string;
  "卖家发布的商品列表"?: any[]; // Define more strictly if needed
  "卖家收到的评价列表"?: any[]; // Define more strictly if needed
}

export interface AiAnalysis {
  is_recommended: boolean;
  reason: string;
  analysis_source?: 'ai' | 'keyword';
  keyword_hit_count?: number;
  value_score?: number;
  value_summary?: string;
  prompt_version?: string;
  risk_tags?: string[];
  criteria_analysis?: Record<string, any>;
  matched_keywords?: string[];
  error?: string;
}

export interface PriceInsight {
  observation_count: number;
  current_price?: number | null;
  avg_price?: number | null;
  median_price?: number | null;
  min_price?: number | null;
  max_price?: number | null;
  market_avg_price?: number | null;
  market_median_price?: number | null;
  price_change_amount?: number | null;
  price_change_percent?: number | null;
  deal_score?: number | null;
  deal_label?: string;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
}

export interface ResultInsights {
  market_summary: {
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
    snapshot_time?: string | null;
  };
  history_summary: {
    unique_items: number;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  };
  daily_trend: Array<{
    day: string;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  }>;
  latest_snapshot_at?: string | null;
}

export interface ScoreComponent {
  score: number;
  weight: number;
  weighted: number;
  /** AI 维度附带：模型给出的原始分与置信度 */
  raw_score?: number;
  confidence?: number;
  /** 关键词维度附带：命中数量 */
  hit_count?: number;
  /** 价格维度附带：当前价 / 参考价 */
  price_ratio?: number;
}

/**
 * 后端融合评分（src/services/analysis_scoring_bridge.py 产出）。
 *
 * 与 AiAnalysis.value_score 的区别：value_score 是可选的、由模型自由给出的数字，
 * 经常缺失（缺失时前端会退化成显示 0%）；本字段是后端**确定性计算**的结果，
 * 只要拿到 AI 结论、关键词命中或价格依据中的任意一项就会存在。
 */
export interface FusedScore {
  score: number;
  components: Record<string, ScoreComponent>;
  risk_penalty: number;
  /** 是否缺少价格依据而降级为 AI + 关键词两维度 */
  degraded: boolean;
  /** 是否触发一票否决（硬性条件不满足），此时分数被强制封顶 */
  vetoed: boolean;
  /** 触发否决的评估项名，如 model_chip、shipping */
  ai_failures?: string[];
  /** 需人工确认的评估项（信息缺失，不等同于不符合） */
  ai_unknowns?: string[];
  available_dimensions: string[];
}

/**
 * 卖家信用评分（src/services/seller_credit_service.py 产出）。
 *
 * ``score`` 可能是 ``null``：卖家信息不足时后端**不猜分数**，而不是给 0。
 * 前端必须把这种情况显示成「未评分」——「没数据」与「信用很差」是两件事，
 * 把前者渲染成 0 分会误伤正常卖家。
 */
export interface SellerCredit {
  /** 0-100；信息不足时为 null（不要当成 0） */
  score: number | null;
  /** 机器可读等级，如 excellent / good */
  level?: string;
  /** 中文等级标签，如「信用优秀」 */
  level_label?: string;
  /** 是否触发信用否决（存在明确的差评类硬信号） */
  vetoed?: boolean;
  veto_reason?: string | null;
  /** 风险标签，如「描述不符」「低好评率」 */
  risk_flags?: string[];
  /** 参与计算的维度 */
  available_factors?: string[];
  /** 因信息缺失未参与的维度（缺失 ≠ 负面） */
  unknown_factors?: string[];
}

export interface ResultItem {
  "爬取时间": string;
  "搜索关键字": string;
  "任务名称": string;
  "商品信息": ProductInfo;
  "卖家信息": SellerInfo;
  ai_analysis: AiAnalysis;
  /** 后端融合评分；旧数据可能没有该字段 */
  评分?: FusedScore;
  /** 卖家信用评分；旧数据可能没有该字段 */
  卖家信用评分?: SellerCredit;
  price_insight?: PriceInsight;
  _status?: 'active' | 'hidden' | 'expired';
  _effective_hidden?: boolean;
  _hidden_reason?: 'manual' | 'rule' | 'expired' | null;
  _matched_blacklist_keywords?: string[];
}
