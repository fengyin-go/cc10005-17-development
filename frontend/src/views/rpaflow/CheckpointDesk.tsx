import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rpa-checkpoint'
const UNRESOLVED = ['待核对', '待补记', '已阻断']

type Row = Record<string, any>
type Notice = { ok: boolean; text: string }

type Desk = {
  items: Row[]
  total: number
  cards: { label: string; value: number }[]
  leases: Row[]
}

type Card = Row & {
  seq: number
  step_status: string
}

type Detail = {
  checkpoint: Row
  cards: Card[]
  current_card: Card | null
  lease: Row | null
  page: { 页面编号: string; 快照: Row; 审计: Row[] }
  revisions: Row[]
  stale_receipts: Row[]
}

const KIND_OPTIONS = ['认证', '填表', '确认', '观察']
const ACTION_OPTIONS = [
  { value: 'login', label: '登录' },
  { value: 'fill', label: '填表' },
  { value: 'confirm', label: '确认' },
  { value: 'observe', label: '观察' },
  { value: 'cancel', label: '作废回执' },
]
const WAIT_OPTIONS = ['元素出现', '文本包含', '回执编号生成', '固定等待']
const SIMULATE_OPTIONS = [
  { value: '', label: '正常回执' },
  { value: '无回执', label: '已发出但无回执' },
  { value: '未送达', label: '未送达' },
]

function statusClass(status: string): string {
  if (UNRESOLVED.includes(status)) return 'chip warn'
  if (status === '已完成') return 'chip ok'
  if (status === '已回滚' || status === '已跳过') return 'chip muted'
  return 'chip'
}

function actionLabel(action: string): string {
  return ACTION_OPTIONS.find((item) => item.value === action)?.label ?? action
}

async function call(path: string, body?: Row): Promise<Row> {
  const response = await request(path, body ? { method: 'POST', body: JSON.stringify(body) } : undefined)
  const payload = (await response.json().catch(() => null)) as Row | null
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.message
    throw new Error(String(detail ?? `接口返回 ${response.status}，数据未更新`))
  }
  return payload ?? {}
}

function lastRevision(revisions: Row[]): Row | null {
  return revisions.length > 0 ? revisions[revisions.length - 1] : null
}

export default function CheckpointDesk() {
  const [desk, setDesk] = useState<Desk | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [operator, setOperator] = useState('值班管理员')
  const [token, setToken] = useState('')
  const [note, setNote] = useState('')
  const [notice, setNotice] = useState<Notice | null>(null)
  const [busy, setBusy] = useState(false)

  const [pageState, setPageState] = useState<Row | null>(null)
  const [lateRequest, setLateRequest] = useState('')
  const [lateBatch, setLateBatch] = useState('')
  const [editRows, setEditRows] = useState<Row[]>([])

  const loadDesk = useCallback(async () => {
    try {
      const payload = (await call(ENDPOINT)) as unknown as Desk
      setDesk(payload)
      setSelected((prev) => (prev === null && payload.items?.length ? payload.items[0].id : prev))
    } catch (error) {
      setNotice({ ok: false, text: error instanceof Error ? error.message : '续做台读取失败' })
    }
  }, [])

  const loadDetail = useCallback(async (id: number) => {
    try {
      const next = (await call(`${ENDPOINT}/${id}`)) as unknown as Detail
      setDetail(next)
      setPageState(next.page?.快照 ?? null)
      const source = lastRevision(next.revisions ?? [])
      setEditRows(
        (source?.定义 ?? []).map((row: Row) => ({
          卡名: row.卡名,
          动作类型: row.动作类型,
          执行动作: row.执行动作,
          等待模式: row.等待条件?.mode ?? '元素出现',
          等待表达式: row.等待条件?.expr ?? '',
          结果提取: row.结果提取,
          回滚动作: row.回滚定义?.action ?? '',
          模拟: row.回滚定义?.页面参数?.模拟 ?? '',
        })),
      )
      return next
    } catch (error) {
      setNotice({ ok: false, text: error instanceof Error ? error.message : '检查点读取失败' })
      return null
    }
  }, [])

  useEffect(() => {
    void loadDesk()
  }, [loadDesk])

  useEffect(() => {
    if (selected !== null) {
      void loadDetail(selected)
    }
  }, [selected, loadDetail])

  async function refresh() {
    if (selected !== null) {
      await loadDetail(selected)
    }
    await loadDesk()
  }

  async function mutate(path: string, body: Row) {
    setBusy(true)
    setNotice(null)
    try {
      const payload = await call(path, { operator, note, token, ...body })
      setNotice({ ok: Boolean(payload.ok), text: String(payload.message ?? '') })
      if (payload.entry?.lease?.token) {
        setToken(String(payload.entry.lease.token))
      }
      await refresh()
    } catch (error) {
      setNotice({ ok: false, text: error instanceof Error ? error.message : '操作失败' })
    } finally {
      setBusy(false)
    }
  }

  async function submitLateReceipt() {
    await mutate(`${ENDPOINT}/${selected}/late-receipts`, {
      request_id: lateRequest,
      batch: lateBatch,
    })
  }

  async function loadPage(pageId: string) {
    try {
      setPageState(await call(`${ENDPOINT}/pages/${pageId}`))
    } catch (error) {
      setNotice({ ok: false, text: error instanceof Error ? error.message : '演示页读取失败' })
    }
  }

  async function demoAction(pageId: string, action: string, values: Row) {
    try {
      const result = await call(`${ENDPOINT}/pages/${pageId}/actions`, {
        action,
        request_id: `REQ-MANUAL-${Date.now().toString(36).toUpperCase()}`,
        ...values,
      })
      setNotice({ ok: true, text: String(result.message ?? '') })
      await loadPage(pageId)
    } catch (error) {
      setNotice({ ok: false, text: error instanceof Error ? error.message : '演示页动作失败' })
    }
  }

  const current = detail?.current_card ?? null
  const needsReview = !!current && UNRESOLVED.includes(String(current.step_status))
  const rollback = (current?.回滚 ?? {}) as Row
  const rollbackNeedsReview = ['待核对', '已阻断'].includes(String(rollback.状态 ?? ''))
  const evidence = (current?.证据 ?? null) as Row | null
  const cardIndex = Number(detail?.checkpoint.当前卡序 ?? 0)

  const sortedRevisions = useMemo(
    () => [...(detail?.revisions ?? [])].sort((a, b) => Number(b.版本) - Number(a.版本)),
    [detail],
  )

  const editPayload = useMemo(
    () =>
      editRows.map((row, index) => ({
        步骤名: row.卡名 || `步骤 ${index + 1}`,
        动作类型: row.动作类型,
        执行动作: row.执行动作,
        等待条件: {
          mode: row.等待模式,
          expr: row.等待表达式,
          timeout: Number(row.等待超时 ?? 15),
        },
        结果提取: row.结果提取 || '回执状态',
        页面参数: row.模拟 ? { 模拟: row.模拟 } : {},
        回滚定义: row.回滚动作
          ? {
             action: row.回滚动作,
             页面参数: row.模拟 ? { 模拟: row.模拟 } : {},
             说明: '编辑后的回滚定义',
            }
          : {},
      })),
    [editRows],
  )

  return (
    <section className="desk" data-module="rpa-checkpoint">
      <header className="page-head">
        <div>
          <h2>RPA检查点续做台</h2>
          <p className="page-desc">
            每一步一张卡，附带网页证据、等待条件与结果提取。执行失败先核对“动作是否已被页面接受”，
            再决定补记、重试或停止。
          </p>
        </div>
      </header>

      <div className="stat-row">
        {(desk?.cards ?? []).map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>

      <div className="checkpoint-list">
        {(desk?.items ?? []).map((item) => (
          <button
            key={String(item.id)}
            type="button"
            className={`checkpoint-item${selected === item.id ? ' active' : ''}`}
            onClick={() => setSelected(Number(item.id))}
          >
            <span className="checkpoint-code">{String(item.检查点编号)}</span>
            <span className="checkpoint-name">{String(item.检查点名)}</span>
            <span className="checkpoint-meta">
              <span className={statusClass(String(item.续做状态))}>{String(item.续做状态)}</span>
              <span className="muted-text">
                第 {Number(item.当前卡序 ?? 0) + 1}/{String(item.卡片总数)} 张 · v{String(item.当前版本)} · 未决 {String(item.未决卡数)}
              </span>
            </span>
            {item.issue && <span className="checkpoint-issue">{String(item.issue)}</span>}
          </button>
        ))}
        {desk?.items?.length === 0 && <div className="empty-state">暂无检查点</div>}
      </div>

      {notice && (
        <div className={`notice ${notice.ok ? 'ok' : 'bad'}`}>
          <strong>{notice.ok ? '已完成' : '已拦截'}</strong>
          <span>{notice.text}</span>
        </div>
      )}

      {detail && (
        <div className="desk-body">
          <div className="desk-main">
            <section className="panel">
              <div className="panel-head">
                <div>
                  <h3>{String(detail.checkpoint.检查点名)}</h3>
                  <p className="muted-text">
                    {String(detail.checkpoint.检查点编号)} · 流程 {String(detail.checkpoint.流程编号)} · 页面 {String(detail.checkpoint.页面编号)} ·
                    v{String(detail.checkpoint.当前版本)} · 批次 {String(detail.checkpoint.执行批次 ?? '未领取')}
                  </p>
                </div>
                <span className={statusClass(String(detail.checkpoint.续做状态))}>
                  {String(detail.checkpoint.续做状态)}
                </span>
              </div>

              {detail.checkpoint.issue && <div className="issue-box">{String(detail.checkpoint.issue)}</div>}

              <div className="lease-bar">
                <label className="filter-item">
                  <span>操作人</span>
                  <input value={operator} onChange={(event) => setOperator(event.target.value)} />
                </label>
                <label className="filter-item grow">
                  <span>执行权令牌</span>
                  <input
                    value={token}
                    placeholder="领取执行权后自动填入；旧批次令牌用于提交迟到回执"
                    onChange={(event) => setToken(event.target.value)}
                  />
                </label>
                {detail.lease ? (
                  <>
                    <span className="lease-tag">
                      执行权：{String(detail.lease.holder)} · {String(detail.lease.batch)} · 到期 {String(detail.lease.expires_at)}
                    </span>
                    <button className="btn" type="button" onClick={() => setToken(String(detail.lease?.token ?? ''))}>
                      填入当前令牌
                    </button>
                    <button
                      className="btn"
                      type="button"
                      disabled={busy}
                      onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/lease`, { action: '释放', token })}
                    >
                      释放执行权
                    </button>
                  </>
                ) : (
                  <button
                    className="btn primary"
                    type="button"
                    disabled={busy}
                    onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/lease`, { action: '领取', token })}
                  >
                    领取执行权
                  </button>
                )}
                <button
                  className="btn"
                  type="button"
                  disabled={busy}
                  onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/resume`, { token })}
                >
                  恢复续做
                </button>
              </div>
            </section>

            <section className="panel">
              <div className="panel-head">
                <h3>步骤卡</h3>
                <span className="muted-text">执行失败 → 证据核对 → 补记 / 重试 / 停止</span>
              </div>
              <div className="card-strip">
                {detail.cards.map((card, index) => (
                  <article key={String(card.seq)} className={`step-card${index === cardIndex ? ' current' : ''}`}>
                    <header>
                      <span className="step-seq">#{card.seq + 1}</span>
                      <span className={statusClass(String(card.step_status))}>{String(card.step_status)}</span>
                    </header>
                    <strong>{String(card.卡名)}</strong>
                    <span className="muted-text">
                      动作 {actionLabel(String(card.执行动作))} · {String(card.动作类型)} · v{String(card.定义版本)}
                      {card.沿用版本 ? `（在途沿用 v${card.沿用版本}）` : ''}
                    </span>
                    <dl className="step-facts">
                      <dt>等待</dt>
                      <dd>
                        {String(card.等待条件?.mode ?? '—')}「{String(card.等待条件?.expr ?? '')}」
                        {card.等待条件?.timeout ? ` / ${String(card.等待条件.timeout)}s` : ''}
                      </dd>
                      <dt>提取</dt>
                      <dd>
                        {String(card.结果提取 ?? '—')}
                        {card.结果?.值 ? ` → ${String(card.结果.值)}` : ''}
                      </dd>
                      <dt>回滚</dt>
                      <dd>
                        {String(card.回滚定义?.action ?? '未定义')}
                        {card.回滚?.状态 ? ` · ${String(card.回滚.状态)}` : ''}
                      </dd>
                    </dl>
                    {card.最近执行?.说明 && <span className="muted-text">{String(card.最近执行.说明)}</span>}
                    {index === cardIndex && (
                      <div className="step-actions">
                        <button
                          className="btn primary"
                          type="button"
                          disabled={busy || card.step_status !== '未开始'}
                          onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/execute`, { token })}
                        >
                          执行当前卡
                        </button>
                        {UNRESOLVED.includes(String(card.step_status)) && (
                          <span className="muted-text">在下方证据核对区给出结论</span>
                        )}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </section>

            {needsReview && current && (
              <section className="panel review-panel">
                <div className="panel-head">
                  <h3>证据核对区：第 {current.seq + 1} 张卡</h3>
                  <span className={statusClass(String(current.step_status))}>{String(current.step_status)}</span>
                </div>
                <p className="review-question">
                  动作「{actionLabel(String(current.执行动作))}」已经发出。请先判断：<b>演示页面是否已经接受这次动作？</b>
                </p>
                <EvidenceView evidence={evidence} />
                <label className="filter-item full">
                  <span>核对说明（会写入证据记录）</span>
                  <input
                    value={note}
                    placeholder="例如：匹配回执 RCPT-D1-001 属于本次请求"
                    onChange={(event) => setNote(event.target.value)}
                  />
                </label>
                <div className="review-actions">
                  <button
                    className="btn primary"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/review`, {
                        decision: '页面已接受',
                        token,
                      })
                    }
                  >
                    页面已接受 → 补记结果并继续
                  </button>
                  <button
                    className="btn"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/review`, {
                        decision: '页面未接受',
                        token,
                      })
                    }
                  >
                    页面未接受 → 允许重试
                  </button>
                  <button
                    className="btn danger"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/review`, {
                        decision: '无法判明',
                        token,
                      })
                    }
                  >
                    无法判明 → 停止并解释
                  </button>
                  <button
                    className="btn ghost"
                    type="button"
                    disabled={busy || !current.回滚定义?.action}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/rollback`, { token })
                    }
                  >
                    改为执行异常回滚
                  </button>
                </div>
              </section>
            )}

            {rollbackNeedsReview && current && (
              <section className="panel review-panel">
                <div className="panel-head">
                  <h3>回滚证据核对：第 {current.seq + 1} 张卡</h3>
                  <span className={statusClass(String(rollback.状态))}>{String(rollback.状态)}</span>
                </div>
                <p className="review-question">
                  回滚动作「{actionLabel(String(current.回滚定义?.action ?? ''))}」已经发出。
                  请核对：<b>演示页面是否已经执行了这次回滚？</b>
                </p>
                <p className="muted-text">
                  回滚本身也会改变演示页面。若页面已接受却重试回滚，作废次数会再次增加。
                </p>
                <EvidenceView evidence={rollback.证据} compact />
                <div className="review-actions">
                  <button
                    className="btn primary"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/rollback-review`, {
                        decision: '页面已接受',
                        token,
                      })
                    }
                  >
                    回滚已接受 → 不再重试
                  </button>
                  <button
                    className="btn"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/rollback-review`, {
                        decision: '页面未接受',
                        token,
                      })
                    }
                  >
                    回滚未接受 → 允许重试
                  </button>
                  <button
                    className="btn danger"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/rollback-review`, {
                        decision: '无法判明',
                        token,
                      })
                    }
                  >
                    回滚无法判明 → 停止
                  </button>
                </div>
              </section>
            )}

            {!needsReview && current && (
              <section className="panel">
                <div className="panel-head">
                  <h3>异常回滚</h3>
                  <span className="muted-text">按步骤卡定义执行；回滚失败同样要核对副作用</span>
                </div>
                <p className="muted-text">
                  回滚定义：{String(current.回滚定义?.action ?? '未定义')}
                  {current.回滚定义?.页面参数?.模拟 ? `（模拟 ${String(current.回滚定义.页面参数.模拟)}）` : ''}
                  {rollback.状态 ? ` · 当前状态 ${String(rollback.状态)}` : ''}
                </p>
                <button
                  className="btn"
                  type="button"
                  disabled={busy || !current.回滚定义?.action}
                  onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/cards/${current.seq}/rollback`, { token })}
                >
                  执行异常回滚
                </button>
              </section>
            )}

            <section className="panel">
              <div className="panel-head">
                <h3>流程版本</h3>
                <span className="muted-text">编辑形成新版本；在途卡继续按旧版等待条件与回滚定义执行</span>
              </div>
              <div className="revision-list">
                {sortedRevisions.map((revision) => (
                  <article key={String(revision.版本)} className="revision-item">
                    <header>
                      <strong>v{String(revision.版本)}</strong>
                      <span className="muted-text">{String(revision.at)} · {String(revision.actor)}</span>
                    </header>
                    <span>{String(revision.说明)}</span>
                    {revision.在途卡?.seq !== null && revision.在途卡?.seq !== undefined && (
                      <span className="muted-text">
                        在途卡：第 {Number(revision.在途卡.seq) + 1} 张（{String(revision.在途卡.卡名)}）沿用 v{String(revision.在途卡.沿用版本)}
                      </span>
                    )}
                  </article>
                ))}
              </div>
              <div className="edit-grid">
                {editRows.map((row, index) => (
                  <div key={index} className="edit-row">
                    <span className="edit-seq">#{index + 1}</span>
                    <input
                      value={row.卡名}
                      onChange={(event) => updateEdit(setEditRows, index, { 卡名: event.target.value })}
                    />
                    <select
                      value={row.动作类型}
                      onChange={(event) => updateEdit(setEditRows, index, { 动作类型: event.target.value })}
                    >
                      {KIND_OPTIONS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
                    </select>
                    <select
                      value={row.执行动作}
                      onChange={(event) => updateEdit(setEditRows, index, { 执行动作: event.target.value })}
                    >
                      {ACTION_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </select>
                    <select
                      value={row.等待模式}
                      onChange={(event) => updateEdit(setEditRows, index, { 等待模式: event.target.value })}
                    >
                      {WAIT_OPTIONS.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
                    </select>
                    <input
                      value={row.等待表达式}
                      placeholder="等待表达式"
                      onChange={(event) => updateEdit(setEditRows, index, { 等待表达式: event.target.value })}
                    />
                    <input
                      value={row.结果提取}
                      placeholder="结果提取字段"
                      onChange={(event) => updateEdit(setEditRows, index, { 结果提取: event.target.value })}
                    />
                    <select
                      value={row.回滚动作}
                      onChange={(event) => updateEdit(setEditRows, index, { 回滚动作: event.target.value })}
                    >
                      <option value="">不回滚</option>
                      <option value="cancel">作废回执（cancel）</option>
                    </select>
                    <select
                      value={row.模拟}
                      onChange={(event) => updateEdit(setEditRows, index, { 模拟: event.target.value })}
                    >
                      {SIMULATE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </select>
                  </div>
                ))}
              </div>
              <div className="panel-actions">
                <button
                  className="btn"
                  type="button"
                  disabled={busy || editPayload.length === 0}
                  onClick={() => void mutate(`${ENDPOINT}/${detail.checkpoint.id}/edit`, { steps: editPayload })}
                >
                  保存为新版本
                </button>
                <span className="muted-text">保存只影响后续卡片；在途卡继续按当前版本的等待条件与回滚定义。</span>
              </div>
            </section>

            <section className="panel">
              <div className="panel-head">
                <h3>旧批次证据区</h3>
                <span className="muted-text">失权执行者的迟到回执只留档，不推进步骤</span>
              </div>
              <table className="data-table compact">
                <thead>
                  <tr><th>批次</th><th>请求编号</th><th>提交者</th><th>到达时间</th><th>说明</th></tr>
                </thead>
                <tbody>
                  {(detail.stale_receipts ?? []).map((row) => (
                    <tr key={String(row.id)}>
                      <td>{String(row.batch)}</td>
                      <td>{String(row.request_id)}</td>
                      <td>{String(row.holder)}</td>
                      <td>{String(row.received_at)}</td>
                      <td>{String(row.note)}</td>
                    </tr>
                  ))}
                  {(detail.stale_receipts ?? []).length === 0 && (
                    <tr><td className="empty-state" colSpan={5}>暂无旧批次回执</td></tr>
                  )}
                </tbody>
              </table>
              <div className="inline-form">
                <label className="filter-item">
                  <span>请求编号</span>
                  <input value={lateRequest} onChange={(event) => setLateRequest(event.target.value)} />
                </label>
                <label className="filter-item">
                  <span>旧批次</span>
                  <input value={lateBatch} onChange={(event) => setLateBatch(event.target.value)} />
                </label>
                <button
                  className="btn"
                  type="button"
                  disabled={busy || !lateRequest}
                  onClick={() => void submitLateReceipt()}
                >
                  提交迟到回执
                </button>
              </div>
            </section>
          </div>

          <aside className="desk-side">
            <section className="panel">
              <div className="panel-head">
                <h3>本地演示页面</h3>
                <button className="link" type="button" onClick={() => void loadPage(String(detail.checkpoint.页面编号))}>
                  刷新
                </button>
              </div>
              <p className="muted-text">
                演示页会真实记录填表与确认动作；每次确认都会生成新回执，页面已接受后重试会造成重复副作用。
              </p>
              {pageState ? (
                <dl className="page-facts">
                  <dt>表单</dt>
                  <dd>{String(pageState.表单?.申请人 ?? '—')} / {String(pageState.表单?.金额 ?? '—')} / {String(pageState.表单?.事由 ?? '—')}</dd>
                  <dt>登录</dt>
                  <dd>{String(pageState.登录?.状态 ?? '—')} · {String(pageState.登录?.账号 ?? '—')}</dd>
                  <dt>回执</dt>
                  <dd>{String(pageState.回执?.编号 ?? '无')} · {String(pageState.回执?.状态 ?? '—')}</dd>
                  <dt>提交 / 作废</dt>
                  <dd>{String(pageState.提交次数 ?? 0)} / {String(pageState.作废次数 ?? 0)}</dd>
                </dl>
              ) : (
                <div className="empty-state">尚未读取页面状态</div>
              )}
              <div className="panel-actions">
                <button
                  className="btn"
                  type="button"
                  onClick={() => void demoAction(String(detail.checkpoint.页面编号), 'observe', {})}
                >
                  观察
                </button>
                <button
                  className="btn"
                  type="button"
                  onClick={() => void demoAction(String(detail.checkpoint.页面编号), 'fill', current?.页面参数 ?? {})}
                >
                  填表
                </button>
                <button
                  className="btn"
                  type="button"
                  onClick={() => void demoAction(String(detail.checkpoint.页面编号), 'confirm', {})}
                >
                  确认
                </button>
              </div>
              {(pageState?.审计?.length ?? 0) > 0 && (
                <ul className="audit-list">
                  {((pageState?.审计 ?? []) as Row[]).slice(-6).map((event, index) => (
                    <li key={`${String(event.event_id)}-${index}`}>
                      <span className="muted-text">{String(event.at)}</span> {String(event.note)}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="panel">
              <div className="panel-head">
                <h3>证据速览</h3>
              </div>
              <EvidenceView evidence={evidence} compact />
            </section>
          </aside>
        </div>
      )}
    </section>
  )
}

function EvidenceView({ evidence, compact }: { evidence?: Row | null; compact?: boolean }) {
  if (!evidence) {
    return <div className="empty-state">暂无证据记录</div>
  }
  const snapshot = evidence.页面快照 ?? {}
  const matched: Row[] = evidence.匹配回执 ?? []
  const audit: Row[] = evidence.页面审计 ?? []
  return (
    <div className={`evidence${compact ? ' compact' : ''}`}>
      <div className="evidence-head">
        <span>请求 {String(evidence.request_id)}</span>
        <span>{String(evidence.at)}</span>
        <span className={evidence.已送达 ? 'chip ok' : 'chip warn'}>{evidence.已送达 ? '已送达' : '未送达'}</span>
        <span className="chip">{actionLabel(String(evidence.动作))}</span>
      </div>
      {evidence.核对提示 && <div className="evidence-tip">{String(evidence.核对提示)}</div>}
      <dl className="evidence-facts">
        <dt>等待条件</dt>
        <dd>
          {String(evidence.等待条件?.mode ?? '—')}「{String(evidence.等待条件?.expr ?? '')}」
          {evidence.等待条件?.timeout ? ` / ${String(evidence.等待条件.timeout)}s` : ''}
          · {evidence.等待结果?.满足 ? '已满足' : '未满足'}
          {evidence.等待结果?.说明 ? `：${String(evidence.等待结果.说明)}` : ''}
        </dd>
        <dt>结果提取</dt>
        <dd>{String(evidence.结果提取?.字段 ?? '—')} = {String(evidence.结果提取?.值 ?? '—')}</dd>
        <dt>页面快照</dt>
        <dd>
          表单 {String(snapshot.表单?.申请人 ?? '—')} / {String(snapshot.表单?.金额 ?? '—')}； 回执{' '}
          {String(snapshot.回执?.编号 ?? '无')}（{String(snapshot.回执?.状态 ?? '—')}）； 提交 {String(snapshot.提交次数 ?? 0)} 次，作废{' '}
          {String(snapshot.作废次数 ?? 0)} 次
        </dd>
      </dl>
      <div className="evidence-lists">
        <div>
          <span className="muted-text">匹配回执</span>
          <ul>
            {matched.map((event, index) => (
              <li key={`${String(event.event_id)}-${index}`}>{String(event.at)} · {String(event.note)}</li>
            ))}
            {matched.length === 0 && <li className="muted-text">没有与本次请求匹配的页面事件</li>}
          </ul>
        </div>
        {!compact && (
          <div>
            <span className="muted-text">页面审计</span>
            <ul>
              {audit.map((event, index) => (
                <li key={`${String(event.event_id)}-${index}`}>
                  {String(event.at)} · {String(event.action)} · {String(event.note)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

function updateEdit(
  setRows: React.Dispatch<React.SetStateAction<Row[]>>,
  index: number,
  patch: Row,
) {
  setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))
}
