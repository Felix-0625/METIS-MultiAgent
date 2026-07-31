/**
 * 全局员工池 v5
 * - 展示核心员工：PM团队 / Supervisor团队 / 管理层（HR / CCB）
 * - 执行层专家已迁移到「专家池」页面（/experts 接口）
 * - PM/Supervisor 团队支持新增成员（能力相同，记忆独立）
 * - 删除走 CCB 保护：组长不可删、成员数不得少于4、在项目中需二次确认
 */

import React, { useEffect, useState } from 'react'
import axios from 'axios'
import { API_BASE_URL } from '../services/apiBase'

const API = API_BASE_URL

// ─── 类型 ─────────────────────────────────────────────────────────────────────

interface Employee {
  employee_id: string
  name: string
  role: string
  agent_type: string
  avatar: string
  department: string
  role_description: string
  working_style: string
  communication_style: string
  domains: string[]
  skills: string[]
  behavior_rules: string[]
  output_format: string
  status: string
  current_projects: string[]
  total_projects: number
  avg_quality_score: number
  is_busy: boolean
  project_count: number
}

interface Stats {
  total: number
  available: number
  busy: number
  by_type: Record<string, number>
  by_department: Record<string, number>
}

// ─── 常量 ─────────────────────────────────────────────────────────────────────

const DEPT_LABELS: Record<string, string> = {
  pm_team: 'PM 团队',
  supervisor_team: 'Supervisor 团队',
  management: '管理层（HR / CCB）',
}

const DEPT_COLORS: Record<string, string> = {
  pm_team: 'bg-purple-50 border-purple-200',
  supervisor_team: 'bg-blue-50 border-blue-200',
  management: 'bg-orange-50 border-orange-200',
}

const DEPT_BADGE: Record<string, string> = {
  pm_team: 'bg-purple-100 text-purple-700',
  supervisor_team: 'bg-blue-100 text-blue-700',
  management: 'bg-orange-100 text-orange-700',
}

const TYPE_OPTIONS = [
  { value: 'pm', label: 'PM' },
  { value: 'supervisor', label: 'Supervisor' },
  { value: 'hr', label: 'HR' },
  { value: 'ccb', label: 'CCB' },
]

const DEPT_OPTIONS = [
  { value: 'pm_team', label: 'PM 团队' },
  { value: 'supervisor_team', label: 'Supervisor 团队' },
  { value: 'management', label: '管理层' },
]

// ─── 员工卡片 ─────────────────────────────────────────────────────────────────

function EmployeeCard({
  emp,
  onEdit,
  onDelete,
}: {
  emp: Employee
  onEdit: (e: Employee) => void
  onDelete: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className={`border rounded-lg p-3 bg-white shadow-sm hover:shadow-md transition-shadow`}>
      <div className="flex items-start gap-3">
        <span className="text-2xl mt-0.5">{emp.avatar || '🤖'}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-sm">{emp.name}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded-full ${DEPT_BADGE[emp.department] || 'bg-gray-100 text-gray-600'}`}>
              {DEPT_LABELS[emp.department] || emp.department}
            </span>
            <span className={`text-xs px-1.5 py-0.5 rounded-full ${emp.is_busy ? 'bg-yellow-100 text-yellow-700' : 'bg-green-100 text-green-700'}`}>
              {emp.is_busy ? `忙碌（${emp.project_count} 个项目）` : '空闲'}
            </span>
          </div>
          <div className="text-xs text-gray-500 mt-0.5">{emp.role} · {emp.agent_type.toUpperCase()}</div>
          {emp.role_description && (
            <div className="text-xs text-gray-600 mt-1 line-clamp-2">{emp.role_description}</div>
          )}
          {emp.skills.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {emp.skills.slice(0, 5).map(s => (
                <span key={s} className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded">{s}</span>
              ))}
              {emp.skills.length > 5 && <span className="text-xs text-gray-400">+{emp.skills.length - 5}</span>}
            </div>
          )}
        </div>
        <div className="flex gap-1 shrink-0">
          <button onClick={() => setExpanded(v => !v)}
            className="text-xs text-gray-400 hover:text-gray-600 px-1.5 py-1 rounded hover:bg-gray-100">
            {expanded ? '收起' : '详情'}
          </button>
          <button onClick={() => onEdit(emp)}
            className="text-xs text-blue-500 hover:text-blue-700 px-1.5 py-1 rounded hover:bg-blue-50">
            编辑
          </button>
          <button onClick={() => onDelete(emp.employee_id)}
            className="text-xs text-red-400 hover:text-red-600 px-1.5 py-1 rounded hover:bg-red-50">
            删除
          </button>
        </div>
      </div>

      {expanded && (
        <div className="mt-3 pt-3 border-t space-y-2 text-xs text-gray-600">
          {emp.working_style && <div><span className="font-medium">做事风格：</span>{emp.working_style}</div>}
          {emp.communication_style && <div><span className="font-medium">沟通风格：</span>{emp.communication_style}</div>}
          {emp.domains.length > 0 && (
            <div><span className="font-medium">擅长领域：</span>{emp.domains.join('、')}</div>
          )}
          {emp.behavior_rules.length > 0 && (
            <div>
              <span className="font-medium">行为规范：</span>
              <ul className="mt-1 space-y-0.5 pl-3">
                {emp.behavior_rules.map((r, i) => <li key={i} className="list-disc">{r}</li>)}
              </ul>
            </div>
          )}
          {emp.output_format && <div><span className="font-medium">输出格式：</span>{emp.output_format}</div>}
          {emp.total_projects > 0 && (
            <div className="text-gray-400">参与过 {emp.total_projects} 个项目</div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── 员工表单弹窗 ─────────────────────────────────────────────────────────────

function EmployeeFormModal({
  initial,
  onSave,
  onClose,
}: {
  initial?: Partial<Employee>
  onSave: (data: Partial<Employee>) => Promise<void>
  onClose: () => void
}) {
  const [form, setForm] = useState<Partial<Employee>>({
    name: '',
    role: '',
    agent_type: 'pm',
    avatar: '🤖',
    department: 'management',
    role_description: '',
    working_style: '',
    communication_style: '',
    domains: [],
    skills: [],
    behavior_rules: [],
    output_format: '',
    ...initial,
  })
  const [domainsStr, setDomainsStr] = useState((initial?.domains || []).join('、'))
  const [skillsStr, setSkillsStr] = useState((initial?.skills || []).join('、'))
  const [rulesStr, setRulesStr] = useState((initial?.behavior_rules || []).join('\n'))
  const [saving, setSaving] = useState(false)

  async function handleSave() {
    if (!form.name?.trim() || !form.role?.trim()) return
    setSaving(true)
    await onSave({
      ...form,
      domains: domainsStr.split(/[,，、\s]+/).filter(Boolean),
      skills: skillsStr.split(/[,，、\s]+/).filter(Boolean),
      behavior_rules: rulesStr.split('\n').map(s => s.trim()).filter(Boolean),
    })
    setSaving(false)
  }

  const inp = 'w-full border rounded px-2 py-1 text-sm'
  const label = 'block text-xs text-gray-500 mb-1'

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-gray-800">{initial?.employee_id ? '编辑员工' : '新增员工'}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl">×</button>
        </div>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={label}>姓名 *</label>
              <input className={inp} value={form.name || ''} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="如：PM 组长 · 张伟" />
            </div>
            <div>
              <label className={label}>角色 *</label>
              <input className={inp} value={form.role || ''} onChange={e => setForm(f => ({ ...f, role: e.target.value }))} placeholder="如：PM 组长" />
            </div>
            <div>
              <label className={label}>Agent 类型</label>
              <select className={inp} value={form.agent_type || 'pm'} onChange={e => setForm(f => ({ ...f, agent_type: e.target.value }))}>
                {TYPE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div>
              <label className={label}>部门</label>
              <select className={inp} value={form.department || 'management'} onChange={e => setForm(f => ({ ...f, department: e.target.value }))}>
                {DEPT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div>
              <label className={label}>头像（Emoji）</label>
              <input className={inp} value={form.avatar || '🤖'} onChange={e => setForm(f => ({ ...f, avatar: e.target.value }))} />
            </div>
          </div>
          <div>
            <label className={label}>角色描述</label>
            <textarea rows={2} className={inp} value={form.role_description || ''} onChange={e => setForm(f => ({ ...f, role_description: e.target.value }))} placeholder="职责描述，会注入 system prompt" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={label}>做事风格</label>
              <input className={inp} value={form.working_style || ''} onChange={e => setForm(f => ({ ...f, working_style: e.target.value }))} placeholder="如：严谨、快速" />
            </div>
            <div>
              <label className={label}>沟通风格</label>
              <input className={inp} value={form.communication_style || ''} onChange={e => setForm(f => ({ ...f, communication_style: e.target.value }))} placeholder="如：简洁直接" />
            </div>
          </div>
          <div>
            <label className={label}>擅长领域（逗号分隔）</label>
            <input className={inp} value={domainsStr} onChange={e => setDomainsStr(e.target.value)} placeholder="如：Python, FastAPI, 需求分析" />
          </div>
          <div>
            <label className={label}>技能列表（逗号分隔）</label>
            <input className={inp} value={skillsStr} onChange={e => setSkillsStr(e.target.value)} placeholder="如：React, TypeScript, Docker" />
          </div>
          <div>
            <label className={label}>行为规范（每行一条）</label>
            <textarea rows={3} className={`${inp} font-mono text-xs`} value={rulesStr} onChange={e => setRulesStr(e.target.value)} placeholder="不确定时直接说不确定&#10;输出前必须自检" />
          </div>
          <div>
            <label className={label}>输出格式要求</label>
            <input className={inp} value={form.output_format || ''} onChange={e => setForm(f => ({ ...f, output_format: e.target.value }))} placeholder="如：结构化 Markdown，包含目标和验收标准" />
          </div>
        </div>
        <div className="flex gap-2 mt-4 justify-end">
          <button onClick={onClose} className="px-4 py-2 border rounded text-sm hover:bg-gray-50">取消</button>
          <button onClick={handleSave} disabled={saving || !form.name?.trim() || !form.role?.trim()}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── 主页面 ───────────────────────────────────────────────────────────────────

// ─── CCB 删除确认弹窗 ─────────────────────────────────────────────────────────

function CCBConfirmModal({
  message,
  onConfirm,
  onCancel,
}: {
  message: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
        <div className="flex items-start gap-3 mb-4">
          <span className="text-2xl">⚠️</span>
          <div>
            <h3 className="font-semibold text-gray-800 mb-1">CCB 删除保护</h3>
            <p className="text-sm text-gray-600 whitespace-pre-line">{message}</p>
          </div>
        </div>
        <div className="flex gap-2 justify-end">
          <button onClick={onCancel} className="px-4 py-2 border rounded text-sm hover:bg-gray-50">取消</button>
          <button onClick={onConfirm} className="px-4 py-2 bg-red-600 text-white rounded text-sm hover:bg-red-700">
            确认强制删除
          </button>
        </div>
      </div>
    </div>
  )
}

const AgentPool: React.FC = () => {
  const [employees, setEmployees] = useState<Employee[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(false)
  const [filterDept, setFilterDept] = useState<string>('all')
  const [filterStatus, setFilterStatus] = useState<string>('all')
  const [search, setSearch] = useState('')
  const [editTarget, setEditTarget] = useState<Partial<Employee> | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [msg, setMsg] = useState('')
  // CCB 二次确认
  const [ccbConfirm, setCcbConfirm] = useState<{ message: string; token: string } | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const res = await axios.get(`${API}/employee-pool`)
      setEmployees(res.data.employees || [])
      setStats(res.data.stats || null)
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 403) {
        setMsg('当前账号无权查看员工池')
      } else if (axios.isAxiosError(error) && error.response?.status === 401) {
        setMsg('登录已失效，请重新登录')
      } else {
        setMsg('员工池加载失败，请稍后重试')
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // 过滤
  const filtered = employees.filter(e => {
    if (filterDept !== 'all' && e.department !== filterDept) return false
    if (filterStatus === 'available' && e.is_busy) return false
    if (filterStatus === 'busy' && !e.is_busy) return false
    if (search) {
      const q = search.toLowerCase()
      return e.name.toLowerCase().includes(q) || e.role.toLowerCase().includes(q) ||
        e.skills.some(s => s.toLowerCase().includes(q)) || e.domains.some(d => d.toLowerCase().includes(q))
    }
    return true
  })

  // 按部门分组
  const grouped: Record<string, Employee[]> = {}
  for (const e of filtered) {
    const dept = e.department || 'management'
    if (!grouped[dept]) grouped[dept] = []
    grouped[dept].push(e)
  }

  const deptOrder = ['pm_team', 'supervisor_team', 'management']

  async function handleSave(data: Partial<Employee>) {
    try {
      if (data.employee_id) {
        await axios.patch(`${API}/employee-pool/${data.employee_id}`, data)
        setMsg('员工信息已更新')
      } else {
        await axios.post(`${API}/employee-pool`, data)
        setMsg('员工已添加到员工池')
      }
      setShowForm(false)
      setEditTarget(null)
      load()
    } catch {
      setMsg('保存失败')
    }
  }

  // CCB 保护删除：先调用检查接口，根据结果决定是否需要二次确认
  async function handleDelete(id: string) {
    const emp = employees.find(e => e.employee_id === id)
    if (!emp) return
    try {
      const res = await axios.post(`${API}/ccb/check-delete-member`, {
        team_type: emp.department,
        member_id: id,
        member_name: emp.name,
        is_leader: emp.role.includes('组长') || emp.role.includes('Leader'),
        current_count: employees.filter(e => e.department === emp.department).length,
        is_in_project: emp.is_busy,
      })
      const data = res.data
      if (data.allowed) {
        // 直接删除
        await axios.delete(`${API}/employee-pool/${id}`)
        setMsg(`✅ ${emp.name} 已删除`)
        load()
      } else if (data.require_confirm && data.confirm_token) {
        // 需要二次确认
        setCcbConfirm({ message: data.message, token: data.confirm_token })
      } else {
        // 拒绝删除
        setMsg(data.message)
      }
    } catch {
      // 后端无 CCB 接口时降级为普通确认
      if (!confirm(`确认删除 ${emp.name}？`)) return
      try {
        await axios.delete(`${API}/employee-pool/${id}`)
        setMsg('已删除')
        load()
      } catch {
        setMsg('删除失败')
      }
    }
  }

  // 用户二次确认强制删除
  async function handleCCBConfirm() {
    if (!ccbConfirm) return
    try {
      const res = await axios.post(`${API}/ccb/confirm-delete`, { confirm_token: ccbConfirm.token })
      if (res.data.allowed) {
        await axios.delete(`${API}/employee-pool/${res.data.member_id}`)
        setMsg(`✅ 已强制删除 ${res.data.member_name}`)
        load()
      } else {
        setMsg(res.data.message || '确认失败')
      }
    } catch {
      setMsg('操作失败')
    } finally {
      setCcbConfirm(null)
    }
  }

  // PM/Supervisor 团队新增成员
  async function handleAddTeamMember(teamType: 'pm' | 'supervisor') {
    try {
      const res = await axios.post(`${API}/team/${teamType}/add-member`)
      if (res.data.success) {
        setMsg(`✅ 已新增${teamType === 'pm' ? 'PM' : 'Supervisor'}成员：${res.data.name || '新成员'}`)
        load()
      } else {
        setMsg(`新增失败：${res.data.detail || res.data.message || '未知错误'}`)
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.message || '请求失败'
      setMsg(`新增成员失败：${detail}`)
    }
  }

  async function handleReset() {
    if (!confirm('确认重置员工池为预置状态？这会清空所有自定义员工。')) return
    try {
      await axios.post(`${API}/employee-pool/reset-presets`)
      setMsg('员工池已重置为预置状态')
      load()
    } catch {
      setMsg('重置失败')
    }
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* 顶部标题 + 统计 */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">全局员工池</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            管理核心员工：PM 团队、Supervisor 团队、HR、CCB。执行层专家请前往「专家池」页面管理。
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={load} className="px-3 py-1.5 border rounded text-sm hover:bg-gray-50">刷新</button>
          <button onClick={handleReset} className="px-3 py-1.5 border rounded text-sm text-orange-600 hover:bg-orange-50">重置预置</button>
          <button onClick={() => { setEditTarget({}); setShowForm(true) }}
            className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">
            + 新增员工
          </button>
        </div>
      </div>

      {/* 统计卡片 */}
      {stats && (
        <div className="grid grid-cols-4 gap-3 mb-4">
          {[
            { label: '总员工', value: stats.total, color: 'text-gray-700' },
            { label: '空闲', value: stats.available, color: 'text-green-600' },
            { label: '忙碌', value: stats.busy, color: 'text-yellow-600' },
            { label: '部门数', value: Object.keys(stats.by_department).length, color: 'text-blue-600' },
          ].map(s => (
            <div key={s.label} className="bg-white border rounded-lg p-3 text-center">
              <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
              <div className="text-xs text-gray-500 mt-0.5">{s.label}</div>
            </div>
          ))}
        </div>
      )}

      {/* 过滤栏 */}
      <div className="flex gap-3 mb-4 flex-wrap">
        <input className="border rounded px-3 py-1.5 text-sm w-48" placeholder="搜索姓名/角色/技能…"
          value={search} onChange={e => setSearch(e.target.value)} />
        <select className="border rounded px-2 py-1.5 text-sm" value={filterDept} onChange={e => setFilterDept(e.target.value)}>
          <option value="all">全部部门</option>
          {DEPT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <select className="border rounded px-2 py-1.5 text-sm" value={filterStatus} onChange={e => setFilterStatus(e.target.value)}>
          <option value="all">全部状态</option>
          <option value="available">空闲</option>
          <option value="busy">忙碌</option>
        </select>
        <span className="text-sm text-gray-400 self-center">共 {filtered.length} 人</span>
      </div>

      {/* 消息提示 */}
      {msg && (
        <div className="mb-3 px-3 py-2 bg-blue-50 text-blue-700 text-sm rounded border border-blue-200 flex items-center justify-between">
          {msg}
          <button onClick={() => setMsg('')} className="text-blue-400 hover:text-blue-600 ml-2">×</button>
        </div>
      )}

      {/* 员工列表（按部门分组） */}
      {loading ? (
        <div className="text-center text-gray-400 py-12">加载中…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center text-gray-400 py-12">
          <div className="text-4xl mb-2">👥</div>
          <div>员工池为空，点击「新增员工」或「重置预置」初始化</div>
        </div>
      ) : (
        <div className="space-y-6">
          {deptOrder.filter(d => grouped[d]?.length > 0).map(dept => (
            <div key={dept}>
              <div className={`flex items-center gap-2 px-3 py-2 rounded-t-lg border ${DEPT_COLORS[dept] || 'bg-gray-50 border-gray-200'}`}>
                <span className="font-medium text-sm">{DEPT_LABELS[dept] || dept}</span>
                <span className="text-xs text-gray-500">（{grouped[dept].length} 人）</span>
                {/* PM/Supervisor 团队：新增成员按钮 */}
                {dept === 'pm_team' && (
                  <button onClick={() => handleAddTeamMember('pm')}
                    className="ml-auto text-xs px-2 py-0.5 bg-purple-100 text-purple-700 rounded hover:bg-purple-200">
                    + 新增PM成员
                  </button>
                )}
                {dept === 'supervisor_team' && (
                  <button onClick={() => handleAddTeamMember('supervisor')}
                    className="ml-auto text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200">
                    + 新增Supervisor成员
                  </button>
                )}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3 border border-t-0 rounded-b-lg bg-gray-50/50">
                {grouped[dept].map(emp => (
                  <EmployeeCard key={emp.employee_id} emp={emp}
                    onEdit={e => { setEditTarget(e); setShowForm(true) }}
                    onDelete={handleDelete} />
                ))}
              </div>
            </div>
          ))}
          {/* 其他部门 */}
          {Object.keys(grouped).filter(d => !deptOrder.includes(d)).map(dept => (
            <div key={dept}>
              <div className="flex items-center gap-2 px-3 py-2 rounded-t-lg border bg-gray-50 border-gray-200">
                <span className="font-medium text-sm">{dept}</span>
                <span className="text-xs text-gray-500">（{grouped[dept].length} 人）</span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3 border border-t-0 rounded-b-lg bg-gray-50/50">
                {grouped[dept].map(emp => (
                  <EmployeeCard key={emp.employee_id} emp={emp}
                    onEdit={e => { setEditTarget(e); setShowForm(true) }}
                    onDelete={handleDelete} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新增/编辑弹窗 */}
      {showForm && (
        <EmployeeFormModal
          initial={editTarget || {}}
          onSave={handleSave}
          onClose={() => { setShowForm(false); setEditTarget(null) }}
        />
      )}

      {/* CCB 二次确认弹窗 */}
      {ccbConfirm && (
        <CCBConfirmModal
          message={ccbConfirm.message}
          onConfirm={handleCCBConfirm}
          onCancel={() => setCcbConfirm(null)}
        />
      )}
    </div>
  )
}

export default AgentPool
