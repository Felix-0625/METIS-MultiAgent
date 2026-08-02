import React, { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { API_BASE_URL } from '../services/apiBase'
import { buildRepairApplyTarget } from './fileBrowserDefectState'

const API = (p: string) => `${API_BASE_URL}${p}`
const apiFetch = (p: string, init: RequestInit = {}) => fetch(API(p), { ...init, credentials: 'include' })

// ── 类型定义 ────────────────────────────────────────────────────────────────
interface ChatMsg { role: 'user' | 'assistant'; content: string }
interface Defect {
  id: string; message: string; file_path: string; severity: string
  status: string; fix_hint: string; subproject_name: string; subproject_id: string
  detected_phase?: string; phase_id?: string
  observation_id: string; fix_rounds?: number; needs_manual_reason?: string
  action_allowed?: boolean; blocked_reason?: string
  authoritative_run_identity?: Record<string, string> | null
  identity_confidence?: string; requires_identity_review?: boolean
}

const canRepairDefect = (defect?: Defect | null) => !!defect
  && defect.action_allowed === true
  && !!defect.file_path?.trim()
  && ['needs_manual', 'open'].includes(defect.status)
  && defect.identity_confidence === 'high'
  && defect.requires_identity_review !== true
interface WorkspaceFile {
  file_path: string; agent_role?: string; agent_id?: string; phase_id?: string
  phase_name?: string; size_bytes?: number
}
interface ArchiveFile {
  file_path: string; category: string; phase_name: string
  size_bytes: number; extension: string; is_useless: boolean
}
interface ArchiveResult {
  files: ArchiveFile[]; stats: Record<string, number>; total: number
}

// ── 颜色辅助 ────────────────────────────────────────────────────────────────
const CAT_COLOR: Record<string, string> = {
  source: 'bg-blue-100 text-blue-700',
  test: 'bg-green-100 text-green-700',
  doc: 'bg-yellow-100 text-yellow-700',
  config: 'bg-purple-100 text-purple-700',
  output: 'bg-gray-100 text-gray-600',
  useless: 'bg-red-100 text-red-400 line-through',
}
const SEV_COLOR: Record<string, string> = {
  error: 'text-red-600 font-semibold',
  warning: 'text-yellow-600',
}

// ── 通用聊天气泡 ────────────────────────────────────────────────────────────
function ChatBubble({ msg }: { msg: ChatMsg }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-3`}>
      <div className={`max-w-[80%] rounded-2xl px-4 py-2 text-sm whitespace-pre-wrap shadow-sm
        ${isUser ? 'bg-blue-500 text-white' : 'bg-white border border-gray-200 text-gray-800'}`}>
        {msg.content}
      </div>
    </div>
  )
}

// ── 聊天输入框 ──────────────────────────────────────────────────────────────
function ChatInput({ onSend, loading, placeholder }: {
  onSend: (v: string) => void; loading: boolean; placeholder?: string
}) {
  const [val, setVal] = useState('')
  const send = () => { if (val.trim()) { onSend(val.trim()); setVal('') } }
  return (
    <div className="flex gap-2 pt-2 border-t border-gray-100">
      <input
        className="flex-1 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
        placeholder={placeholder || '输入消息...'}
        value={val}
        onChange={e => setVal(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
        disabled={loading}
      />
      <button
        onClick={send}
        disabled={loading || !val.trim()}
        className="px-4 py-2 bg-blue-500 text-white rounded-lg text-sm disabled:opacity-40 hover:bg-blue-600 transition"
      >
        {loading ? '...' : '发送'}
      </button>
    </div>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 面板一：代码整改（每个 defect 独立上下文）
// ══════════════════════════════════════════════════════════════════════════════
// ── LocalStorage 持久化工具 ─────────────────────────────────────────────────
function lsGet<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback } catch { return fallback }
}
function lsSet(key: string, val: unknown) {
  try { localStorage.setItem(key, JSON.stringify(val)) } catch {}
}

function RepairPanel({
  projectId,
  onOpenProjectIssue,
}: {
  projectId: string
  onOpenProjectIssue: (defect: Defect) => void
}) {
  // ── 状态 ────────────────────────────────────────────────────────────────
  const [fileTree, setFileTree]   = useState<any[]>([])
  const [defects, setDefects]     = useState<Defect[]>([])
  const [sel, setSel]             = useState<Defect | null>(null)
  const [selFile, setSelFile]     = useState<string | null>(null)
  const [preview, setPreview]     = useState<{ path: string; content: string } | null>(null)
  const [history, setHistory]     = useState<Record<string, ChatMsg[]>>(() =>
    lsGet(`eng_repair_history_${projectId}`, {})
  )
  const [planVersions, setPlanVersions] = useState<Record<string, string>>({})
  const [proposals, setProposals] = useState<Record<string, {
    digest: string; confirmable: boolean; error?: string; wholeFile: boolean
  }>>({})
  const [loading, setLoading]     = useState(false)
  const [applyForm, setApplyForm] = useState<{ filePath: string; content: string } | null>(null)
  // 质检
  const [qaRunning, setQaRunning]         = useState(false)
  const [subprojectId, setSubprojectId]   = useState('')
  const [subprojectList, setSubprojectList] = useState<{ id: string; name: string }[]>([])
  const [phaseFilePaths, setPhaseFilePaths] = useState<Record<string, Set<string>>>({})
  const [qaLog, setQaLog]                 = useState<string[]>([])
  const qaPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [defectFilter, setDefectFilter]   = useState<string>('all')
  const [phaseFilter, setPhaseFilter]     = useState<string>('all')
  const [issueScopeFilter, setIssueScopeFilter] = useState<'all' | 'file' | 'project'>('all')
  const [pendingPhaseFilter, setPendingPhaseFilter] = useState('all')
  const [pendingIssueScopeFilter, setPendingIssueScopeFilter] = useState<'all' | 'file' | 'project'>('all')
  // 展开/折叠完整交付文件树
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => { loadAll() }, [projectId])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [history, sel])
  useEffect(() => { lsSet(`eng_repair_history_${projectId}`, history) }, [history, projectId])
  useEffect(() => () => {
    if (qaPollRef.current) clearInterval(qaPollRef.current)
  }, [])

  const loadAll = async () => {
    const [dRes, fRes, spRes] = await Promise.all([
      // 不传 status 参数，获取所有缺陷，由前端本地过滤，确保拿到最新状态
      apiFetch(`/engineer/${projectId}/all-defects`).then(x => x.json()),
      apiFetch(`/projects/${projectId}/files`).then(x => x.json()),
      apiFetch(`/projects/${projectId}/phases`).then(x => x.json()).catch(() => ({ phases: [] })),
    ])
    // 后端按最新 observation 去重；已验证缺陷后续复现时仍会重新打开。
    setDefects(dRes.defects || [])
    const nextTree = fRes.tree || []
    setFileTree(nextTree)
    setExpanded(current => {
      if (current.size > 0) return current
      return new Set(nextTree.filter((node: any) => node.type !== 'file').map((node: any) => node.key))
    })
    const spList: { id: string; name: string }[] = (spRes.phases || []).map((s: any) => ({
      id: s.phase_id,
      name: s.name || s.phase_name || s.phase_id,
    }))
    setSubprojectList(spList)
    const phaseFiles = await Promise.all(spList.map(async phase => {
      const payload = await apiFetch(`/projects/${projectId}/phases/${phase.id}/files`)
        .then(response => response.ok ? response.json() : { files: [] })
        .catch(() => ({ files: [] }))
      const paths = (payload.files || [])
        .map((file: any) => String(file.path || file.file_path || file.key || ''))
        .filter(Boolean)
      return [phase.id, new Set<string>(paths)] as const
    }))
    setPhaseFilePaths(Object.fromEntries(phaseFiles))
    // 默认选中第一个子项目
    if (spList.length > 0 && !subprojectId) setSubprojectId(spList[0].id)
  }

  // defect 按文件路径分组
  const activeDefects = defects.filter(d =>
    ['needs_manual', 'open', 'fixing', 'pending_verification'].includes(d.status)
  )
  const defectsByFile = activeDefects.reduce<Record<string, Defect[]>>((acc, d) => {
    if (!acc[d.file_path]) acc[d.file_path] = []
    acc[d.file_path].push(d)
    return acc
  }, {})
  const allDefectsByFile = defects.reduce<Record<string, Defect[]>>((acc, defect) => {
    if (!defect.file_path) return acc
    if (!acc[defect.file_path]) acc[defect.file_path] = []
    acc[defect.file_path].push(defect)
    return acc
  }, {})

  const FILE_STATUS: Record<string, { label: string; className: string }> = {
    needs_manual: { label: '待整改', className: 'bg-orange-100 text-orange-700' },
    open: { label: '待修复', className: 'bg-red-100 text-red-700' },
    fixing: { label: '修复中', className: 'bg-blue-100 text-blue-700' },
    pending_verification: { label: '待复检', className: 'bg-amber-100 text-amber-700' },
    fixed: { label: '已修复', className: 'bg-green-100 text-green-700' },
    verified: { label: '已验证', className: 'bg-teal-100 text-teal-700' },
    escalated: { label: '已升级', className: 'bg-purple-100 text-purple-700' },
  }
  const FILE_STATUS_PRIORITY = [
    'needs_manual', 'open', 'fixing', 'pending_verification', 'escalated', 'fixed', 'verified',
  ]

  // 扁平化文件树节点
  const flattenNodes = (nodes: any[]): any[] =>
    nodes.flatMap(n => n.type === 'file' ? [n] : flattenNodes(n.children || []))

  const allFiles = flattenNodes(fileTree)

  const fileMatchesFilters = (path: string) => {
    if (issueScopeFilter === 'project') return false
    const fileDefects = defects.filter(defect => defect.file_path === path)
    const phaseMatches = phaseFilter === 'all'
      || phaseFilePaths[phaseFilter]?.has(path)
      || fileDefects.some(defect => defect.detected_phase === phaseFilter || defect.phase_id === phaseFilter)
    return phaseMatches
  }

  const filterFileNodes = (nodes: any[]): any[] => nodes.flatMap((node: any) => {
    if (node.type === 'file') return fileMatchesFilters(String(node.key)) ? [node] : []
    const children = filterFileNodes(node.children || [])
    return children.length > 0 ? [{ ...node, children }] : []
  })
  const filteredFileTree = filterFileNodes(fileTree)
  const visibleFiles = flattenNodes(filteredFileTree)

  const toggleDirectory = (path: string) => {
    setExpanded(current => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const getFileIcon = (name: string) => {
    const ext = name.split('.').pop()?.toLowerCase() || ''
    if (['ts','tsx','js','jsx','py','java','go','rs'].includes(ext)) return '📄'
    if (['md','txt','rst'].includes(ext)) return '📝'
    if (['json','yaml','yml','toml'].includes(ext)) return '⚙️'
    return '📃'
  }

  const handleFileClick = async (filePath: string) => {
    setSelFile(filePath)
    setPreview(null)
    const ds = defectsByFile[filePath]
    if (ds && ds.length > 0) {
      setSel(ds.find(canRepairDefect) || ds[0])
    } else {
      setSel(null)
      // 预览文件内容
      try {
        const r = await apiFetch(`/projects/${projectId}/files/read?path=${encodeURIComponent(filePath)}`).then(x => x.json())
        setPreview({ path: filePath, content: r.content || '（空文件）' })
      } catch {}
    }
  }

  const msgs = sel ? (history[sel.id] || []) : []

  const sendRepair = async (text: string) => {
    if (!sel) return
    if (!canRepairDefect(sel)) return
    const newMsgs: ChatMsg[] = [...msgs, { role: 'user', content: text }]
    setHistory(h => ({ ...h, [sel.id]: newMsgs }))
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/chat/repair`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          defect_id: sel.id,
          message: text,
          defect_info: msgs.length === 0 ? sel : undefined,
          all_defects: defects,
        }),
      })
      const r = await response.json()
      if (!response.ok) throw new Error(r.detail || '整改对话请求被拒绝')
      if (r.proposal_digest) {
        setProposals(current => ({
          ...current,
          [sel.id]: {
            digest: r.proposal_digest,
            confirmable: !!r.proposal_confirmable,
            error: r.proposal_error,
            wholeFile: !!r.requires_whole_file_authorization,
          },
        }))
      }
      setHistory(h => ({ ...h, [sel.id]: [...newMsgs, { role: 'assistant', content: r.reply || '' }] }))
    } catch (e: any) {
      setHistory(h => ({
        ...h,
        [sel.id]: [...newMsgs, { role: 'assistant', content: `❌ ${e.message || '整改对话失败'}` }],
      }))
    } finally { setLoading(false) }
  }

  const confirmProposal = async () => {
    if (!sel) return
    if (!canRepairDefect(sel)) return
    const proposal = proposals[sel.id]
    if (!proposal?.confirmable) {
      setHistory(current => ({
        ...current,
        [sel.id]: [
          ...(current[sel.id] || []),
          { role: 'assistant', content: `❌ ${proposal?.error || '当前 proposal 缺少可验证的变更范围'}` },
        ],
      }))
      return
    }
    const allowWholeFile = proposal.wholeFile
      ? window.confirm('这是 whole-file 高风险整改授权。确认允许替换整个文件吗？')
      : false
    if (proposal.wholeFile && !allowWholeFile) return
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/confirm-fix-plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          defect_id: sel.id,
          proposal_digest: proposal.digest,
          observation_id: sel.observation_id,
          file_path: sel.file_path,
          allow_whole_file: allowWholeFile,
        }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '方案确认失败')
      setPlanVersions(current => ({
        ...current,
        [sel.id]: result.confirmed_plan_version,
      }))
      setHistory(current => ({
        ...current,
        [sel.id]: [
          ...(current[sel.id] || []),
          { role: 'assistant', content: '✅ 整改方案已通过独立确认，并绑定当前文件 baseline。' },
        ],
      }))
    } catch (e: any) {
      setHistory(current => ({
        ...current,
        [sel.id]: [
          ...(current[sel.id] || []),
          { role: 'assistant', content: `❌ 方案确认失败：${e.message}` },
        ],
      }))
    } finally {
      setLoading(false)
    }
  }

  const applyFix = async () => {
    if (!sel || !applyForm) return
    if (!canRepairDefect(sel)) return
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/apply-fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...buildRepairApplyTarget(
            { id: sel.id, file_path: applyForm.filePath },
            planVersions,
          ),
          new_content: applyForm.content,
          run_qa: true,
          defect_info: sel,
        }),
      })
      const r = await response.json()
      if (!response.ok) throw new Error(r.detail || r.message || '写入被拒绝')
      setApplyForm(null)
      await loadAll()
      const msg: ChatMsg = {
        role: 'assistant',
        content: r.success
          ? `✅ 整改已原子写入（第${r.edit_count}次）：${r.message}\n当前状态：等待 authoritative Supervisor/Final QA 复检；静态预检不代表最终通过。${r.exceeded ? '\n⚠️ 已达修改上限' : ''}`
          : `❌ 写入被拒绝：${r.message || r.error}`,
      }
      setHistory(h => ({ ...h, [sel.id]: [...(h[sel.id] || []), msg] }))
    } catch (e: any) {
      setHistory(h => ({
        ...h,
        [sel.id]: [...(h[sel.id] || []), { role: 'assistant', content: `❌ 写入被拒绝：${e.message}` }],
      }))
    } finally { setLoading(false) }
  }

  const runQA = async () => {
    if (!subprojectId.trim()) { alert('请填写子项目ID'); return }
    const qaPhase = subprojectId.trim()
    setQaRunning(true)
    setQaLog(prev => [...prev, `▶ 触发质检：${qaPhase}`])
    try {
      const response = await apiFetch(`/engineer/${projectId}/run-qa`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subproject_id: qaPhase }),
      })
      const r = await response.json()
      if (!response.ok) throw new Error(r.detail || 'authoritative QA 启动失败')
      const state = r.status?.status || r.status || 'starting'
      setQaLog(prev => [...prev, `▶ authoritative QA 状态：${state}`])
      await loadAll()
      const terminal = new Set([
        'passed', 'needs_manual', 'failed', 'infrastructure_blocked',
        'interrupted', 'quality_regressed', 'no_progress', 'qa_blocked',
        'stale', 'blocked', 'rebuild_recovery_required',
      ])
      if (!terminal.has(String(state))) {
        if (qaPollRef.current) clearInterval(qaPollRef.current)
        qaPollRef.current = setInterval(async () => {
          try {
            const pollResponse = await apiFetch(
              `/projects/${projectId}/phases/${qaPhase}/auto-repair/status`
            )
            const poll = await pollResponse.json()
            if (!pollResponse.ok) throw new Error(poll.detail || 'QA 状态查询失败')
            const pollState = String(poll.status || 'unknown')
            setQaLog(prev => {
              const next = `• authoritative QA 状态：${pollState}`
              return prev[prev.length - 1] === next ? prev : [...prev, next]
            })
            if (terminal.has(pollState)) {
              if (qaPollRef.current) clearInterval(qaPollRef.current)
              qaPollRef.current = null
              setQaRunning(false)
              await loadAll()
            }
          } catch (e: any) {
            if (qaPollRef.current) clearInterval(qaPollRef.current)
            qaPollRef.current = null
            setQaRunning(false)
            setQaLog(prev => [...prev, `❌ QA 状态查询失败：${e.message}`])
          }
        }, 2000)
        return
      }
    } catch (e: any) {
      setQaLog(prev => [...prev, `❌ 出错：${e.message}`])
    } finally {
      if (!qaPollRef.current) setQaRunning(false)
    }
  }

  const problemCount = Object.keys(defectsByFile).length

  const renderFileNodes = (nodes: any[], depth = 0): React.ReactNode => nodes.map((node: any) => {
    const path = String(node.key || node.title || '')
    if (node.type !== 'file') {
      const isExpanded = expanded.has(path)
      const descendantFiles = flattenNodes(node.children || [])
      const descendantProblems = descendantFiles.reduce(
        (count, file) => count + (defectsByFile[String(file.key)]?.length || 0),
        0,
      )
      return (
        <React.Fragment key={path}>
          <button
            type="button"
            onClick={() => toggleDirectory(path)}
            className="w-full flex items-center gap-1.5 py-1.5 pr-2 text-xs text-gray-700 hover:bg-gray-50"
            style={{ paddingLeft: `${8 + depth * 14}px` }}
            title={path}
          >
            <span className="w-3 text-gray-400">{isExpanded ? '▾' : '▸'}</span>
            <span>{isExpanded ? '📂' : '📁'}</span>
            <span className="font-medium truncate">{node.title}</span>
            <span className="ml-auto text-gray-400">{descendantFiles.length}</span>
            {descendantProblems > 0 && <span className="text-orange-500">⚠ {descendantProblems}</span>}
          </button>
          {isExpanded && renderFileNodes(node.children || [], depth + 1)}
        </React.Fragment>
      )
    }

    const ds = defectsByFile[path]
    const fileDefects = allDefectsByFile[path] || []
    const primaryStatus = FILE_STATUS_PRIORITY.find(status =>
      fileDefects.some(defect => defect.status === status)
    )
    const statusTag = primaryStatus ? FILE_STATUS[primaryStatus] : undefined
    const hasDefect = !!ds?.length
    const isSelected = selFile === path
    return (
      <button
        type="button"
        key={path}
        onClick={() => handleFileClick(path)}
        className={`w-full text-left flex items-center gap-1.5 py-1.5 pr-2 text-xs transition border-l-2
          ${isSelected
            ? hasDefect ? 'bg-orange-50 border-orange-400' : 'bg-blue-50 border-blue-400'
            : 'border-transparent hover:bg-gray-50'}`}
        style={{ paddingLeft: `${22 + depth * 14}px` }}
        title={path}
      >
        <span className="flex-shrink-0">{getFileIcon(node.title)}</span>
        <span className={`truncate font-mono ${hasDefect ? 'text-orange-700' : 'text-gray-700'}`}>
          {node.title}
        </span>
        {statusTag ? (
          <span className={`ml-auto flex-shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${statusTag.className}`}>
            {statusTag.label}{fileDefects.length > 1 ? ` ${fileDefects.length}` : ''}
          </span>
        ) : (
          <span className="ml-auto flex-shrink-0 text-green-400">✓</span>
        )}
      </button>
    )
  })

  return (
    <div className="flex h-full gap-3">
      {/* ── 左侧：完整交付文件树 ────────────────────────────────────────── */}
      <div className="w-72 flex-shrink-0 flex flex-col gap-2">
        {/* 文件看板卡片 */}
        <div className="flex-1 bg-white rounded-xl border border-gray-200 flex flex-col overflow-hidden">
          {/* 卡片头 */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100 flex-shrink-0">
            <div className="flex items-center gap-1.5">
              <span className="text-yellow-500">📁</span>
              <span className="text-xs font-semibold text-gray-700">项目文件</span>
              <span className="text-xs text-gray-400 bg-gray-100 rounded-full px-1.5">{allFiles.length}</span>
              {problemCount > 0 && (
                <span className="text-xs text-orange-500 bg-orange-50 rounded-full px-1.5">⚠ {problemCount}</span>
              )}
            </div>
            <button onClick={loadAll} className="text-xs text-blue-400 hover:text-blue-600">↻</button>
          </div>
          <div className="grid grid-cols-1 gap-1.5 px-2 py-2 border-b border-gray-100 flex-shrink-0">
            <select aria-label="筛选文件阶段" value={pendingPhaseFilter}
              onChange={event => setPendingPhaseFilter(event.target.value)}
              className="w-full border rounded px-2 py-1 text-xs bg-white text-gray-700">
              <option value="all">全部阶段</option>
              {subprojectList.map(phase => <option key={phase.id} value={phase.id}>{phase.name}</option>)}
            </select>
            <select aria-label="筛选文件范围" value={pendingIssueScopeFilter}
              onChange={event => setPendingIssueScopeFilter(event.target.value as 'all' | 'file' | 'project')}
              className="w-full border rounded px-2 py-1 text-xs bg-white text-gray-700">
              <option value="all">全部问题范围</option>
              <option value="file">文件级问题</option>
              <option value="project">项目级问题</option>
            </select>
            <button
              type="button"
              onClick={() => {
                setPhaseFilter(pendingPhaseFilter)
                setIssueScopeFilter(pendingIssueScopeFilter)
                void loadAll()
              }}
              className="w-full rounded bg-blue-500 py-1.5 text-xs text-white hover:bg-blue-600"
            >
              查询
            </button>
          </div>
          {/* 文件列表 */}
          <div className="flex-1 overflow-y-auto">
            {visibleFiles.length === 0 ? (
              <div className="text-xs text-gray-400 text-center py-8 px-3">
                {issueScopeFilter === 'project'
                  ? '项目级问题不绑定单一文件，请查看右侧状态列表'
                  : allFiles.length === 0 ? '当前项目暂无可交付文件' : '当前筛选条件下没有文件'}
              </div>
            ) : (
              renderFileNodes(filteredFileTree)
            )}
          </div>
        </div>

        <div className="bg-blue-50 border border-blue-100 rounded-xl p-3 text-xs text-blue-700">
          整改完成后由全栈工程师执行语法、构建和相关测试自检，不进入阶段 QA/QC 循环。
        </div>
      </div>

      {/* ── 右侧：整改对话 / 文件预览 ──────────────────────────────────── */}
      <div className="flex-1 flex flex-col bg-white rounded-xl border border-gray-200 p-4 min-w-0">
        {!sel && !preview ? (
          /* 未选中文件时：显示缺陷汇总列表，点击直接进入整改对话 */
          (() => {
            const STATUS_CARD_STYLE: Record<string, string> = {
              needs_manual: 'border-orange-100 bg-orange-50 hover:bg-orange-100',
              open:         'border-red-100 bg-red-50 hover:bg-red-100',
              fixing:       'border-blue-100 bg-blue-50 hover:bg-blue-100',
              fixed:        'border-green-100 bg-green-50 hover:bg-green-100',
              verified:     'border-teal-100 bg-teal-50 hover:bg-teal-100',
              escalated:    'border-purple-100 bg-purple-50 hover:bg-purple-100',
            }
            const filtered = defects.filter(d =>
              (defectFilter === 'all' || d.status === defectFilter)
            )
            return (
              <div className="flex-1 flex flex-col min-h-0">
                {/* 顶栏 */}
                <div className="border-b pb-2 mb-2 flex items-center justify-between gap-2 flex-shrink-0">
                  <div className="font-semibold text-sm text-gray-700 flex-shrink-0">
                    状态列表
                    {defects.length > 0 && (
                      <span className="ml-2 text-xs font-normal text-gray-400">共 {defects.length} 条</span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 ml-auto">
                    <select aria-label="筛选状态列表" value={defectFilter}
                      onChange={event => setDefectFilter(event.target.value)}
                      className="border rounded px-2 py-1 text-xs bg-white text-gray-700">
                      <option value="all">全部状态</option>
                      <option value="needs_manual">需人工</option>
                      <option value="open">待修复</option>
                      <option value="fixing">修复中</option>
                      <option value="pending_verification">等待复检</option>
                      <option value="fixed">已修复</option>
                      <option value="verified">已验证</option>
                      <option value="escalated">已升级</option>
                    </select>
                  </div>
                </div>
                {/* 缺陷列表 */}
                {filtered.length === 0 ? (
                  <div className="flex-1 flex flex-col items-center justify-center text-gray-400 gap-2">
                    <span className="text-3xl">{defects.length === 0 ? '✅' : '🔍'}</span>
                    <div className="text-sm">{defects.length === 0 ? '暂无缺陷' : '该状态下暂无缺陷'}</div>
                    <div className="text-xs text-gray-300">
                      {defects.length === 0 ? '可触发质检后查看结果' : '切换上方状态筛选查看其他缺陷'}
                    </div>
                  </div>
                ) : (
                  <div className="flex-1 overflow-y-auto space-y-2">
                    {filtered.map(d => {
                      const cardStyle = STATUS_CARD_STYLE[d.status] || 'border-gray-100 bg-gray-50 hover:bg-gray-100'
                      const canRepair = canRepairDefect(d)
                      return (
                        <button
                          key={d.id}
                          onClick={() => canRepair
                            ? (setSel(d), setSelFile(d.file_path), setPreview(null))
                            : (!d.file_path && ['needs_manual', 'open'].includes(d.status)
                              ? onOpenProjectIssue(d)
                              : undefined)}
                          className={`w-full text-left p-3 rounded-lg border transition ${cardStyle} ${
                            !canRepair && !!d.file_path ? 'cursor-default opacity-80' : ''
                          }`}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="flex-1 min-w-0">
                              <div className={`text-xs font-semibold mb-0.5 ${SEV_COLOR[d.severity] || 'text-gray-700'}`}>
                                [{d.severity.toUpperCase()}] {d.message}
                              </div>
                              <span className={`inline-flex mb-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                                d.file_path ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700'
                              }`}>
                                {d.file_path ? '文件级' : '项目级'}
                              </span>
                              {d.file_path ? (
                                <div className="text-xs text-gray-500 font-mono truncate">{d.file_path}</div>
                              ) : (
                                <div className="text-xs text-purple-600 font-medium">项目级问题 · 未绑定单一文件</div>
                              )}
                              {d.needs_manual_reason && (
                                <div className="text-xs text-orange-500 mt-0.5 truncate">⚠ {d.needs_manual_reason}</div>
                              )}
                              {d.fix_hint && (
                                <div className="text-xs text-blue-500 mt-0.5 truncate">💡 {d.fix_hint}</div>
                              )}
                            </div>
                            <div className="flex flex-col items-end gap-1 flex-shrink-0">
                              <span className="text-xs text-gray-400 bg-white border border-gray-200 rounded px-1.5 py-0.5">
                                {d.subproject_name || d.subproject_id}
                              </span>
                              {(d.fix_rounds ?? 0) > 0 && (
                                <span className="text-xs text-red-400">已尝试 {d.fix_rounds} 次</span>
                              )}
                              {canRepair && <span className="text-xs text-purple-500">点击整改 →</span>}
                              {!d.file_path && ['needs_manual', 'open'].includes(d.status) && (
                                <span className="text-xs text-purple-500">进入项目整改 →</span>
                              )}
                              {d.status === 'fixed' && <span className="text-xs text-green-500">✓ 已修复</span>}
                              {d.status === 'verified' && <span className="text-xs text-teal-500">✓ 已验证</span>}
                              {d.status === 'fixing' && <span className="text-xs text-blue-500">⟳ 修复中</span>}
                              {d.status === 'pending_verification' && <span className="text-xs text-amber-500">⌛ 等待权威复检</span>}
                              {(d.requires_identity_review || d.identity_confidence !== 'high') && (
                                <span className="text-xs text-red-500">身份待复核</span>
                              )}
                              {d.status === 'escalated' && <span className="text-xs text-purple-500">⬆ 已升级</span>}
                            </div>
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })()
        ) : preview && !sel ? (
          /* 无缺陷文件：只展示内容预览 */
          <div className="flex-1 flex flex-col min-h-0">
            <div className="flex items-center justify-between border-b pb-2 mb-2 flex-shrink-0">
              <div className="text-xs font-semibold text-gray-600 font-mono">{preview.path}</div>
              <span className="text-xs text-green-500">✓ 无缺陷</span>
            </div>
            <div className="flex-1 overflow-auto bg-gray-900 rounded-lg py-3">
              <div className="min-w-max text-xs font-mono leading-relaxed">
                {preview.content.split('\n').map((line, index) => (
                  <div key={index} className="flex hover:bg-white/5">
                    <span className="w-12 px-3 text-right text-gray-500 select-none flex-shrink-0">{index + 1}</span>
                    <pre className="pr-4 text-green-300 whitespace-pre">{line || ' '}</pre>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : sel ? (
          /* 有缺陷文件：整改对话 */
          <>
            <div className="border-b pb-2 mb-3 flex-shrink-0">
              <div className="text-sm font-semibold text-gray-700">{sel.message}</div>
              <div className="text-xs text-gray-400">{sel.file_path} · {sel.subproject_name}</div>
              {sel.needs_manual_reason && (
                <div className="text-xs text-orange-500 mt-0.5 bg-orange-50 rounded px-2 py-0.5">
                  ⚠ {sel.needs_manual_reason}
                  {sel.fix_rounds ? `（已自动尝试 ${sel.fix_rounds} 次）` : ''}
                </div>
              )}
              {sel.fix_hint && <div className="text-xs text-blue-600 mt-0.5">建议：{sel.fix_hint}</div>}
              {!canRepairDefect(sel) && (
                <div className="text-xs text-gray-600 mt-1 bg-gray-50 rounded px-2 py-1">
                  {sel.requires_identity_review || sel.identity_confidence !== 'high'
                    ? '低置信缺陷必须先完成身份复核，当前不可整改'
                    : sel.status === 'fixing'
                      ? '整改任务正在执行，不能重复提交'
                      : '整改已写入，正在等待 authoritative Supervisor/Final QA 复检'}
                </div>
              )}
              {/* 同文件多个缺陷切换 */}
              {defectsByFile[sel.file_path]?.length > 1 && (
                <div className="flex gap-1 mt-1 flex-wrap">
                  {defectsByFile[sel.file_path].map(d => (
                    <button key={d.id} onClick={() => setSel(d)}
                      className={`text-xs px-2 py-0.5 rounded-full border transition
                        ${sel.id === d.id
                          ? 'bg-orange-100 border-orange-400 text-orange-700'
                          : 'border-gray-200 text-gray-500 hover:bg-gray-50'}`}>
                      {d.severity}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="flex-1 overflow-y-auto">
              {msgs.length === 0 && (
                <p className="text-xs text-gray-400 text-center py-4">发送消息开始整改对话</p>
              )}
              {msgs.map((m, i) => <ChatBubble key={i} msg={m} />)}
              <div ref={bottomRef} />
            </div>
            {applyForm && canRepairDefect(sel) && (
              <div className="border-t pt-2 mt-2 space-y-1 flex-shrink-0">
                <div className="text-xs font-semibold text-gray-600">执行写文件</div>
                <input className="w-full border rounded px-2 py-1 text-xs"
                  value={applyForm.filePath}
                  onChange={e => setApplyForm(f => f && ({ ...f, filePath: e.target.value }))} />
                <textarea className="w-full border rounded px-2 py-1 text-xs font-mono h-20 resize-none"
                  value={applyForm.content}
                  onChange={e => setApplyForm(f => f && ({ ...f, content: e.target.value }))} />
                <div className="flex gap-2">
                  <button onClick={applyFix} disabled={loading}
                    className="px-3 py-1 bg-green-500 text-white text-xs rounded disabled:opacity-40">
                    ✅ 确认写入
                  </button>
                  <button onClick={() => setApplyForm(null)}
                    className="px-3 py-1 bg-gray-200 text-gray-600 text-xs rounded">
                    取消
                  </button>
                </div>
              </div>
            )}
            {!applyForm && canRepairDefect(sel) && (
              <div className="flex gap-2 mt-1">
                {!planVersions[sel.id] && proposals[sel.id] && (
                  <button
                    onClick={() => void confirmProposal()}
                    disabled={loading || !proposals[sel.id].confirmable}
                    title={proposals[sel.id].error}
                    className="text-xs px-3 py-1 border border-blue-400 text-blue-600 rounded hover:bg-blue-50 disabled:opacity-40">
                    独立确认整改方案
                  </button>
                )}
                <button
                  onClick={() => setApplyForm({ filePath: sel.file_path, content: '' })}
                  disabled={!planVersions[sel.id]}
                  className="text-xs px-3 py-1 border border-green-400 text-green-600 rounded hover:bg-green-50 flex-shrink-0 disabled:opacity-40">
                  {planVersions[sel.id] ? '按已确认方案写入整改' : '请先独立确认整改方案'}
                </button>
              </div>
            )}
            {canRepairDefect(sel) && (
              <ChatInput onSend={sendRepair} loading={loading} placeholder="描述整改方案..." />
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}


// ══════════════════════════════════════════════════════════════════════════════
function DocsPanel({ projectId }: { projectId: string }) {
  const [type, setType] = useState('user')
  const [extra, setExtra] = useState('')
  // 每种类型的文档结果持久化（key: manual_type → result）
  const [results, setResults] = useState<Record<string, { content: string; file_path: string; char_count: number }>>(() =>
    lsGet(`eng_docs_results_${projectId}`, {})
  )
  const [loading, setLoading] = useState(false)
  const result = results[type] || null
  useEffect(() => { lsSet(`eng_docs_results_${projectId}`, results) }, [results, projectId])

  const generate = async () => {
    setLoading(true)
    try {
      const r = await apiFetch(`/engineer/${projectId}/generate-manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manual_type: type, extra_instruction: extra }),
      }).then(x => x.json())
      setResults(prev => ({ ...prev, [type]: r }))
    } finally { setLoading(false) }
  }

  const TYPES = [
    { v: 'user', label: '用户使用手册' },
    { v: 'api', label: 'API 接口文档' },
    { v: 'deploy', label: '部署运维手册' },
  ]

  return (
    <div className="h-full flex flex-col gap-4">
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex-shrink-0 space-y-3">
        <div className="font-semibold text-sm text-gray-700">生成使用手册</div>
        <div className="flex gap-2">
          {TYPES.map(t => (
            <button key={t.v} onClick={() => setType(t.v)}
              className={`px-3 py-1 rounded-full text-xs border transition
                ${type === t.v ? 'bg-blue-500 text-white border-blue-500' : 'border-gray-300 text-gray-600 hover:bg-gray-50'}`}>
              {t.label}
            </button>
          ))}
        </div>
        <textarea className="w-full border rounded-lg px-3 py-2 text-sm resize-none h-16 focus:outline-none focus:ring-2 focus:ring-blue-400"
          placeholder="额外要求（可选）" value={extra} onChange={e => setExtra(e.target.value)} />
        <button onClick={generate} disabled={loading}
          className="px-4 py-2 bg-blue-500 text-white rounded-lg text-sm disabled:opacity-40 hover:bg-blue-600 transition">
          {loading ? '生成中...' : '一键生成'}
        </button>
      </div>
      {result && (
        <div className="flex-1 bg-white rounded-xl border border-gray-200 p-4 overflow-y-auto">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-500">已保存：{result.file_path} · {result.char_count} 字</span>
          </div>
          <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">{result.content}</pre>
        </div>
      )}
    </div>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 面板三：文件归档
// ══════════════════════════════════════════════════════════════════════════════
function ArchivePanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ArchiveResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState('all')

  const loadCached = async () => {
    const r = await apiFetch(`/engineer/${projectId}/archived-files`).then(x => x.json())
    if (r.total > 0) setData(r)
  }
  useEffect(() => { loadCached() }, [projectId])

  const scan = async () => {
    setLoading(true)
    try {
      const r = await apiFetch(`/engineer/${projectId}/archive-files`, { method: 'POST' }).then(x => x.json())
      setData(r)
    } finally { setLoading(false) }
  }

  const files = data ? (filter === 'all' ? data.files : data.files.filter(f => f.category === filter)) : []

  return (
    <div className="h-full flex flex-col gap-3">
      <div className="bg-white rounded-xl border border-gray-200 p-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <div className="font-semibold text-sm text-gray-700">文件归档</div>
          <button onClick={scan} disabled={loading}
            className="px-3 py-1 bg-blue-500 text-white rounded text-xs disabled:opacity-40">
            {loading ? '扫描中...' : '扫描归档'}
          </button>
        </div>
        {data && (
          <div className="mt-2 flex flex-wrap gap-2">
            {(['all', 'source', 'test', 'doc', 'config', 'output', 'useless'] as const).map(c => {
              const cnt = c === 'all' ? data.total : (data.stats[c] || 0)
              return (
                <button key={c} onClick={() => setFilter(c)}
                  className={`px-2 py-0.5 rounded-full text-xs border transition
                    ${filter === c ? 'bg-blue-500 text-white border-blue-500' : 'border-gray-300 text-gray-600 hover:bg-gray-50'}`}>
                  {c} ({cnt})
                </button>
              )
            })}
          </div>
        )}
      </div>
      <div className="flex-1 bg-white rounded-xl border border-gray-200 overflow-y-auto">
        {!data && <div className="text-center text-gray-400 text-sm py-12">点击「扫描归档」对项目文件进行分类</div>}
        {data && files.length === 0 && <div className="text-center text-gray-400 text-sm py-8">该分类下暂无文件</div>}
        {files.map((f, i) => (
          <div key={i} className="flex items-center gap-3 px-4 py-2 border-b border-gray-50 hover:bg-gray-50 text-xs">
            <span className={`px-2 py-0.5 rounded-full text-xs ${CAT_COLOR[f.category] || 'bg-gray-100 text-gray-600'}`}>
              {f.category}
            </span>
            <span className="flex-1 text-gray-700 font-mono truncate">{f.file_path}</span>
            {f.phase_name && <span className="text-blue-500 flex-shrink-0">{f.phase_name}</span>}
            <span className="text-gray-400 flex-shrink-0">{(f.size_bytes / 1024).toFixed(1)}KB</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 面板：项目咨询（项目问答 / 功能变更讨论，多会话隔离）
// ══════════════════════════════════════════════════════════════════════════════
interface ConsultationMessage extends ChatMsg { speaker?: string; ts?: number }
interface ConsultationSession {
  id: string; mode: 'inquiry' | 'change' | 'rectification'; title: string
  messages: ConsultationMessage[]; updated_at: number
  source_issue_binding?: { defect_id?: string; observation_id?: string }
}

function ConsultationPanel({ projectId }: { projectId: string }) {
  const [sessions, setSessions] = useState<ConsultationSession[]>([])
  const [participants, setParticipants] = useState<string[]>([])
  const [activeId, setActiveId] = useState('')
  const [mode, setMode] = useState<'inquiry' | 'change'>('inquiry')
  const [newTitle, setNewTitle] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const active = sessions.find(s => s.id === activeId)
  const visibleSessions = sessions.filter(s => s.mode === mode)

  const loadSessions = async () => {
    const response = await apiFetch(`/engineer/${projectId}/consultations`)
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || '咨询记录加载失败')
    setSessions(result.sessions || [])
    setParticipants(result.participants || [])
    if (!activeId) {
      const first = (result.sessions || []).find((s: ConsultationSession) => s.mode === mode)
      if (first) setActiveId(first.id)
    }
  }
  useEffect(() => { void loadSessions() }, [projectId])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [active?.messages])

  const switchMode = (next: 'inquiry' | 'change') => {
    setMode(next)
    const first = sessions.find(s => s.mode === next)
    setActiveId(first?.id || '')
  }

  const createSession = async () => {
    const response = await apiFetch(`/engineer/${projectId}/consultations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, title: newTitle.trim() }),
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || '创建会话失败')
    setSessions(current => [result.session, ...current])
    setActiveId(result.session.id)
    setNewTitle('')
  }

  const renameSession = async (session: ConsultationSession) => {
    const title = window.prompt('输入新的对话名称', session.title)?.trim()
    if (!title || title === session.title) return
    const response = await apiFetch(`/engineer/${projectId}/consultations/${session.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || '重命名失败')
    setSessions(current => current.map(s => s.id === session.id ? result.session : s))
  }

  const sendMessage = async (text: string) => {
    if (!active) return
    const optimistic = { role: 'user' as const, speaker: '用户', content: text, ts: Date.now() / 1000 }
    setSessions(current => current.map(s => s.id === active.id ? { ...s, messages: [...(s.messages || []), optimistic] } : s))
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/consultations/${active.id}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '发送失败')
      setSessions(current => current.map(s => s.id === active.id ? result.session : s))
    } catch (e: any) {
      setSessions(current => current.map(s => s.id === active.id ? {
        ...s, messages: [...(s.messages || []), { role: 'assistant', speaker: '系统', content: `发送失败：${e.message}`, ts: Date.now() / 1000 }],
      } : s))
    } finally { setLoading(false) }
  }

  return (
    <div className="h-full flex gap-3">
      <aside className="w-72 bg-white rounded-xl border border-gray-200 p-3 flex flex-col">
        <div className="grid grid-cols-2 gap-1 mb-3">
          <button onClick={() => switchMode('inquiry')} className={`rounded px-2 py-2 text-xs ${mode === 'inquiry' ? 'bg-blue-500 text-white' : 'bg-gray-100 text-gray-600'}`}>项目问答</button>
          <button onClick={() => switchMode('change')} className={`rounded px-2 py-2 text-xs ${mode === 'change' ? 'bg-blue-500 text-white' : 'bg-gray-100 text-gray-600'}`}>功能变更讨论</button>
        </div>
        <input value={newTitle} onChange={e => setNewTitle(e.target.value)} placeholder="输入新对话名称" maxLength={80} className="mb-1 border rounded px-2 py-1.5 text-xs" />
        <button onClick={createSession} disabled={!newTitle.trim()} className="mb-2 rounded border border-blue-300 text-blue-600 py-1.5 text-xs hover:bg-blue-50 disabled:opacity-40">＋ 新建独立对话</button>
        <div className="flex-1 overflow-y-auto space-y-1">
          {visibleSessions.map(session => <div key={session.id} className={`flex items-center rounded ${activeId === session.id ? 'bg-blue-50 text-blue-700' : 'hover:bg-gray-50 text-gray-600'}`}><button onClick={() => setActiveId(session.id)} className="flex-1 min-w-0 text-left px-3 py-2 text-xs truncate">{session.title}</button><button onClick={() => void renameSession(session)} title="重命名" className="px-2 text-gray-400 hover:text-blue-600">✎</button></div>)}
          {!visibleSessions.length && <div className="text-xs text-gray-400 text-center py-8">暂无对话</div>}
        </div>
      </aside>
      <div className="flex-1 flex flex-col bg-white rounded-xl border border-gray-200 p-4 min-w-0">
        <div className="border-b pb-2 mb-3">
          <div className="font-semibold text-sm text-gray-700">{mode === 'inquiry' ? '项目问答' : '功能变更讨论'}</div>
          <div className="text-xs text-gray-400 mt-1">默认由 PM组长回答；输入 {participants.slice(0, 4).map(p => `@${p}`).join('、')} 可指定专家</div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {!active && <p className="text-xs text-gray-400 text-center py-8">新建或选择一个独立对话</p>}
          {active?.messages.map((m, i) => <div key={i}>
            {m.role === 'assistant' && <div className="text-[11px] text-gray-400 mb-1">{m.speaker || 'PM组长'}</div>}
            <ChatBubble msg={m} />
          </div>)}
          <div ref={bottomRef} />
        </div>
        {active && <ChatInput onSend={sendMessage} loading={loading} placeholder={mode === 'inquiry' ? '询问项目功能、实现或文件...' : '讨论新增、删除或修改功能...'} />}
      </div>
    </div>
  )
}

function RectificationPanel({
  projectId,
  preferredSessionId,
}: {
  projectId: string
  preferredSessionId?: string
}) {
  const [sessions, setSessions] = useState<ConsultationSession[]>([])
  const [activeId, setActiveId] = useState('')
  const [loading, setLoading] = useState(false)
  const [proposal, setProposal] = useState<any>(null)
  const active = sessions.find(s => s.id === activeId)

  const load = async () => {
    const response = await apiFetch(`/engineer/${projectId}/consultations`)
    const result = await response.json()
    const rectifications = (result.sessions || []).filter((s: ConsultationSession) => s.mode === 'rectification')
    setSessions(rectifications)
    const preferred = rectifications.find((session: ConsultationSession) => session.id === preferredSessionId)
    if (preferred) setActiveId(preferred.id)
    else if (!activeId && rectifications[0]) setActiveId(rectifications[0].id)
  }
  useEffect(() => { void load() }, [projectId, preferredSessionId])
  useEffect(() => { setProposal((sessions.find(s => s.id === activeId) as any)?.proposal || null) }, [activeId, sessions])

  const createSession = async () => {
    const response = await apiFetch(`/engineer/${projectId}/consultations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'rectification' }),
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || '创建整改会话失败')
    setSessions(current => [result.session, ...current]); setActiveId(result.session.id); setProposal(null)
  }
  const send = async (text: string) => {
    if (!active) return
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/consultations/${active.id}/messages`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '发送失败')
      setSessions(current => current.map(s => s.id === active.id ? result.session : s))
    } finally { setLoading(false) }
  }
  const prepare = async () => {
    if (!active) return
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/rectifications/prepare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: active.id }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '提案生成失败')
      setProposal(result.proposal)
    } catch (e: any) { alert(e.message) } finally { setLoading(false) }
  }
  const apply = async () => {
    if (!active || !proposal || !window.confirm(`确认修改 ${proposal.changes?.length || 0} 个文件？`)) return
    setLoading(true)
    try {
      const response = await apiFetch(`/engineer/${projectId}/rectifications/apply`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: active.id, proposal_id: proposal.id }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '整改执行失败')
      setProposal(result.proposal); await load()
    } catch (e: any) { alert(e.message) } finally { setLoading(false) }
  }

  return <div className="h-full flex gap-3">
    <aside className="w-72 bg-white rounded-xl border p-3 flex flex-col">
      <button onClick={createSession} className="rounded border border-blue-300 text-blue-600 py-1.5 text-xs mb-2">＋ 新建整改对话</button>
      <div className="flex-1 overflow-y-auto space-y-1">{sessions.map(s => <button key={s.id} onClick={() => setActiveId(s.id)} className={`w-full text-left rounded px-3 py-2 text-xs truncate ${activeId === s.id ? 'bg-blue-50 text-blue-700' : 'hover:bg-gray-50'}`}>{s.title}</button>)}</div>
    </aside>
    <section className="flex-1 bg-white rounded-xl border p-4 flex flex-col min-w-0">
      <div className="border-b pb-2 mb-2"><div className="font-semibold text-sm">项目整改</div><div className="text-xs text-gray-400">每项整改独立对话；确认提案后才写文件，并由全栈工程师自行检查。</div></div>
      <div className="flex-1 overflow-y-auto">{!active && <div className="text-xs text-gray-400 text-center py-8">新建或选择整改对话</div>}{active?.messages.map((m, i) => <div key={i}>{m.role === 'assistant' && <div className="text-[11px] text-gray-400">{m.speaker || '全栈工程师'}</div>}<ChatBubble msg={m}/></div>)}</div>
      {proposal && <div className="border rounded-lg p-3 mb-2 bg-gray-50 text-xs"><div className="font-semibold mb-1">{proposal.summary}</div>{proposal.changes?.map((c:any) => <div key={c.path} className="font-mono text-gray-600">{c.path} — {c.reason || '修改'}</div>)}<div className="mt-2 text-gray-500">状态：{proposal.status === 'applied' ? '已执行并通过自检' : '等待确认'}</div></div>}
      {active && <div className="space-y-2"><ChatInput onSend={send} loading={loading} placeholder="粘贴最终整改方案，或继续讨论实现细节..."/><div className="flex justify-end gap-2"><button onClick={prepare} disabled={loading} className="px-3 py-1.5 border rounded text-xs">生成文件变更提案</button><button onClick={apply} disabled={loading || !proposal || proposal.status !== 'pending_confirm'} className="px-3 py-1.5 bg-blue-500 text-white rounded text-xs disabled:opacity-40">确认执行并自检</button></div></div>}
    </section>
  </div>
}

// ══════════════════════════════════════════════════════════════════════════════
// 主页面
// ══════════════════════════════════════════════════════════════════════════════
export default function EngineerWorkspace() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const pid = projectId || ''
  // ?tab=repair 参数支持从阶段看板直接跳转到整改面板
  const [tab, setTab] = useState<'repair' | 'consultation' | 'adjustment' | 'archive'>(
    (searchParams.get('tab') as any) || 'repair'
  )
  const [status, setStatus] = useState<Record<string, any>>({})
  const [ctxLoading, setCtxLoading] = useState(false)
  const [rectificationSessionId, setRectificationSessionId] = useState('')

  useEffect(() => {
    if (!pid) return
    apiFetch(`/engineer/${pid}/status`).then(x => x.json()).then(setStatus).catch(() => {})
  }, [pid])

  const openProjectIssue = async (defect: Defect) => {
    setTab('adjustment')
    const storageKey = `engineer_project_issue_session_${pid}_${defect.id}`
    const existingSessionId = sessionStorage.getItem(storageKey)
    if (existingSessionId) {
      const sessionsResponse = await apiFetch(`/engineer/${pid}/consultations`).catch(() => null)
      const sessionsResult = sessionsResponse?.ok ? await sessionsResponse.json() : { sessions: [] }
      const existing = (sessionsResult.sessions || []).find(
        (session: ConsultationSession) => session.id === existingSessionId
      )
      if (existing?.source_issue_binding?.defect_id === defect.id) {
        setRectificationSessionId(existingSessionId)
        return
      }
      sessionStorage.removeItem(storageKey)
    }
    try {
      const createResponse = await apiFetch(`/engineer/${pid}/consultations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: 'rectification',
          title: `项目级整改：${defect.message.slice(0, 40)}`,
          source_issue_id: defect.id,
        }),
      })
      const created = await createResponse.json()
      if (!createResponse.ok) throw new Error(created.detail || '项目整改会话创建失败')
      const sessionId = String(created.session?.id || '')
      if (!sessionId) throw new Error('项目整改会话缺少 ID')
      const transferMessage = [
        '【系统自动转交的项目级问题】',
        `问题ID：${defect.id}`,
        `观察ID：${defect.observation_id || '无'}`,
        `严重级别：${defect.severity || 'unknown'}`,
        `来源阶段：${defect.detected_phase || defect.phase_id || defect.subproject_name || '全项目'}`,
        `问题描述：${defect.message}`,
        `待整改原因：${defect.needs_manual_reason || '未提供'}`,
        `修复建议：${defect.fix_hint || '未提供'}`,
        '要求：先定位根因和受影响文件，输出 affected_files、修改方案、验证命令及回滚边界；未经确认不要写入。',
      ].join('\n')
      const messageResponse = await apiFetch(`/engineer/${pid}/consultations/${sessionId}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: transferMessage }),
      })
      const messageResult = await messageResponse.json()
      if (!messageResponse.ok) throw new Error(messageResult.detail || '项目级问题转交失败')
      sessionStorage.setItem(storageKey, sessionId)
      setRectificationSessionId(sessionId)
    } catch (error: any) {
      alert(error.message || '项目级问题转交失败')
    }
  }

  const loadCtx = async () => {
    setCtxLoading(true)
    try {
      const r = await apiFetch(`/engineer/${pid}/load-context`, { method: 'POST' }).then(x => x.json())
      if (r.success) apiFetch(`/engineer/${pid}/status`).then(x => x.json()).then(setStatus)
    } finally { setCtxLoading(false) }
  }

  const TABS = [
    { k: 'repair', label: '🔧 代码整改' },
    { k: 'consultation', label: '💬 项目咨询' },
    { k: 'adjustment', label: '🛠️ 项目整改' },
    { k: 'archive', label: '🗂 文件盘点' },
  ] as const

  if (!pid) return <div className="p-8 text-gray-400">请先选择项目</div>

  return (
    <div className="h-screen flex flex-col bg-gray-50 metis-engineer-workspace">
      {/* 顶栏 */}
      <div className="flex items-center justify-between px-6 py-3 bg-white border-b border-gray-200 flex-shrink-0 metis-engineer-header">
        <div className="flex items-center gap-3">
          {/* 返回项目 */}
          <button
            onClick={() => navigate(`/projects/${pid}/pm-team`)}
            className="text-gray-400 hover:text-gray-700 text-lg leading-none metis-engineer-back"
            title="返回项目"
          >←</button>
          <span className="text-xl">🛠</span>
          <div>
            <div className="font-semibold text-gray-800">全栈工程师工作台</div>
            <div className="text-xs text-gray-400 flex items-center gap-2">
              <span>{status.has_project_background ? '✅ 已加载项目背景' : '⚠️ 未加载项目背景'}</span>
              {status.has_long_term_memory && <span title="个人长期记忆已激活">🧠 记忆</span>}
              {status.repair_defects > 0 && <span>{status.repair_defects} 个缺陷整改中</span>}
              {status.expert_pool_available === false && <span className="text-orange-400">⚠ 专家池不可用</span>}
            </div>
          </div>
        </div>
        {/* 仅在背景未加载时显示同步按钮，已加载时隐藏（规划确认时自动注入）*/}
        {!status.has_project_background && (
          <div className="flex items-center gap-2">
            <button onClick={loadCtx} disabled={ctxLoading}
              className="text-xs px-3 py-1 border border-orange-400 text-orange-500 rounded hover:bg-orange-50 disabled:opacity-40">
              {ctxLoading ? '加载中...' : '⚠️ 同步 PM 规划'}
            </button>
          </div>
        )}
      </div>

      {/* 标签栏 */}
      <div className="flex gap-1 px-6 pt-3 flex-shrink-0 metis-engineer-tabs">
        {TABS.map(t => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`px-4 py-2 rounded-t-lg text-sm font-medium transition
              ${tab === t.k ? 'bg-white border border-b-white border-gray-200 text-blue-600' : 'text-gray-500 hover:text-gray-700'}`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-hidden px-6 pb-4 pt-0 metis-engineer-content">
        <div className="h-full border-t-0 rounded-b-xl">
          {tab === 'repair'  && <RepairPanel projectId={pid} onOpenProjectIssue={openProjectIssue} />}
          {tab === 'consultation' && <ConsultationPanel projectId={pid} />}
          {tab === 'adjustment' && <RectificationPanel projectId={pid} preferredSessionId={rectificationSessionId} />}
          {tab === 'archive' && <ArchivePanel projectId={pid} />}
        </div>
      </div>
    </div>
  )
}
