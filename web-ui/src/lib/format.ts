// 关注列表与搜索页共用的展示格式化工具

/** 价格数值展示；值为 null/undefined 时回退到 fallback 文案 */
export function formatPrice(value: number | null | undefined, fallback = '--'): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return fallback
  }
  return `¥${Number(value).toFixed(2)}`
}
