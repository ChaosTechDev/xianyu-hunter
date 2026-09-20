import { http } from '@/lib/http'
import type { OfficialSearchResult } from '@/types/search.d.ts'

export interface SearchItemsParams {
  keyword: string
  /** 登录态文件路径；缺省或文件不存在时后端自动回退匿名搜索 */
  accountPath: string
  page?: number
  pageSize?: number
}

/** 调用官方闲鱼搜索接口抓取实时商品列表 */
export async function searchOfficialItems(params: SearchItemsParams): Promise<OfficialSearchResult> {
  return await http('/api/search/items', {
    params: {
      keyword: params.keyword,
      account_path: params.accountPath,
      page: params.page ?? 1,
      page_size: params.pageSize ?? 30,
    },
  })
}
