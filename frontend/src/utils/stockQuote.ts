// Every refresh replaces the entire observation. Missing is not zero or old data.
export function stockQuoteView(d: Record<string, any> = {}) {
  const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v : NaN
  return {
    price: number(d.price ?? d.close ?? d.last_price), changePercent: number(d.change_percent ?? d.pct_chg),
    open: number(d.open), high: number(d.high), low: number(d.low), prevClose: number(d.prev_close ?? d.pre_close),
    volume: number(d.volume), amount: number(d.amount), turnover: number(d.turnover_rate), amplitude: number(d.amplitude),
    tradeDate: d.trade_date || null, turnoverDate: d.trade_date || null, amplitudeDate: d.trade_date || null,
    updatedAt: d.updated_at || null, observedAt: d.quote_observed_at || null,
    source: d.data_source || null, kind: d.quote_kind || null
  }
}
