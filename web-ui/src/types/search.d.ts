// 与后端 src/services/official_search_service.py 的 _normalize_item 返回结构对齐

export interface OfficialSearchItem {
  item_id: string
  title: string
  /** 解析后的数值价格，可能为 null */
  price: number | null
  /** 平台原始价格文本，例如 "¥1,299" */
  price_display: string
  original_price: string
  seller: string
  region: string
  publish_time: string
  link: string
  image_url: string | null
  tags: string[]
}

export interface OfficialSearchResult {
  items: OfficialSearchItem[]
  page: number
  page_size: number
  has_more: boolean
  /** 实际请求使用的登录态文件路径；匿名搜索时为传入的原值 */
  account_path: string
}
