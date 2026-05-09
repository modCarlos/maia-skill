"use client"

import { useState, useEffect, useCallback } from "react"

export interface PortfolioEntry {
  id: string
  symbol: string
  name: string
  sector: string
  buyPrice: number
  quantity: number
  buyDate: string // ISO 8601
}

async function fetchPortfolio(): Promise<PortfolioEntry[]> {
  try {
    const res = await fetch("/api/portfolio", { cache: "no-store" })
    if (!res.ok) return []
    return res.json()
  } catch {
    return []
  }
}

async function persistPortfolio(entries: PortfolioEntry[]) {
  try {
    await fetch("/api/portfolio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entries),
    })
  } catch {
    // fail silently — data already in React state
  }
}

export function usePortfolio() {
  const [entries, setEntries] = useState<PortfolioEntry[]>([])

  // Load from server on mount
  useEffect(() => {
    fetchPortfolio().then(setEntries)
  }, [])

  const addEntry = useCallback(
    (
      pick: { symbol: string; name: string; sector: string },
      buyPrice: number,
      quantity: number
    ) => {
      const newEntry: PortfolioEntry = {
        id: `${pick.symbol}-${Date.now()}`,
        symbol: pick.symbol,
        name: pick.name,
        sector: pick.sector,
        buyPrice,
        quantity,
        buyDate: new Date().toISOString(),
      }
      setEntries((prev) => {
        const updated = [...prev, newEntry]
        persistPortfolio(updated)
        return updated
      })
    },
    []
  )

  const removeEntry = useCallback((id: string) => {
    setEntries((prev) => {
      const updated = prev.filter((e) => e.id !== id)
      persistPortfolio(updated)
      return updated
    })
  }, [])

  const clearAll = useCallback(() => {
    setEntries([])
    persistPortfolio([])
  }, [])

  const hasSymbol = useCallback(
    (symbol: string) => entries.some((e) => e.symbol === symbol),
    [entries]
  )

  const countForSymbol = useCallback(
    (symbol: string) =>
      entries
        .filter((e) => e.symbol === symbol)
        .reduce((sum, e) => sum + e.quantity, 0),
    [entries]
  )

  return { entries, addEntry, removeEntry, clearAll, hasSymbol, countForSymbol }
}
