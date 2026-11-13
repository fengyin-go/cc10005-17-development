import { useEffect, useState } from 'react'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; key?: string; created: number; pending: number; abnormal: number }[]
}

const fallbackCards = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
const fallbackModules = [
  {"name": "站点管理", "key": "site", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "抽取模板", "key": "template", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "代理池管理", "key": "proxy", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "采集任务", "key": "spider", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "调度策略", "key": "schedule", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "反爬对抗", "key": "antibot", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "限速控制", "key": "ratelimit", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "失败重试", "key": "retry", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "字段映射", "key": "extractor", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "采集流水线", "key": "pipeline", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "结果校验", "key": "validator", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "数据落盘", "key": "storage", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "采集监控", "key": "monitor", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "数据清洗", "key": "cleanse", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "增量采集", "key": "delta", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "数据分发", "key": "export", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "RPA流程", "key": "rpaflow", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "RPA检查点", "key": "rpa_checkpoint", "created": 0, "pending": 0, "abnormal": 0},
  {"name": "登录凭证", "key": "auth", "created": 0, "pending": 0, "abnormal": 0},
]

export default function Overview() {
  const [cards, setCards] = useState<Overview['cards']>(fallbackCards)
  const [moduleRows, setModuleRows] = useState<Overview['modules']>(fallbackModules)

  useEffect(() => {
    fetchJson<Overview>('/api/overview')
      .then((payload) => {
        setCards(payload.cards)
        setModuleRows(payload.modules)
      })
      .catch(() => undefined)
  }, [])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
