"use client"

import { useState, useEffect } from "react"
import type { ReportData } from "@/types/report"
import type { Language } from "@/lib/translations"

function sanitizeReportData(report: ReportData): ReportData {
  const { currencies: _removedAllocation, ...portfolioAllocation } = report.portfolio_allocation as ReportData["portfolio_allocation"] & {
    currencies?: number
  }
  const { currencies: _removedSector, ...sectors } = report.sectors

  return {
    ...report,
    portfolio_allocation: portfolioAllocation,
    risk_adjusted_picks: report.risk_adjusted_picks.filter((pick) => pick.sector !== "currencies"),
    sectors,
  }
}

export function useReportData(lang: Language = "en") {
  const [data, setData] = useState<ReportData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)

    const file = lang === "es" ? "/data/report-es.json" : "/data/report.json"

    fetch(file)
      .then((res) => {
        if (!res.ok) {
          if (lang === "es" && res.status === 404) {
            // Fallback to English if Spanish file doesn't exist
            return fetch("/data/report.json").then((fallback) => {
              if (!fallback.ok) throw new Error(`Failed to load report data: ${fallback.status}`)
              return fallback.json()
            })
          }
          throw new Error(`Failed to load report data: ${res.status}`)
        }
        return res.json()
      })
        .then((report) => setData(sanitizeReportData(report)))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [lang])

  return { data, loading, error }
}
