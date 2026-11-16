import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/state'
const columns = [
  "配置编号",
  "后端类型",
  "存储路径",
  "状态大小",
  "过期策略",
  "清理间隔",
  "快照频率",
  "配置状态"
]
const actions = [
  "启用后端",
  "暂停后端",
  "关闭后端"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "后端配置总数",
    "value": 0
  },
  {
    "label": "已启用后端",
    "value": 0
  },
  {
    "label": "已暂停后端",
    "value": 0
  }
]

type Row = Record<string, string | number | boolean | null>
type VersionRow = {
  id: number
  version: number
  enabled: boolean
  config: Record<string, string | number | null>
  created_at?: string
}

type EnableForm = {
  状态大小: string
  快照频率: string
  清理间隔: string
}

function buildEnableForm(row: Row | null): EnableForm {
  const pick = (key: string) => {
    const value = row?.[key]
    return value === null || value === undefined ? '' : String(value)
  }
  return {
    状态大小: pick('状态大小'),
    快照频率: pick('快照频率'),
    清理间隔: pick('清理间隔'),
  }
}

export default function StateView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 详情抽屉状态：列表入口与抽屉内的启用入口读写同一条后端记录。
  const [drawerRow, setDrawerRow] = useState<Row | null>(null)
  const [versions, setVersions] = useState<VersionRow[]>([])
  const [enableForm, setEnableForm] = useState<EnableForm>({ 状态大小: '', 快照频率: '', 清理间隔: '' })
  const [submitting, setSubmitting] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('后端配置列表读取失败')
      }
      const payload = await response.json()
      const nextRows: Row[] = payload.items ?? []
      setRows(nextRows)
      setTotal(payload.total ?? 0)
      // 抽屉打开时同步刷新同一条记录，保证页面回显与后端持久化数据一致。
      setDrawerRow((prev) => {
        if (!prev) return prev
        const updated = nextRows.find((item) => String(item.id) === String(prev.id))
        return updated ?? prev
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '状态后端管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadVersions = useCallback(async (row: Row) => {
    try {
      const response = await request(`${ENDPOINT}/${row.id}/versions`)
      if (!response.ok) {
        setVersions([])
        return
      }
      const payload = await response.json()
      setVersions(payload.items ?? [])
    } catch {
      setVersions([])
    }
  }, [])

  async function openDrawer(row: Row) {
    setErrorMessage('')
    setDrawerRow(row)
    setEnableForm(buildEnableForm(row))
    await loadVersions(row)
  }

  function closeDrawer() {
    setDrawerRow(null)
    setVersions([])
    setEnableForm({ 状态大小: '', 快照频率: '', 清理间隔: '' })
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        // 动作与配置统一收在 values 里，与后端 EntryPayload 口径一致。
        body: JSON.stringify({ values: { action } }),
      })
      if (!response.ok) {
        throw new Error('状态后端管理动作未生效，请稍后重试')
      }
      const payload = await response.json()
      if (payload.ok === false) {
        throw new Error(payload.message ?? '状态后端管理动作未生效')
      }
      await reload()
      if (drawerRow && String(drawerRow.id) === String(row.id)) {
        await loadVersions(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '状态后端管理操作失败')
    }
  }

  async function submitEnable() {
    if (!drawerRow) return
    setErrorMessage('')
    setSubmitting(true)
    try {
      const response = await request(`${ENDPOINT}/${drawerRow.id}/actions`, {
        method: 'POST',
        // 启用动作一次写齐状态大小、快照频率、清理间隔，由后端固化配置版本。
        body: JSON.stringify({
          values: {
            action: '启用后端',
            状态大小: enableForm.状态大小,
            快照频率: enableForm.快照频率,
            清理间隔: enableForm.清理间隔,
          },
        }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.message ?? '启用后端未生效，请稍后重试')
      }
      await reload()
      await loadVersions(drawerRow)
      // 以持久化后的记录为准回填表单，刷新/重新进入也不会丢失。
      setEnableForm(buildEnableForm(payload.entry ?? drawerRow))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '启用后端操作失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="page" data-module="state">
      <header className="page-head">
        <div>
          <h2>状态后端管理管理</h2>
          <p className="page-desc">维护状态后端的类型选择、存储配置与过期策略，为状态后端管理提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('后端配置登记入口尚未接入审批流')}>
            登记后端配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出状态后端管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDrawer(row)}>
                  详情
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无状态后端管理数据，可先登记后端配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {drawerRow && (
        <div className="drawer-mask" role="presentation" onClick={closeDrawer}>
          <aside className="drawer" role="dialog" aria-label="状态后端配置详情" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <h3>后端配置详情 · {String(drawerRow['配置编号'] ?? '')}</h3>
              <button className="btn ghost" type="button" onClick={closeDrawer}>关闭</button>
            </div>

            <dl className="drawer-detail">
              {columns.map((field) => (
                <div key={field} className="drawer-field">
                  <dt>{field}</dt>
                  <dd>{drawerRow[field] ?? '—'}</dd>
                </div>
              ))}
              <div className="drawer-field">
                <dt>启用字段</dt>
                <dd>{drawerRow.enabled === true ? '已启用（enabled=true）' : '未启用（enabled=false）'}</dd>
              </div>
              <div className="drawer-field">
                <dt>配置版本号</dt>
                <dd>{drawerRow['配置版本号'] ? `v${String(drawerRow['配置版本号'])}` : '—'}</dd>
              </div>
            </dl>

            <fieldset className="enable-form">
              <legend>启用后端配置（一次写齐，落库为配置版本数据）</legend>
              <label className="filter-item">
                <span>存储路径（只读，沿用当前记录）</span>
                <input value={String(drawerRow['存储路径'] ?? '')} readOnly />
              </label>
              <label className="filter-item">
                <span>状态大小</span>
                <input
                  value={enableForm.状态大小}
                  placeholder="例如 1.8GB"
                  onChange={(event) => setEnableForm((prev) => ({ ...prev, 状态大小: event.target.value }))}
                />
              </label>
              <label className="filter-item">
                <span>快照频率</span>
                <input
                  value={enableForm.快照频率}
                  placeholder="例如 5m"
                  onChange={(event) => setEnableForm((prev) => ({ ...prev, 快照频率: event.target.value }))}
                />
              </label>
              <label className="filter-item">
                <span>清理间隔</span>
                <input
                  value={enableForm.清理间隔}
                  placeholder="例如 30m"
                  onChange={(event) => setEnableForm((prev) => ({ ...prev, 清理间隔: event.target.value }))}
                />
              </label>
              <button className="btn primary" type="button" disabled={submitting} onClick={() => void submitEnable()}>
                {submitting ? '启用中…' : '启用后端'}
              </button>
            </fieldset>

            <section className="version-list">
              <h4>配置版本历史（完整配置值可追溯）</h4>
              {versions.length === 0 ? (
                <p className="empty-state">暂无配置版本，启用后自动固化第一份版本快照</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>版本</th>
                      <th>状态大小</th>
                      <th>快照频率</th>
                      <th>清理间隔</th>
                      <th>存储路径</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((version) => (
                      <tr key={version.id}>
                        <td>v{version.version}</td>
                        <td>{String(version.config?.['状态大小'] ?? '—')}</td>
                        <td>{String(version.config?.['快照频率'] ?? '—')}</td>
                        <td>{String(version.config?.['清理间隔'] ?? '—')}</td>
                        <td>{String(version.config?.['存储路径'] ?? '—')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </aside>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条状态后端管理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
