/** Format only explicit structured evidence; never mine arbitrary narrative numbers. */
export function priceRange(value: unknown): string | null {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return String(value)
  if (typeof value === 'string' && value.trim()) return value.trim()
  if (Array.isArray(value) && value.length === 2 && value.every(v => typeof v === 'number' && Number.isFinite(v) && v > 0) && value[0] <= value[1]) {
    return `${value[0]}–${value[1]}`
  }
  return null
}

export function pricePlans(value: unknown) {
  if (!Array.isArray(value)) return []
  return value.filter(p => p && typeof p === 'object' && !Array.isArray(p)).map((p, i) => ({
    name: typeof p.name === 'string' ? p.name : `条件方案 ${i + 1}`,
    range: priceRange(p.reference_range_cny ?? p.entry_reference_range ?? p.entry_reference),
    unit: p.reference_range_cny != null ? '人民币元/股' : '',
    conditions: Array.isArray(p.conditions) ? p.conditions.join('；') : String(p.conditions ?? p.condition ?? p.trigger ?? '见原始条件说明'),
    status: typeof p.status === 'string' ? p.status : '研究参考，不等于当前买入授权'
  }))
}

export function observedPrice(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const p = value as Record<string, unknown>
  if (typeof p.price !== 'number' || !Number.isFinite(p.price) || p.price <= 0 || p.currency !== 'CNY' || !p.data_source || !p.trade_date) return null
  return {
    price: p.price.toLocaleString('zh-CN', { maximumFractionDigits: 4 }),
    date: String(p.observed_at || p.trade_date), source: String(p.data_source),
    basis: p.price_basis === 'raw' ? '不复权' : '口径未明确',
    label: p.session === 'post_close' ? '盘后报价快照，非官方收盘确认' : '供应商报价快照，非成交保证'
  }
}
