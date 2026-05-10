export interface ReportData {
  brand: string
  creator: string
  generated_at: string
  risk_profile: "conservative" | "moderate" | "aggressive"
  executive_summary: string
  macro_environment: MacroEnvironment
  portfolio_allocation: PortfolioAllocation
  cross_sector_insights: CrossSectorInsight[]
  risk_adjusted_picks: RiskAdjustedPick[]
  historical_accuracy: HistoricalAccuracy
  warnings: string[]
  sectors: Record<string, SectorData>
}

export interface MacroEnvironment {
  summary: string
  interest_rate_outlook: "rising" | "stable" | "falling"
  inflation_outlook: "rising" | "stable" | "falling"
  geopolitical_risk: "high" | "medium" | "low"
  key_factors: string[]
}

export type PortfolioAllocation = Record<string, number>

export interface CrossSectorInsight {
  insight: string
  implication: string
}

export interface RiskAdjustedPick {
  rank: number
  name: string
  symbol: string
  sector: string
  confidence: number
  risk_score: number
  risk_adjusted_score: number
  recommendation: "buy" | "hold" | "sell"
  reasoning: string
  position_size: string
}

export interface HistoricalAccuracy {
  previous_date: string | null
  calls_made: number
  calls_correct: number
  accuracy_pct: number
  notable: string
}

export interface SectorData {
  sector: string
  timestamp: string
  assets: Asset[]
  sector_summary: string
  sector_outlook: "bullish" | "bearish" | "neutral"
  top_pick: string
  top_pick_reasoning: string
  data_unavailable?: boolean
}

export interface Asset {
  name: string
  symbol: string
  current_price: string
  change_24h: string
  change_7d: string
  change_30d: string
  ytd_change: string
  week_52_high: string
  week_52_low: string
  market_cap: string
  volume_24h: string
  sentiment: "bullish" | "bearish" | "neutral"
  social_sentiment: "bullish" | "bearish" | "neutral" | "mixed"
  social_buzz: "high" | "medium" | "low"
  confidence: number
  source_agreement: "high" | "medium" | "low"
  sources_checked: string[]
  key_news: string[]
  social_highlights: string[]
  recommendation: "buy" | "hold" | "sell"
  reasoning: string
  // Optional, hydrated client-side from portfolio_market.json when available
  piotroski_score?: number | null
  piotroski_strength?: "strong" | "neutral" | "weak" | null
}
