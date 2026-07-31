import { useState, useEffect, useRef } from 'react'
import { API_BASE_URL } from '../services/apiBase'

const API = API_BASE_URL
const authFetch = (input: RequestInfo | URL, init: RequestInit = {}) =>
  fetch(input, { ...init, credentials: 'include' })

// ─── 类型 ─────────────────────────────────────────────────────────────────────

interface WorkMode {
  thinking_style: 'chain_of_thought' | 'step_by_step' | 'direct' | 'socratic'
  execution_style: 'conservative' | 'balanced' | 'aggressive'
  verbosity: 'brief' | 'normal' | 'detailed'
  enable_cot: boolean
  enable_self_check: boolean
  custom_prompt_addon: string
  name: string
}

interface TrainingSession {
  session_id: string
  created_at: number
  session_type: string
  user_input: string
  agent_output: string
  feedback: string
  correction: string
  applied: boolean
}

interface Expert {
  expert_id: string
  name: string
  role: string
  agent_type: string
  avatar: string
  role_description: string
  working_style: string
  communication_style: string
  decision_style: string
  domains: string[]
  skills: { name: string; level: string }[]
  skill_ids: string[]
  behavior_rules: string[]
  output_format: string
  work_mode: WorkMode
  thinking_framework: string
  long_term_memory: string
  user_preferences: string[]
  training_sessions: TrainingSession[]
  status: string
  avg_quality_score: number
}

interface ChatMsg { role: 'user' | 'assistant'; content: string }

// ─── API 函数 ─────────────────────────────────────────────────────────────────

async function fetchExperts(): Promise<Expert[]> {
  const r = await authFetch(`${API}/experts`)
  const d = await r.json()
  return d.experts || []
}

async function fetchExpert(id: string): Promise<Expert> {
  const r = await authFetch(`${API}/experts/${id}`)
  return r.json()
}

async function updateExpert(id: string, updates: Partial<Expert>) {
  await authFetch(`${API}/experts/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
}

async function updateWorkMode(id: string, wm: Partial<WorkMode>) {
  await authFetch(`${API}/experts/${id}/work-mode`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(wm),
  })
}

async function chatTrain(id: string, message: string, history: ChatMsg[]) {
  const r = await authFetch(`${API}/experts/${id}/chat-train`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history }),
  })
  return r.json()
}

async function sendFeedback(id: string, payload: {
  session_type: string; user_input: string; agent_output: string
  feedback: string; correction: string; auto_apply: boolean
}) {
  const r = await authFetch(`${API}/experts/${id}/training/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return r.json()
}

async function applyTraining(id: string) {
  const r = await authFetch(`${API}/experts/${id}/training/apply`, { method: 'POST' })
  return r.json()
}

async function uploadKnowledgeText(id: string, content: string, source_name: string) {
  const r = await authFetch(`${API}/experts/${id}/knowledge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, source_name, knowledge_type: 'document' }),
  })
  return r.json()
}

async function uploadKnowledgeFile(id: string, file: File) {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('source_name', file.name)
  fd.append('knowledge_type', 'document')
  const r = await authFetch(`${API}/experts/${id}/knowledge/upload`, { method: 'POST', body: fd })
  return r.json()
}

async function previewPrompt(id: string): Promise<string> {
  const r = await authFetch(`${API}/experts/${id}/system-prompt`)
  const d = await r.json()
  return d.system_prompt || ''
}

async function fetchTraining(id: string) {
  const r = await authFetch(`${API}/experts/${id}/training`)
  return r.json()
}

async function fetchSkillsForType(agentType: string): Promise<{ id: string; name: string }[]> {
  const r = await authFetch(`${API}/skills/for-agent/${agentType}`)
  const d = await r.json()
  return (d.skills || []).map((s: { id: string; name: string }) => ({ id: s.id, name: s.name }))
}

// ─── 基本配置面板（角色信息 + Skill + 个人记忆）────────────────────────────────

function BasicConfigPanel({ expert, onSaved }: { expert: Expert; onSaved: () => void }) {
  const [form, setForm] = useState({
    role_description: expert.role_description || '',
    working_style: expert.working_style || '',
    communication_style: expert.communication_style || '',
    decision_style: expert.decision_style || '',
    output_format: expert.output_format || '',
    domains: (expert.domains || []).join('、'),
  })
  const [skills, setSkills] = useState<{ name: string; level: string }[]>(expert.skills || [])
  const [skillIds, setSkillIds] = useState<string[]>(expert.skill_ids || [])
  const [newSkill, setNewSkill] = useState({ name: '', level: 'intermediate' })
  const [poolSkills, setPoolSkills] = useState<{ id: string; name: string }[]>([])
  const [memory, setMemory] = useState(expert.long_term_memory || '')
  const [prefs, setPrefs] = useState<string[]>(expert.user_preferences || [])
  const [newPref, setNewPref] = useState('')
  const [rules, setRules] = useState<string[]>(expert.behavior_rules || [])
  const [newRule, setNewRule] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    fetchSkillsForType(expert.agent_type).then(setPoolSkills)
  }, [expert.agent_type])

  async function save() {
    setSaving(true)
    await updateExpert(expert.expert_id, {
      role_description: form.role_description,
      working_style: form.working_style,
      communication_style: form.communication_style,
      decision_style: form.decision_style,
      output_format: form.output_format,
      domains: form.domains.split(/[,，、\s]+/).filter(Boolean),
      skills,
      skill_ids: skillIds,
      behavior_rules: rules,
      long_term_memory: memory,
      user_preferences: prefs,
    })
    setSaving(false); setSaved(true)
    setTimeout(() => setSaved(false), 2000)
    onSaved()
  }

  const inp = 'w-full border rounded px-2 py-1 text-sm'
  const sectionTitle = 'font-medium text-gray-700 text-sm mb-2 mt-4 border-b pb-1'

  return (
    <div className="space-y-2 max-w-2xl">
      {/* 角色信息 */}
      <div className={sectionTitle}>角色定位</div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">角色描述（注入 system prompt）</label>
        <textarea rows={3} className={inp} placeholder="我是谁，我负责什么，我的核心价值是什么"
          value={form.role_description} onChange={e => setForm(f => ({ ...f, role_description: e.target.value }))} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">做事风格</label>
          <input className={inp} placeholder="如：严谨、快速、保守" value={form.working_style}
            onChange={e => setForm(f => ({ ...f, working_style: e.target.value }))} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">沟通风格</label>
          <input className={inp} placeholder="如：简洁直接、结构化" value={form.communication_style}
            onChange={e => setForm(f => ({ ...f, communication_style: e.target.value }))} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">决策风格</label>
          <input className={inp} placeholder="如：数据驱动、经验驱动" value={form.decision_style}
            onChange={e => setForm(f => ({ ...f, decision_style: e.target.value }))} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">擅长领域（逗号分隔）</label>
          <input className={inp} placeholder="如：Python, FastAPI, PostgreSQL" value={form.domains}
            onChange={e => setForm(f => ({ ...f, domains: e.target.value }))} />
        </div>
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">输出格式要求</label>
        <input className={inp} placeholder="如：代码必须有注释，回答用 Markdown 格式" value={form.output_format}
          onChange={e => setForm(f => ({ ...f, output_format: e.target.value }))} />
      </div>

      {/* Skill 配置 */}
      <div className={sectionTitle}>Skill 配置</div>
      <div className="flex flex-wrap gap-2 mb-2">
        {skills.map((s, i) => (
          <span key={i} className="flex items-center gap-1 bg-blue-50 text-blue-700 text-xs px-2 py-1 rounded-full">
            {s.name}
            <span className="text-blue-400">({s.level === 'expert' ? '专家' : s.level === 'intermediate' ? '中级' : '初级'})</span>
            <button onClick={() => setSkills(prev => prev.filter((_, j) => j !== i))}
              className="text-blue-300 hover:text-red-500 ml-1">×</button>
          </span>
        ))}
        {skills.length === 0 && <span className="text-xs text-gray-400">暂无 Skill</span>}
      </div>
      {/* Skill 池选择器：手动输入 + 从池中选择 */}
      <div className="flex gap-2">
        <input list="skill-pool-list" className="flex-1 border rounded px-2 py-1 text-sm"
          placeholder="输入名称或从下方 Skill 池选择…" value={newSkill.name}
          onChange={e => setNewSkill(s => ({ ...s, name: e.target.value }))} />
        <datalist id="skill-pool-list">
          {poolSkills.map(s => <option key={s.id} value={s.name} />)}
        </datalist>
        <select className="border rounded px-2 py-1 text-sm"
          value={newSkill.level} onChange={e => setNewSkill(s => ({ ...s, level: e.target.value }))}>
          <option value="beginner">初级</option>
          <option value="intermediate">中级</option>
          <option value="expert">专家</option>
        </select>
        <button onClick={() => {
          if (!newSkill.name.trim()) return
          setSkills(prev => [...prev, { name: newSkill.name.trim(), level: newSkill.level }])
          setNewSkill({ name: '', level: 'intermediate' })
        }} className="px-3 py-1 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">添加</button>
      </div>
      {/* Skill 池快速选择（按 agent_type 分类） */}
      {poolSkills.length > 0 && (
        <div className="border rounded p-2 bg-gray-50 mt-1">
          <div className="text-xs text-gray-400 mb-1.5">
            📦 Skill 池（{expert.agent_type} 类型，点击快速添加）
          </div>
          <div className="flex flex-wrap gap-1.5">
            {poolSkills.map(s => {
              const already = skills.some(sk => sk.name === s.name)
              return (
                <button
                  key={s.id}
                  disabled={already}
                  onClick={() => {
                    if (already) return
                    setSkills(prev => [...prev, { name: s.name, level: 'intermediate' }])
                    setSkillIds(prev => prev.includes(s.id) ? prev : [...prev, s.id])
                  }}
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                    already
                      ? 'bg-blue-100 text-blue-400 border-blue-200 cursor-default'
                      : 'bg-white text-gray-600 border-gray-300 hover:bg-blue-50 hover:text-blue-700 hover:border-blue-300 cursor-pointer'
                  }`}
                >
                  {already ? '✓ ' : '+ '}{s.name}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* 行为规范 */}
      <div className={sectionTitle}>行为规范（注入 system prompt）</div>
      <div className="space-y-1">
        {rules.map((r, i) => (
          <div key={i} className="flex items-center gap-2 bg-gray-50 rounded px-2 py-1">
            <span className="text-xs text-gray-500 w-4">{i + 1}.</span>
            <span className="flex-1 text-sm">{r}</span>
            <button onClick={() => setRules(prev => prev.filter((_, j) => j !== i))}
              className="text-gray-300 hover:text-red-500 text-xs">删除</button>
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <input className="flex-1 border rounded px-2 py-1 text-sm" placeholder="添加行为规范，如：不确定时直接说不确定"
          value={newRule} onChange={e => setNewRule(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && newRule.trim()) { setRules(r => [...r, newRule.trim()]); setNewRule('') } }} />
        <button onClick={() => { if (newRule.trim()) { setRules(r => [...r, newRule.trim()]); setNewRule('') } }}
          className="px-3 py-1 bg-gray-600 text-white rounded text-sm hover:bg-gray-700">添加</button>
      </div>

      {/* 用户偏好 */}
      <div className={sectionTitle}>用户个性化偏好（训练积累）</div>
      <div className="flex flex-wrap gap-2 mb-2">
        {prefs.map((p, i) => (
          <span key={i} className="flex items-center gap-1 bg-purple-50 text-purple-700 text-xs px-2 py-1 rounded-full">
            {p}
            <button onClick={() => setPrefs(prev => prev.filter((_, j) => j !== i))}
              className="text-purple-300 hover:text-red-500 ml-1">×</button>
          </span>
        ))}
        {prefs.length === 0 && <span className="text-xs text-gray-400">暂无偏好设置</span>}
      </div>
      <div className="flex gap-2">
        <input className="flex-1 border rounded px-2 py-1 text-sm" placeholder="如：回答要简洁，代码要加注释"
          value={newPref} onChange={e => setNewPref(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && newPref.trim()) { setPrefs(p => [...p, newPref.trim()]); setNewPref('') } }} />
        <button onClick={() => { if (newPref.trim()) { setPrefs(p => [...p, newPref.trim()]); setNewPref('') } }}
          className="px-3 py-1 bg-purple-600 text-white rounded text-sm hover:bg-purple-700">添加</button>
      </div>

      {/* 长期记忆直接编辑 */}
      <div className={sectionTitle}>长期记忆（直接编辑）</div>
      <p className="text-xs text-gray-400">可直接修改，也可通过「对话训练」或「知识库」自动积累。</p>
      <textarea rows={6} className={`${inp} font-mono text-xs`}
        placeholder="专家的长期记忆会注入到每次任务的 system prompt 中…"
        value={memory} onChange={e => setMemory(e.target.value)} />

      <button onClick={save} disabled={saving}
        className="mt-2 px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
        {saving ? '保存中…' : saved ? '✓ 已保存' : '保存所有配置'}
      </button>
    </div>
  )
}

// ─── 主组件（放在所有子组件之后）────────────────────────────────────────────────

function ExpertPoolPage() {
  const [experts, setExperts] = useState<Expert[]>([])
  const [selected, setSelected] = useState<Expert | null>(null)
  const [tab, setTab] = useState<'basic' | 'config' | 'train' | 'knowledge' | 'prompt'>('basic')
  const [prompt, setPrompt] = useState('')
  const [loadingPrompt, setLoadingPrompt] = useState(false)
  const [msg, setMsg] = useState('')
  const [ccbConfirm, setCcbConfirm] = useState<{ message: string; token: string } | null>(null)

  useEffect(() => { load() }, [])

  async function load() {
    const list = await fetchExperts()
    setExperts(list)
    if (list.length > 0 && !selected) setSelected(list[0])
  }

  async function handleDeleteExpert(expert: Expert) {
    try {
      const r = await authFetch(`${API}/ccb/check-delete-expert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expert_id: expert.expert_id,
          expert_name: expert.name,
          is_in_project: expert.status === 'busy',
          current_project_name: '',
        }),
      })
      const data = await r.json()
      if (data.allowed) {
        await authFetch(`${API}/experts/${expert.expert_id}`, { method: 'DELETE' })
        setMsg(`✅ 专家 ${expert.name} 已删除`)
        if (selected?.expert_id === expert.expert_id) setSelected(null)
        load()
      } else if (data.require_confirm && data.confirm_token) {
        setCcbConfirm({ message: data.message, token: data.confirm_token })
      } else {
        setMsg(data.message)
      }
    } catch {
      if (!confirm(`确认删除专家 ${expert.name}？`)) return
      await authFetch(`${API}/experts/${expert.expert_id}`, { method: 'DELETE' })
      if (selected?.expert_id === expert.expert_id) setSelected(null)
      load()
    }
  }

  async function handleCCBConfirm() {
    if (!ccbConfirm) return
    try {
      const r = await authFetch(`${API}/ccb/confirm-delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_token: ccbConfirm.token }),
      })
      const data = await r.json()
      if (data.allowed) {
        await authFetch(`${API}/experts/${data.member_id}`, { method: 'DELETE' })
        setMsg(`✅ 已强制删除专家 ${data.member_name}`)
        if (selected?.expert_id === data.member_id) setSelected(null)
        load()
      } else {
        setMsg(data.message || '确认失败')
      }
    } catch {
      setMsg('操作失败')
    } finally {
      setCcbConfirm(null)
    }
  }

  async function reloadSelected() {
    if (!selected) return
    const fresh = await fetchExpert(selected.expert_id)
    setSelected(fresh)
    setExperts(prev => prev.map(e => e.expert_id === fresh.expert_id ? fresh : e))
  }

  async function loadPrompt() {
    if (!selected) return
    setLoadingPrompt(true)
    const p = await previewPrompt(selected.expert_id)
    setPrompt(p); setLoadingPrompt(false)
  }

  useEffect(() => {
    if (tab === 'prompt' && selected) loadPrompt()
  }, [tab, selected?.expert_id])

  const tabs = [
    { key: 'basic',     label: '👤 基本配置' },
    { key: 'config',    label: '⚙️ 工作模式' },
    { key: 'train',     label: '💬 对话训练' },
    { key: 'knowledge', label: '📚 知识库' },
    { key: 'prompt',    label: '👁 Prompt 预览' },
  ] as const

  const statusColor: Record<string, string> = {
    available: 'bg-green-100 text-green-700',
    busy: 'bg-yellow-100 text-yellow-700',
    retired: 'bg-gray-100 text-gray-500',
  }

  return (
    <div className="flex h-screen bg-gray-50">
      {/* 左侧专家列表 */}
      <div className="w-64 bg-white border-r flex flex-col">
        <div className="px-4 py-3 border-b font-semibold text-gray-700 flex items-center justify-between">
          <span>专家池</span>
          <span className="text-xs text-gray-400">{experts.length} 位</span>
        </div>
        {/* 消息提示 */}
        {msg && (
          <div className="mx-2 mt-2 px-2 py-1.5 bg-blue-50 text-blue-700 text-xs rounded border border-blue-200 flex items-center justify-between">
            <span className="truncate">{msg}</span>
            <button onClick={() => setMsg('')} className="ml-1 text-blue-400 shrink-0">×</button>
          </div>
        )}
        <div className="flex-1 overflow-y-auto">
          {experts.length === 0 && (
            <div className="text-center text-gray-400 text-sm pt-8 px-4">
              暂无专家，系统启动时会自动预置9类专家
            </div>
          )}
          {experts.map(e => {
            const isPreset = e.expert_id.startsWith('expert-')
            return (
              <div key={e.expert_id}
                className={`border-b hover:bg-gray-50 transition-colors ${
                  selected?.expert_id === e.expert_id ? 'bg-blue-50 border-l-2 border-l-blue-600' : ''}`}>
                <button className="w-full text-left px-4 py-3"
                  onClick={() => { setSelected(e); setTab('basic') }}>
                  <div className="flex items-center gap-2">
                    <span className="text-xl">{e.avatar || '🤖'}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1">
                        <span className="font-medium text-sm truncate">{e.name}</span>
                        {isPreset && (
                          <span className="text-xs bg-orange-100 text-orange-600 px-1 rounded shrink-0">预置</span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 truncate">{e.role}</div>
                    </div>
                    <span className={`text-xs px-1.5 py-0.5 rounded-full shrink-0 ${statusColor[e.status] || 'bg-gray-100 text-gray-500'}`}>
                      {e.status === 'available' ? '空闲' : e.status === 'busy' ? '忙碌' : '退休'}
                    </span>
                  </div>
                  {e.long_term_memory && (
                    <div className="mt-1 text-xs text-purple-500">有长期记忆</div>
                  )}
                </button>
                {/* 删除按钮（走 CCB 保护） */}
                <div className="px-4 pb-2 flex justify-end">
                  <button onClick={ev => { ev.stopPropagation(); handleDeleteExpert(e) }}
                    className="text-xs text-red-400 hover:text-red-600 hover:bg-red-50 px-2 py-0.5 rounded">
                    删除
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* CCB 二次确认弹窗 */}
      {ccbConfirm && (
        <CCBConfirmModal
          message={ccbConfirm.message}
          onConfirm={handleCCBConfirm}
          onCancel={() => setCcbConfirm(null)}
        />
      )}

      {/* 右侧详情 */}
      {selected ? (
        <div className="flex-1 flex flex-col min-w-0">
          {/* 顶部信息栏 */}
          <div className="bg-white border-b px-6 py-4 flex items-center gap-4">
            <span className="text-3xl">{selected.avatar || '🤖'}</span>
            <div>
              <div className="font-semibold text-lg">{selected.name}</div>
              <div className="text-sm text-gray-500">{selected.role} · {selected.agent_type}</div>
              {selected.domains.length > 0 && (
                <div className="flex gap-1 mt-1 flex-wrap">
                  {selected.domains.slice(0, 5).map(d => (
                    <span key={d} className="text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded-full">{d}</span>
                  ))}
                </div>
              )}
            </div>
            <div className="ml-auto text-right text-sm text-gray-400">
              {selected.avg_quality_score > 0 && <div>质检均分 {selected.avg_quality_score.toFixed(1)}</div>}
              {selected.training_sessions?.length > 0 && (
                <div className="text-purple-500">{selected.training_sessions.length} 条训练记录</div>
              )}
            </div>
          </div>

          {/* Tab 导航 */}
          <div className="bg-white border-b px-6 flex gap-1">
            {tabs.map(t => (
              <button key={t.key} onClick={() => setTab(t.key)}
                className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                  tab === t.key ? 'border-blue-600 text-blue-600 font-medium' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
                {t.label}
              </button>
            ))}
          </div>

          {/* Tab 内容 */}
          <div className={`flex-1 overflow-y-auto p-6 ${tab === 'train' ? 'flex flex-col' : ''}`}>
            {tab === 'basic' && (
              <BasicConfigPanel expert={selected} onSaved={reloadSelected} />
            )}
            {tab === 'config' && (
              <WorkModePanel expert={selected} onSaved={reloadSelected} />
            )}
            {tab === 'train' && (
              <div className="flex-1 flex flex-col min-h-0" style={{ height: 'calc(100vh - 200px)' }}>
                <TrainChatPanel expert={selected} onMemoryUpdated={reloadSelected} />
              </div>
            )}
            {tab === 'knowledge' && (
              <KnowledgePanel expert={selected} onUpdated={reloadSelected} />
            )}
            {tab === 'prompt' && (
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-sm text-gray-500">当前专家的完整 System Prompt（含四大原则 + 长期记忆）</span>
                  <button onClick={loadPrompt} disabled={loadingPrompt}
                    className="px-3 py-1 border rounded text-xs hover:bg-gray-50 disabled:opacity-50">
                    {loadingPrompt ? '加载中…' : '刷新'}
                  </button>
                </div>
                <pre className="text-xs bg-gray-900 text-green-300 rounded p-4 whitespace-pre-wrap overflow-y-auto max-h-[70vh]">
                  {prompt || '（点击刷新加载）'}
                </pre>
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-gray-400">
          从左侧选择一位专家开始配置或训练
        </div>
      )}
    </div>
  )
}

// ─── 知识库面板 ───────────────────────────────────────────────────────────────

function KnowledgePanel({ expert, onUpdated }: { expert: Expert; onUpdated: () => void }) {
  const [tab, setTab] = useState<'text' | 'file'>('text')
  const [text, setText] = useState('')
  const [sourceName, setSourceName] = useState('')
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  async function submitText() {
    if (!text.trim()) return
    setUploading(true); setResult(null)
    const res = await uploadKnowledgeText(expert.expert_id, text, sourceName || '粘贴文本')
    setUploading(false)
    if (res.success) {
      setResult(`✓ 已存入长期记忆（${res.compressed ? '已压缩提炼' : '直接存入'}，${res.summary_length} 字）`)
      setText(''); setSourceName('')
      onUpdated()
    } else {
      setResult(`✗ 失败：${res.error || '未知错误'}`)
    }
  }

  async function submitFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true); setResult(null)
    const res = await uploadKnowledgeFile(expert.expert_id, file)
    setUploading(false)
    if (res.success) {
      setResult(`✓ 文件「${file.name}」已存入长期记忆（${res.compressed ? '已压缩提炼' : '直接存入'}，${res.summary_length} 字）`)
      onUpdated()
    } else {
      setResult(`✗ 失败：${res.error || '未知错误'}`)
    }
    if (fileRef.current) fileRef.current.value = ''
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-500">
        向专家喂入知识文件或文本，内容会自动提炼后注入长期记忆，下次执行任务时自动生效。
      </p>
      <div className="flex gap-2 border-b">
        {(['text', 'file'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-3 py-1 text-sm border-b-2 -mb-px ${tab === t ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500'}`}>
            {t === 'text' ? '粘贴文本' : '上传文件'}
          </button>
        ))}
      </div>
      {tab === 'text' ? (
        <div className="space-y-2">
          <input className="w-full border rounded px-2 py-1 text-sm" placeholder="来源名称（可选，如：API设计规范）"
            value={sourceName} onChange={e => setSourceName(e.target.value)} />
          <textarea rows={8} className="w-full border rounded px-2 py-1 text-sm font-mono"
            placeholder="粘贴技术文档、规范、代码示例、项目背景…"
            value={text} onChange={e => setText(e.target.value)} />
          <button onClick={submitText} disabled={uploading || !text.trim()}
            className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700 disabled:opacity-50">
            {uploading ? '处理中…' : '存入长期记忆'}
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-gray-400">支持 txt / md / pdf / docx / json / csv，最大 10MB</p>
          <input ref={fileRef} type="file" accept=".txt,.md,.pdf,.docx,.doc,.json,.csv"
            onChange={submitFile} disabled={uploading}
            className="block w-full text-sm text-gray-500 file:mr-3 file:py-1 file:px-3 file:rounded file:border-0 file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100" />
          {uploading && <p className="text-sm text-gray-400">上传并提炼中…</p>}
        </div>
      )}
      {result && (
        <div className={`text-sm px-3 py-2 rounded ${result.startsWith('✓') ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
          {result}
        </div>
      )}
      {expert.long_term_memory && (
        <div>
          <div className="text-xs text-gray-500 mb-1">当前长期记忆预览（前 500 字）</div>
          <pre className="text-xs bg-gray-50 border rounded p-2 whitespace-pre-wrap max-h-40 overflow-y-auto">
            {expert.long_term_memory.slice(0, 500)}{expert.long_term_memory.length > 500 ? '…' : ''}
          </pre>
        </div>
      )}
    </div>
  )
}

// ─── 四大原则展示组件 ─────────────────────────────────────────────────────────

function PrinciplesPanel() {
  const principles = [
    {
      name: 'Plan Before Execute',
      desc: '先在内部完整规划，再输出。不边想边写，不自相矛盾。需求审核与执行输出严格分离。',
      color: 'border-blue-400 bg-blue-50',
      badge: 'bg-blue-100 text-blue-700',
    },
    {
      name: 'Agent-First',
      desc: '先确认需求，再动手执行。思考过程不混入输出。收到任务时先明确：执行什么 / 对象是谁 / 执行标准是什么。',
      color: 'border-purple-400 bg-purple-50',
      badge: 'bg-purple-100 text-purple-700',
    },
    {
      name: 'Test-Driven',
      desc: '输出前必须模拟验证：代码可运行，答案可验证，不确定不输出。',
      color: 'border-green-400 bg-green-50',
      badge: 'bg-green-100 text-green-700',
    },
    {
      name: 'Immutability',
      desc: '有错误不打补丁，直接用新的完整版本替换旧状态。',
      color: 'border-orange-400 bg-orange-50',
      badge: 'bg-orange-100 text-orange-700',
    },
    {
      name: 'Security-First',
      desc: '安全是底线不是选项。不确定就说不确定，不用模糊答案充数。',
      color: 'border-red-400 bg-red-50',
      badge: 'bg-red-100 text-red-700',
    },
  ]
  return (
    <div className="mb-4">
      <div className="text-xs text-gray-500 mb-2 font-medium">📋 全局四大原则（所有专家强制遵守，不可关闭）</div>
      <div className="grid grid-cols-1 gap-2">
        {principles.map(p => (
          <div key={p.name} className={`border-l-4 rounded px-3 py-2 ${p.color}`}>
            <span className={`text-xs font-semibold px-1.5 py-0.5 rounded mr-2 ${p.badge}`}>{p.name}</span>
            <span className="text-xs text-gray-600">{p.desc}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── WorkMode 配置面板 ────────────────────────────────────────────────────────

function WorkModePanel({ expert, onSaved }: { expert: Expert; onSaved: () => void }) {
  const wm = expert.work_mode || {} as WorkMode
  const [form, setForm] = useState<Partial<WorkMode>>({
    thinking_style: wm.thinking_style || 'chain_of_thought',
    execution_style: wm.execution_style || 'balanced',
    verbosity: wm.verbosity || 'normal',
    enable_cot: wm.enable_cot ?? true,
    enable_self_check: wm.enable_self_check ?? true,
    custom_prompt_addon: wm.custom_prompt_addon || '',
  })
  const [framework, setFramework] = useState(expert.thinking_framework || '')
  const [workLogic, setWorkLogic] = useState(expert.work_mode?.custom_prompt_addon || '')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  async function save() {
    setSaving(true)
    await updateWorkMode(expert.expert_id, { ...form, custom_prompt_addon: workLogic })
    await updateExpert(expert.expert_id, { thinking_framework: framework })
    setSaving(false); setSaved(true)
    setTimeout(() => setSaved(false), 2000)
    onSaved()
  }

  const sel = 'w-full border rounded px-2 py-1 text-sm bg-white'
  const sectionTitle = 'font-medium text-gray-700 text-sm mb-2 mt-4 border-b pb-1'

  return (
    <div className="space-y-2 max-w-2xl">
      {/* 四大原则展示 */}
      <PrinciplesPanel />

      <div className={sectionTitle}>思考与执行模式</div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-xs text-gray-500 mb-1">思考方式</label>
          <select className={sel} value={form.thinking_style}
            onChange={e => setForm(f => ({ ...f, thinking_style: e.target.value as WorkMode['thinking_style'] }))}>
            <option value="chain_of_thought">链式思考（CoT）</option>
            <option value="step_by_step">逐步推理</option>
            <option value="direct">直接输出</option>
            <option value="socratic">苏格拉底式追问</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">执行风格</label>
          <select className={sel} value={form.execution_style}
            onChange={e => setForm(f => ({ ...f, execution_style: e.target.value as WorkMode['execution_style'] }))}>
            <option value="conservative">保守稳健</option>
            <option value="balanced">均衡</option>
            <option value="aggressive">激进快速</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">输出详细度</label>
          <select className={sel} value={form.verbosity}
            onChange={e => setForm(f => ({ ...f, verbosity: e.target.value as WorkMode['verbosity'] }))}>
            <option value="brief">简洁</option>
            <option value="normal">正常</option>
            <option value="detailed">详细</option>
          </select>
        </div>
        <div className="flex flex-col gap-2 pt-4">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={!!form.enable_cot}
              onChange={e => setForm(f => ({ ...f, enable_cot: e.target.checked }))} />
            启用 CoT 推理
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={!!form.enable_self_check}
              onChange={e => setForm(f => ({ ...f, enable_self_check: e.target.checked }))} />
            输出前自检
          </label>
        </div>
      </div>

      <div className={sectionTitle}>思考框架与工作逻辑</div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">思考框架（注入 system prompt）</label>
        <textarea rows={2} className="w-full border rounded px-2 py-1 text-sm"
          placeholder="如：先分解问题，再逐步推理，最后验证结论"
          value={framework} onChange={e => setFramework(e.target.value)} />
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">工作逻辑与自定义指令（追加到 system prompt 末尾）</label>
        <p className="text-xs text-gray-400 mb-1">
          描述该专家的具体工作方式、决策逻辑、输出规范。会直接注入到每次任务的 system prompt 中。
        </p>
        <textarea rows={6} className="w-full border rounded px-2 py-1 text-sm font-mono"
          placeholder={`示例：
1. 收到任务时先确认：执行什么 / 对象是谁 / 标准是什么
2. 代码必须有类型注解和错误处理
3. 不确定时直接说不确定，不猜测
4. 输出前先在内部验证逻辑正确性`}
          value={workLogic}
          onChange={e => setWorkLogic(e.target.value)} />
      </div>

      <button onClick={save} disabled={saving}
        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
        {saving ? '保存中…' : saved ? '✓ 已保存' : '保存配置'}
      </button>
    </div>
  )
}

// ─── 训练对话面板 ─────────────────────────────────────────────────────────────

function TrainChatPanel({ expert, onMemoryUpdated }: { expert: Expert; onMemoryUpdated: () => void }) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [feedbackTarget, setFeedbackTarget] = useState<{ idx: number; content: string } | null>(null)
  const [fbForm, setFbForm] = useState({ type: 'correction', feedback: '', correction: '' })
  const [pendingCount, setPendingCount] = useState(0)
  const [applying, setApplying] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchTraining(expert.expert_id).then(d => setPendingCount(d.pending || 0))
  }, [expert.expert_id])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs])

  async function send() {
    if (!input.trim()) return
    const userMsg: ChatMsg = { role: 'user', content: input }
    const newMsgs = [...msgs, userMsg]
    setMsgs(newMsgs); setInput(''); setLoading(true)
    const res = await chatTrain(expert.expert_id, input, msgs)
    setMsgs([...newMsgs, { role: 'assistant', content: res.reply || '（无回复）' }])
    setLoading(false)
  }

  async function submitFeedback() {
    if (!feedbackTarget || !fbForm.feedback.trim()) return
    const assistantMsg = msgs[feedbackTarget.idx]
    const userMsg = feedbackTarget.idx > 0 ? msgs[feedbackTarget.idx - 1] : null
    await sendFeedback(expert.expert_id, {
      session_type: fbForm.type,
      user_input: userMsg?.content || '',
      agent_output: assistantMsg.content,
      feedback: fbForm.feedback,
      correction: fbForm.correction,
      auto_apply: false,
    })
    setPendingCount(c => c + 1)
    setFeedbackTarget(null)
    setFbForm({ type: 'correction', feedback: '', correction: '' })
  }

  async function doApply() {
    setApplying(true)
    await applyTraining(expert.expert_id)
    setApplying(false); setPendingCount(0)
    onMemoryUpdated()
  }

  return (
    <div className="flex flex-col h-full">
      {pendingCount > 0 && (
        <div className="flex items-center justify-between bg-yellow-50 border border-yellow-200 rounded px-3 py-2 mb-2 text-sm">
          <span>有 <b>{pendingCount}</b> 条待应用的训练反馈</span>
          <button onClick={doApply} disabled={applying}
            className="px-3 py-1 bg-yellow-500 text-white rounded text-xs hover:bg-yellow-600 disabled:opacity-50">
            {applying ? '提炼中…' : '提炼到长期记忆'}
          </button>
        </div>
      )}
      <div className="flex-1 overflow-y-auto space-y-3 mb-3 min-h-0">
        {msgs.length === 0 && (
          <div className="text-center text-gray-400 text-sm pt-8">
            向专家提问，测试他的回答质量。<br />对不满意的回答点「纠正」给出反馈。
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
              m.role === 'user' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-800'}`}>
              {m.content}
              {m.role === 'assistant' && (
                <button onClick={() => setFeedbackTarget({ idx: i, content: m.content })}
                  className="block mt-1 text-xs text-gray-400 hover:text-red-500">纠正此回答</button>
              )}
            </div>
          </div>
        ))}
        {loading && <div className="text-gray-400 text-sm pl-2">专家思考中…</div>}
        <div ref={bottomRef} />
      </div>
      {feedbackTarget && (
        <div className="border rounded p-3 mb-2 bg-orange-50 text-sm space-y-2">
          <div className="font-medium text-orange-700">纠正专家回答</div>
          <select className="w-full border rounded px-2 py-1 text-xs"
            value={fbForm.type} onChange={e => setFbForm(f => ({ ...f, type: e.target.value }))}>
            <option value="correction">纠错（回答有误）</option>
            <option value="style_tune">风格调整（表达方式）</option>
            <option value="logic_tune">逻辑调整（思考方式）</option>
            <option value="output_tune">格式调整（输出结构）</option>
          </select>
          <textarea rows={2} placeholder="哪里不对？（必填）" className="w-full border rounded px-2 py-1 text-xs"
            value={fbForm.feedback} onChange={e => setFbForm(f => ({ ...f, feedback: e.target.value }))} />
          <textarea rows={2} placeholder="期望的正确回答（可选）" className="w-full border rounded px-2 py-1 text-xs"
            value={fbForm.correction} onChange={e => setFbForm(f => ({ ...f, correction: e.target.value }))} />
          <div className="flex gap-2">
            <button onClick={submitFeedback} className="px-3 py-1 bg-orange-500 text-white rounded text-xs hover:bg-orange-600">记录反馈</button>
            <button onClick={() => setFeedbackTarget(null)} className="px-3 py-1 border rounded text-xs">取消</button>
          </div>
        </div>
      )}
      <div className="flex gap-2">
        <input className="flex-1 border rounded px-3 py-2 text-sm" placeholder="向专家提问…"
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), send())} />
        <button onClick={send} disabled={loading || !input.trim()}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">发送</button>
      </div>
    </div>
  )
}

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

// ─── 导出（子组件全部定义完毕后再导出主组件）────────────────────────────────────

export default ExpertPoolPage
