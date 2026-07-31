/**
 * 文件管理 v2
 * - 区分执行记录（output/）与项目源文件（src/、docs/ 等）
 * - 支持将选中的 output/ 文件复制到项目根目录，但不将其视为质检证据
 * - 文件预览（代码高亮）
 * - 下载 / 删除
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  Card, Button, Tag, Space, Modal, message, Spin, Empty,
  Tooltip, Alert, List, Divider, Popconfirm, Row, Col, Statistic,
  Input, Typography,
} from 'antd';
import {
  FolderOutlined, FolderOpenOutlined, FileTextOutlined, DownloadOutlined,
  DeleteOutlined, EyeOutlined, ReloadOutlined, CheckCircleOutlined,
  InboxOutlined, WarningOutlined, FileOutlined,
  SaveOutlined, HistoryOutlined, RollbackOutlined, CloudUploadOutlined,
  MessageOutlined, ToolOutlined, SendOutlined,
} from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';
import {
  buildRepairApplyTarget,
  canRepairDefect,
  canonicalUiPath,
  countDefectFilesUnder,
  defectStatusPresentation,
} from './fileBrowserDefectState';
import { isFinalQARunning } from './finalQaState';

const { TextArea } = Input;
const { Text } = Typography;

// ── 与全栈工程师对话的类型 ────────────────────────────────────────────────────
interface ChatMsg { role: 'user' | 'assistant'; content: string }
interface DefectInfo {
  id: string; message: string; file_path: string; severity: string;
  status: string; fix_hint: string; subproject_name: string;
  observation_id: string;
  action_allowed?: boolean; blocked_reason?: string;
  authoritative_run_identity?: Record<string, string> | null;
  identity_confidence?: string; requires_identity_review?: boolean;
  needs_manual_reason?: string; fix_rounds?: number;
}

const API = API_BASE_URL;
axios.defaults.withCredentials = true;

// 目录语义标注
const DIR_META: Record<string, { label: string; color: string; desc: string; isOutput: boolean }> = {
  output:  { label: '📋 执行记录', color: 'default', desc: 'Agent 执行日志与中间记录，不代表质检通过', isOutput: true },
  src:     { label: '💻 源代码',   color: 'blue',    desc: 'Agent 生成的源代码（待质检）',   isOutput: false },
  docs:    { label: '📄 文档',     color: 'cyan',    desc: '需求文档、设计文档、规划书',      isOutput: false },
  tests:   { label: '🧪 测试',     color: 'orange',  desc: '测试用例和测试报告',              isOutput: false },
  uploads: { label: '📎 上传',     color: 'default', desc: '用户上传的参考文件',              isOutput: false },
};

const getFileIcon = (name: string) => {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  if (['ts', 'tsx', 'js', 'jsx', 'py', 'java', 'go', 'rs'].includes(ext)) return '📄';
  if (['md', 'txt', 'rst'].includes(ext)) return '📝';
  if (['json', 'yaml', 'yml', 'toml'].includes(ext)) return '⚙️';
  if (['png', 'jpg', 'gif', 'svg'].includes(ext)) return '🖼️';
  if (['zip', 'tar', 'gz'].includes(ext)) return '📦';
  return '📃';
};

const FileBrowser: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const [loading, setLoading] = useState(false);
  const [fileTree, setFileTree] = useState<any[]>([]);
  // needs_manual 缺陷文件集合
  const [defectFiles, setDefectFiles] = useState<Set<string>>(new Set());
  const [defectMap, setDefectMap] = useState<Record<string, any[]>>({});
  const [allDefects, setAllDefects] = useState<DefectInfo[]>([]);
  const [workspace, setWorkspace] = useState('');
  const [workspaceRel, setWorkspaceRel] = useState('');
  const [previewModal, setPreviewModal] = useState<{ open: boolean; path: string; content: string; name: string }>({
    open: false, path: '', content: '', name: '',
  });
  const [archiveModal, setArchiveModal] = useState(false);
  const [outputFiles, setOutputFiles] = useState<any[]>([]);
  const [archiving, setArchiving] = useState(false);
  const [selectedForArchive, setSelectedForArchive] = useState<Set<string>>(new Set());

  // ── 全栈工程师对话弹窗 ────────────────────────────────────────────────────
  // 问答对话（正常文件）
  const [qaModal, setQaModal] = useState<{
    open: boolean; filePath: string; fileName: string; fileContent: string;
  }>({ open: false, filePath: '', fileName: '', fileContent: '' });
  const [qaMsgs, setQaMsgs] = useState<ChatMsg[]>([]);
  const [qaInput, setQaInput] = useState('');
  const [qaLoading, setQaLoading] = useState(false);
  const qaMsgEndRef = useRef<HTMLDivElement>(null);

  // 整改对话（有缺陷文件）
  const [repairModal, setRepairModal] = useState<{
    open: boolean; filePath: string; fileName: string; defects: DefectInfo[];
    activeDef: DefectInfo | null;
  }>({ open: false, filePath: '', fileName: '', defects: [], activeDef: null });
  const [repairMsgs, setRepairMsgs] = useState<Record<string, ChatMsg[]>>({});
  const [repairInput, setRepairInput] = useState('');
  const [repairLoading, setRepairLoading] = useState(false);
  const [fixingId, setFixingId] = useState<string | null>(null); // 一键修复中的 defect id
  const [confirmedPlanVersions, setConfirmedPlanVersions] = useState<Record<string, string>>({});
  const [fixProposals, setFixProposals] = useState<Record<string, {
    digest: string; confirmable: boolean; error?: string; wholeFile: boolean;
  }>>({});
  const repairMsgEndRef = useRef<HTMLDivElement>(null);

  // 修改正常文件需要二次确认弹窗
  const [editConfirmModal, setEditConfirmModal] = useState<{
    open: boolean; filePath: string; content: string;
  }>({ open: false, filePath: '', content: '' });

  // 滚动到底部
  const scrollQA = () => setTimeout(() => qaMsgEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 80);
  const scrollRepair = () => setTimeout(() => repairMsgEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 80);

  // ── 打开文件对话窗口 ──────────────────────────────────────────────────────
  const openFileChat = async (filePath: string, fileName: string) => {
    const normalizedPath = canonicalUiPath(filePath);
    const hasDefect = defectFiles.has(normalizedPath);
    if (hasDefect) {
      // 有缺陷：打开整改对话
      const defs = (defectMap[normalizedPath] || []) as DefectInfo[];
      const firstDef = defs.find(canRepairDefect) || defs[0];
      setRepairModal({ open: true, filePath: normalizedPath, fileName, defects: defs, activeDef: firstDef || null });
      // 初始化该文件第一个缺陷的历史（如果没有则自动发一条首轮消息）
      if (canRepairDefect(firstDef) && !(repairMsgs[firstDef.id]?.length)) {
        await sendRepairMsg('请分析此缺陷，给出整改方案', firstDef, true);
      }
      scrollRepair();
    } else {
      // 无缺陷：打开问答对话，预加载文件内容
      let fileContent = '';
      try {
        const res = await axios.get(`${API}/projects/${projectId}/files/read?path=${encodeURIComponent(filePath)}`);
        fileContent = res.data.content || '';
      } catch { /* 静默 */ }
      setQaModal({ open: true, filePath, fileName, fileContent });
      setQaMsgs([]);
      setQaInput('');
      scrollQA();
    }
  };

  // ── 问答对话发送（正常文件） ──────────────────────────────────────────────
  const sendQAMsg = async (text: string) => {
    if (!text.trim() || qaLoading) return;
    const userMsg: ChatMsg = { role: 'user', content: text };
    setQaMsgs(prev => [...prev, userMsg]);
    setQaInput('');
    setQaLoading(true);
    scrollQA();
    try {
      // 首条消息注入文件内容
      const contextNote = qaModal.fileContent
        ? `\n\n【文件内容参考】\n文件路径：${qaModal.filePath}\n\`\`\`\n${qaModal.fileContent.slice(0, 3000)}\n\`\`\``
        : '';
      const msgToSend = qaMsgs.length === 0 ? text + contextNote : text;
      const res = await axios.post(`${API}/engineer/${projectId}/chat/qa`, { message: msgToSend });
      setQaMsgs(prev => [...prev, { role: 'assistant', content: res.data.reply || '（无回复）' }]);
    } catch (e: any) {
      setQaMsgs(prev => [...prev, { role: 'assistant', content: `请求失败：${e.message}` }]);
    } finally {
      setQaLoading(false);
      scrollQA();
    }
  };

  // ── 整改对话发送 ──────────────────────────────────────────────────────────
  const sendRepairMsg = async (text: string, defect?: DefectInfo | null, isAuto = false) => {
    const def = defect ?? repairModal.activeDef;
    if (!def || !text.trim()) return;
    if (!canRepairDefect(def)) {
      message.warning(
        def.requires_identity_review || def.identity_confidence !== 'high'
          ? '该缺陷需要先完成身份复核'
          : '该缺陷正在处理或等待权威复检，不能重复提交整改',
      );
      return;
    }
    if (!isAuto) setRepairInput('');
    setRepairLoading(true);

    // isAuto 时也显示触发消息（让用户看到分析请求）
    const prev = repairMsgs[def.id] || [];
    const withUser: ChatMsg[] = [...prev, { role: 'user', content: text }];
    setRepairMsgs(r => ({ ...r, [def.id]: withUser }));
    scrollRepair();

    // 文件字节与真实 defect window 由服务端从 canonical ledger 注入；
    // 客户端不再发送截断的源码副本。
    const isFirst = prev.length === 0;
    let fullMessage = text;
    if (isFirst) {
      fullMessage =
        `缺陷信息：\n` +
        `- 描述：${def.message}\n` +
        `- 文件：${def.file_path}\n` +
        `- 严重程度：${def.severity}\n` +
        `- 修复建议：${def.fix_hint || '无'}\n` +
        `- 原因：${def.needs_manual_reason || '未知'}\n` +
        '\n' +
        `请基于以上信息分析根因，给出整改方案。`;
    }

    try {
      const res = await axios.post(`${API}/engineer/${projectId}/chat/repair`, {
        defect_id: def.id,
        message: fullMessage,
        defect_info: isFirst ? def : undefined,
        all_defects: allDefects,
      });
      if (res.data.proposal_digest) {
        setFixProposals(current => ({
          ...current,
          [def.id]: {
            digest: res.data.proposal_digest,
            confirmable: !!res.data.proposal_confirmable,
            error: res.data.proposal_error,
            wholeFile: !!res.data.requires_whole_file_authorization,
          },
        }));
      }
      const reply: ChatMsg = { role: 'assistant', content: res.data.reply || '' };
      setRepairMsgs(r => ({ ...r, [def.id]: [...withUser, reply] }));
    } catch (e: any) {
      setRepairMsgs(r => ({ ...r, [def.id]: [...withUser, { role: 'assistant', content: `请求失败：${e.message}` }] }));
    } finally {
      setRepairLoading(false);
      scrollRepair();
    }
  };

  const handleConfirmFixProposal = async (def: DefectInfo) => {
    if (!canRepairDefect(def)) {
      message.error('当前缺陷状态或身份置信度不允许确认整改方案');
      return;
    }
    const proposal = fixProposals[def.id];
    if (!proposal?.confirmable) {
      message.error(proposal?.error || '当前 proposal 缺少可验证的变更范围');
      return;
    }
    const allowWholeFile = proposal.wholeFile
      ? window.confirm('这是 whole-file 高风险整改授权。确认允许替换整个文件吗？')
      : false;
    if (proposal.wholeFile && !allowWholeFile) return;
    setRepairLoading(true);
    try {
      const res = await axios.post(`${API}/engineer/${projectId}/confirm-fix-plan`, {
        defect_id: def.id,
        proposal_digest: proposal.digest,
        observation_id: def.observation_id,
        file_path: def.file_path,
        allow_whole_file: allowWholeFile,
      });
      setConfirmedPlanVersions(current => ({
        ...current,
        [def.id]: res.data.confirmed_plan_version,
      }));
      message.success('整改方案已独立确认并绑定当前文件 baseline');
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message || '方案确认失败');
    } finally {
      setRepairLoading(false);
    }
  };

  // ── 一键修改文件错误 ──────────────────────────────────────────────────────
  const handleOneClickFix = async (def: DefectInfo) => {
    if (!canRepairDefect(def)) {
      message.error('当前缺陷状态或身份置信度不允许整改');
      return;
    }
    setFixingId(def.id);
    try {
      const res = await axios.post(`${API}/engineer/${projectId}/apply-fix`, {
        ...buildRepairApplyTarget(def, confirmedPlanVersions),
        new_content: '',       // 让 Agent 自动生成修复内容
        run_qa: true,
        defect_info: def,
      });
      if (res.data.success) {
        message.success(`整改已写入（第${res.data.edit_count}次），正在等待 authoritative 复检`);
        await fetchDefects();
        await fetchFiles();
        // 将结果追加到对话历史
        const resultMsg: ChatMsg = {
          role: 'assistant',
          content: `✅ 整改已执行（第${res.data.edit_count}次）：${res.data.message}\n静态预检不代表最终 QA 通过；缺陷会保留到 authoritative 复检更新账本。`,
        };
        setRepairMsgs(r => ({ ...r, [def.id]: [...(r[def.id] || []), resultMsg] }));
      } else {
        message.error(res.data.message || res.data.error || '修复失败');
        const failMsg: ChatMsg = { role: 'assistant', content: `❌ 写入被拒绝：${res.data.message || res.data.error}` };
        setRepairMsgs(r => ({ ...r, [def.id]: [...(r[def.id] || []), failMsg] }));
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message || '修复失败');
    } finally {
      setFixingId(null);
    }
  };

  // ── 最终整体质检 ───────────────────────────────────────────────────────────
  const [finalQA, setFinalQA] = useState<{
    open: boolean;
    status: string;
    round: number;
    totalRounds: number;
    logs: string[];
    allPassed: boolean;
    needsManual: any[];
    qcSummary: Record<string, any>;
  }>({
    open: false, status: 'not_started', round: 0, totalRounds: 3,
    logs: [], allPassed: false, needsManual: [], qcSummary: {},
  });
  const [finalQARunning, setFinalQARunning] = useState(false);
  const finalQAPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [signoff, setSignoff] = useState<{
    status: 'unknown' | 'ready' | 'blocked' | 'completed';
    blockers: Array<{ code?: string; message: string; action?: string }>;
  }>({ status: 'unknown', blockers: [] });
  const [signingOff, setSigningOff] = useState(false);

  const applyFinalQAStatus = (d: any) => {
    const status = d.status || 'not_started';
    setFinalQA(prev => ({
      ...prev,
      status,
      round: d.round || 0,
      totalRounds: d.total_rounds || 5,
      logs: d.logs || [],
      allPassed: d.all_passed || false,
      needsManual: d.needs_manual || [],
      qcSummary: d.qc_summary || {},
    }));
    const running = isFinalQARunning(status);
    setFinalQARunning(running);
    return running;
  };

  const loadSignoffStatus = async () => {
    if (!projectId) return;
    try {
      const { data } = await axios.get(`${API}/projects/${projectId}/signoff/status`);
      setSignoff({ status: data.status || 'blocked', blockers: data.blockers || [] });
    } catch {
      setSignoff({ status: 'unknown', blockers: [] });
    }
  };

  const pollFinalQAStatus = async () => {
    if (!projectId) return false;
    const { data } = await axios.get(`${API}/projects/${projectId}/final-qa/status`);
    const running = applyFinalQAStatus(data);
    if (!running) await loadSignoffStatus();
    return running;
  };

  const startFinalQAPolling = () => {
    if (finalQAPollRef.current) clearInterval(finalQAPollRef.current);
    finalQAPollRef.current = setInterval(async () => {
      try {
        if (!await pollFinalQAStatus()) {
          clearInterval(finalQAPollRef.current!);
          finalQAPollRef.current = null;
        }
      } catch { /* 保留上次服务端状态，下轮继续 */ }
    }, 3000);
  };

  const triggerFinalQA = async () => {
    setFinalQARunning(true);
    setFinalQA(prev => ({ ...prev, open: true, status: 'running', round: 0, logs: [], allPassed: false, needsManual: [], qcSummary: {} }));
    try {
      await axios.post(`${API}/projects/${projectId}/final-qa`);
      startFinalQAPolling();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '触发失败');
      setFinalQARunning(false);
    }
  };

  // 组件卸载时清除轮询
  useEffect(() => {
    if (!projectId) return;
    void (async () => {
      try {
        if (await pollFinalQAStatus()) startFinalQAPolling();
      } catch { /* 项目可能尚未创建 Final QA 记录 */ }
    })();
    return () => { if (finalQAPollRef.current) clearInterval(finalQAPollRef.current); };
  }, [projectId]);

  const submitSignoff = async () => {
    if (!projectId || signingOff) return;
    setSigningOff(true);
    try {
      const { data } = await axios.post(`${API}/projects/${projectId}/signoff`);
      setSignoff({ status: data.status || 'completed', blockers: data.blockers || [] });
      message.success('项目签核完成');
    } catch (e: any) {
      const data = e.response?.data;
      setSignoff({ status: data?.status || 'blocked', blockers: data?.blockers || [] });
      message.error(data?.blockers?.[0]?.message || e.response?.data?.detail || '项目签核被阻断');
    } finally {
      setSigningOff(false);
    }
  };

  // ── 暂存区 / 提交 / 版本历史 ──────────────────────────────────────────────
  const [stagingIndex, setStagingIndex] = useState<Record<string, any>>({});
  const [commitMsg, setCommitMsg] = useState('');
  const [committing, setCommitting] = useState(false);
  const [versions, setVersions] = useState<any[]>([]);
  const [versionModal, setVersionModal] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  const fetchStagingIndex = async () => {
    if (!projectId) return;
    try {
      const res = await axios.get(`${API}/projects/${projectId}/files/staging`);
      setStagingIndex(res.data.staged_files || {});
    } catch { /* 静默 */ }
  };

  const fetchVersions = async () => {
    if (!projectId) return;
    try {
      const res = await axios.get(`${API}/projects/${projectId}/files/versions`);
      setVersions(res.data.versions || []);
    } catch { /* 静默 */ }
  };

  const handleCommit = async () => {
    if (Object.keys(stagingIndex).length === 0) {
      message.warning('暂存区为空，没有可提交的内容');
      return;
    }
    setCommitting(true);
    try {
      const res = await axios.post(`${API}/projects/${projectId}/files/commit`, { message: commitMsg });
      if (res.data.success) {
        message.success(`提交成功！版本 v${res.data.version}，共 ${res.data.committed_files?.length || 0} 个文件`);
        setCommitMsg('');
        setStagingIndex({});
        fetchFiles();
        fetchVersions();
      } else {
        message.error(res.data.error || '提交失败');
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '提交失败');
    } finally {
      setCommitting(false);
    }
  };

  const handleRollback = async (version: number) => {
    setRollingBack(true);
    try {
      const res = await axios.post(`${API}/projects/${projectId}/files/rollback`, { version });
      if (res.data.success) {
        message.success(res.data.message);
        setVersionModal(false);
        fetchFiles();
      } else {
        message.error(res.data.error || '回滚失败');
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '回滚失败');
    } finally {
      setRollingBack(false);
    }
  };

  const fetchFiles = async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const res = await axios.get(`${API}/projects/${projectId}/files`);
      setFileTree(res.data.tree || []);
      setWorkspace(res.data.workspace || '');
      setWorkspaceRel(res.data.workspace_rel || '');
      // 提取 output/ 目录下的所有文件
      const outputDir = (res.data.tree || []).find((n: any) => n.title === 'output');
      if (outputDir) {
        const flatten = (nodes: any[]): any[] =>
          nodes.flatMap(n => n.type === 'file' ? [n] : flatten(n.children || []));
        setOutputFiles(flatten(outputDir.children || []));
      } else {
        setOutputFiles([]);
      }
    } catch { /* 静默 */ }
    finally { setLoading(false); }
  };

  // 加载 needs_manual 缺陷文件列表
  const fetchDefects = async () => {
    if (!projectId) return;
    try {
      const res = await axios.get(`${API}/engineer/${projectId}/all-defects`);
      const defects: any[] = res.data.defects || [];
      setAllDefects(defects);
      const activeDefects = defects.filter(d =>
        ['needs_manual', 'open', 'fixing', 'pending_verification'].includes(d.status)
      );
      const map: Record<string, any[]> = {};
      activeDefects.forEach(d => {
        const path = canonicalUiPath(d.file_path);
        if (!path) return;
        if (!map[path]) map[path] = [];
        map[path].push({ ...d, file_path: path });
      });
      setDefectMap(map);
      setDefectFiles(new Set(Object.keys(map)));
    } catch { /* 静默，工程师未初始化时正常 */ }
  };

  useEffect(() => {
    fetchFiles();
    fetchStagingIndex();
    fetchVersions();
    fetchDefects();
  }, [projectId]);

  // 预览文件
  const previewFile = async (path: string, name: string) => {
    try {
      const res = await axios.get(`${API}/projects/${projectId}/files/read?path=${encodeURIComponent(path)}`);
      setPreviewModal({ open: true, path, content: res.data.content || '', name });
    } catch (e: any) {
      message.error(e.response?.data?.detail || '读取失败');
    }
  };

  // 删除文件
  const deleteFile = async (path: string) => {
    try {
      await axios.delete(`${API}/projects/${projectId}/files/delete?path=${encodeURIComponent(path)}`);
      message.success('已删除');
      fetchFiles();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败');
    }
  };

  // 归档：将选中的 output/ 文件复制到实际项目根目录（workspace 根）
  const archiveFiles = async () => {
    if (selectedForArchive.size === 0) {
      message.warning('请先选择要归档的文件');
      return;
    }
    setArchiving(true);
    let successCount = 0;
    const failedFiles: string[] = [];
    for (const filePath of selectedForArchive) {
      try {
        // 读取文件内容
        const readRes = await axios.get(`${API}/projects/${projectId}/files/read?path=${encodeURIComponent(filePath)}`);
        // 写入到 workspace 根目录（去掉 output/ 前缀）
        const targetPath = filePath.replace(/^output\//, '');
        await axios.post(`${API}/projects/${projectId}/files/write`, {
          path: targetPath,
          content: readRes.data.content,
        });
        successCount++;
      } catch {
        failedFiles.push(filePath);
      }
    }
    setArchiving(false);
    if (successCount === 0) {
      message.error(`复制失败：${failedFiles.length} 个文件均未写入项目根目录`);
      return;
    }
    if (failedFiles.length > 0) {
      message.warning(`已复制 ${successCount} 个文件，${failedFiles.length} 个失败；失败项仍保持选中`);
      setSelectedForArchive(new Set(failedFiles));
      await fetchFiles();
      return;
    }
    message.success(`已复制 ${successCount} 个文件到项目根目录`);
    setArchiveModal(false);
    setSelectedForArchive(new Set());
    await fetchFiles();
  };

  // 统计
  const totalFiles = fileTree.reduce((acc, n) => {
    const count = (nodes: any[]): number => nodes.reduce((a, node) => a + (node.type === 'file' ? 1 : count(node.children || [])), 0);
    return acc + count([n]);
  }, 0);
  const rootFiles = fileTree.filter(n => n.type === 'file');
  const rootDefectCount = new Set(
    rootFiles
      .map(file => canonicalUiPath(file.key))
      .filter(path => defectFiles.has(path)),
  ).size;

  // 渲染目录节点
  const renderDirSection = (dirName: string) => {
    const meta = DIR_META[dirName] || { label: dirName, color: 'default', desc: '', isOutput: false };
    const node = fileTree.find(n => n.title === dirName);
    const flatten = (nodes: any[]): any[] =>
      nodes.flatMap(n => n.type === 'file' ? [n] : flatten(n.children || []));
    const files = node ? flatten(node.children || []) : [];
    const directoryDefectCount = countDefectFilesUnder(defectFiles, dirName);

    return (
      <Card
        key={dirName}
        size="small"
        className="mb-3"
        title={
          <div className="flex items-center gap-2">
            <FolderOpenOutlined className={meta.isOutput ? 'text-gray-500' : 'text-yellow-500'} />
            <span className="font-medium">{dirName}/</span>
            <Tag color={meta.color} className="text-xs">{meta.label}</Tag>
            <span className="text-xs text-gray-400">{meta.desc}</span>
          </div>
        }
        extra={
          <Space>
            <span className="text-xs text-gray-400">{files.length} 个文件</span>
            {directoryDefectCount > 0 && (
              <Tooltip title="该目录下仍有整改中、待复检或需人工处理的缺陷文件">
                <Tag color="orange" icon={<WarningOutlined />} className="text-xs">
                  {directoryDefectCount} 个缺陷文件
                </Tag>
              </Tooltip>
            )}
            {meta.isOutput && files.length > 0 && (
              <Button
                size="small"
                type="primary"
                icon={<InboxOutlined />}
                onClick={() => {
                  setSelectedForArchive(new Set(files.map(f => f.key)));
                  setArchiveModal(true);
                }}
              >
                复制到项目根目录
              </Button>
            )}
          </Space>
        }
      >
        {files.length === 0 ? (
          <div className="text-xs text-gray-400 py-2">
            {meta.isOutput
              ? '暂无执行记录。此目录不作为质检通过或项目可运行的证明。'
              : '暂无文件'}
          </div>
        ) : (
          <List
            size="small"
            dataSource={files}
            renderItem={(file: any) => {
              const normalizedFile = canonicalUiPath(file.key);
              const fileDefects = (defectMap[normalizedFile] || []) as DefectInfo[];
              const hasDefect = fileDefects.length > 0;
              const statusPresentation = defectStatusPresentation(fileDefects);
              return (
                <List.Item
                  className="py-1"
                  actions={[
                    /* 对话按钮：有缺陷用橙色整改图标，无缺陷用蓝色对话图标 */
                    <Tooltip key="chat" title={hasDefect ? statusPresentation.tooltip : '与全栈工程师对话'}>
                      <Button
                        size="small"
                        type="text"
                        icon={hasDefect ? <ToolOutlined style={{ color: '#f97316' }} /> : <MessageOutlined style={{ color: '#3b82f6' }} />}
                        onClick={() => openFileChat(file.key, file.title)}
                      />
                    </Tooltip>,
                    <Tooltip key="preview" title="预览">
                      <Button size="small" type="text" icon={<EyeOutlined />}
                        onClick={() => previewFile(file.key, file.title)} />
                    </Tooltip>,
                    <Tooltip key="download" title="下载">
                      <a href={`${API}/projects/${projectId}/files/download?path=${encodeURIComponent(file.key)}`}
                        target="_blank" rel="noreferrer">
                        <Button size="small" type="text" icon={<DownloadOutlined />} />
                      </a>
                    </Tooltip>,
                    <Popconfirm key="delete" title="确认删除？" onConfirm={() => deleteFile(file.key)}>
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                    </Popconfirm>,
                  ]}
                >
                  <div
                    className="flex items-center gap-2 cursor-pointer hover:opacity-80"
                    onClick={() => openFileChat(file.key, file.title)}
                  >
                    <span>{getFileIcon(file.title)}</span>
                    <span className={`text-xs font-mono ${hasDefect ? 'text-orange-700' : 'text-gray-700'}`}>{file.key}</span>
                    {file.size && (
                      <span className="text-xs text-gray-300">{(file.size / 1024).toFixed(1)}KB</span>
                    )}
                    {meta.isOutput && (
                      <Tag className="text-xs">执行记录</Tag>
                    )}
                    {hasDefect && (
                      <Tooltip title={
                        fileDefects.map((d: any) =>
                          d.needs_manual_reason || d.message
                        ).join('\n') || statusPresentation.tooltip
                      }>
                        <Tag color={statusPresentation.color} icon={<WarningOutlined />} className="text-xs cursor-help">
                          {statusPresentation.label}
                        </Tag>
                      </Tooltip>
                    )}
                  </div>
                </List.Item>
              );
            }}
          />
        )}
      </Card>
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">📁 文件管理</h2>
        <div className="flex items-center gap-2">
          {workspaceRel && (
            <Tooltip title={`本地路径：${workspace}`}>
              <Tag icon={<FolderOutlined />} color="blue" className="text-xs cursor-default">
                {workspaceRel}
              </Tag>
            </Tooltip>
          )}
          <Button icon={<ReloadOutlined />} onClick={fetchFiles} loading={loading} size="small">刷新</Button>
        </div>
      </div>

      {/* ── 最终整体质检进度弹窗 ────────────────────────────────────────────── */}
      <Modal
        title={
          <Space>
            <CheckCircleOutlined style={{ color: '#7c3aed' }} />
            <span>最终整体质检</span>
            {finalQA.status === 'passed' && <Tag color="success">✅ 全部通过</Tag>}
            {finalQA.status === 'needs_manual' && <Tag color="orange">⚠ 需人工整改</Tag>}
            {finalQARunning && <Tag color="processing">第 {finalQA.round}/{finalQA.totalRounds} 轮...</Tag>}
          </Space>
        }
        open={finalQA.open}
        onCancel={() => setFinalQA(m => ({ ...m, open: false }))}
        footer={
          <Space>
            {!finalQARunning && finalQA.status === 'not_started' && (
              <Button type="primary" icon={<CheckCircleOutlined />} onClick={triggerFinalQA}
                style={{ backgroundColor: '#7c3aed', borderColor: '#7c3aed' }}>
                开始最终质检
              </Button>
            )}
            {!finalQARunning && finalQA.status !== 'not_started' && (
              <Button icon={<ReloadOutlined />} onClick={triggerFinalQA}>重新质检</Button>
            )}
            {finalQA.status === 'passed' && signoff.status === 'ready' && (
              <Button type="primary" loading={signingOff} onClick={submitSignoff}>
                签核项目
              </Button>
            )}
            <Button onClick={() => setFinalQA(m => ({ ...m, open: false }))}>关闭</Button>
          </Space>
        }
        width={640}
        styles={{ body: { padding: '12px 16px' } }}
      >
        {/* 说明 */}
        <Alert
          type="info"
          showIcon
          className="mb-3"
          message={
            <span className="text-xs">
              最终整体质检会扫描所有文件，检测：跨文件接口冲突、模块整合问题、整体代码落地缺陷。
              发现问题后自动返工（最多 {finalQA.totalRounds} 轮），{finalQA.totalRounds} 轮未解决则标记为需人工整改。
            </span>
          }
        />
        {signoff.status === 'completed' && (
          <Alert className="mb-3" type="success" showIcon message="项目已完成签核" />
        )}
        {finalQA.status === 'passed' && signoff.status === 'blocked' && (
          <Alert
            className="mb-3"
            type="warning"
            showIcon
            message="最终质检已通过，但项目尚不能签核"
            description={
              <div>
                {signoff.blockers.map((blocker, index) => (
                  <div key={`${blocker.code || 'blocker'}-${index}`}>
                    {blocker.message}{blocker.action ? `；${blocker.action}` : ''}
                  </div>
                ))}
              </div>
            }
          />
        )}
        {finalQA.status === 'recovery_required' && (
          <Alert
            className="mb-3"
            type="warning"
            showIcon
            message="最终质检中断，服务端正在恢复"
            description="页面会持续读取恢复状态，请勿重复启动质检。"
          />
        )}
        {finalQA.status === 'recovery_blocked' && (
          <Alert
            className="mb-3"
            type="error"
            showIcon
            message="最终质检恢复失败"
            description="该状态已终止自动轮询，请处理服务端阻断原因后重新质检。"
          />
        )}
        {/* 子项目质检摘要 */}
        {Object.keys(finalQA.qcSummary).length > 0 && (
          <div className="mb-3 space-y-1">
            <div className="text-xs font-semibold text-gray-600 mb-1">子项目质检结果：</div>
            {Object.entries(finalQA.qcSummary).map(([spId, info]: [string, any]) => (
              <div key={spId} className="flex items-center gap-2 text-xs">
                <span>{info.passed ? '✅' : '❌'}</span>
                <span className="font-medium text-gray-700">{info.name || spId}</span>
                {info.open_count > 0 && <Tag color="orange" className="text-xs">{info.open_count} 个问题</Tag>}
                {info.score > 0 && <span className="text-gray-400">得分：{info.score}</span>}
              </div>
            ))}
          </div>
        )}
        {/* 执行日志 */}
        <div
          className="bg-gray-900 rounded-lg p-3 overflow-y-auto font-mono"
          style={{ minHeight: 120, maxHeight: '35vh' }}
        >
          {finalQA.logs.length === 0 ? (
            <div className="text-gray-500 text-xs text-center py-4">
              {finalQA.status === 'not_started' ? '点击「开始最终质检」启动' : '等待质检启动...'}
            </div>
          ) : (
            finalQA.logs.map((log, i) => (
              <div key={i} className={`text-xs leading-relaxed ${
                log.includes('✅') ? 'text-green-400' :
                log.includes('❌') ? 'text-red-400' :
                log.includes('⚠️') ? 'text-yellow-400' :
                log.includes('🔄') ? 'text-blue-400' :
                'text-gray-300'
              }`}>
                {log}
              </div>
            ))
          )}
        </div>
        {/* needs_manual 问题列表 */}
        {finalQA.needsManual.length > 0 && (
          <div className="mt-3">
            <div className="text-xs font-semibold text-orange-600 mb-1">
              ⚠ {finalQA.needsManual.length} 个问题需人工整改（已标记到文件列表）：
            </div>
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {finalQA.needsManual.map((iss: any, i: number) => (
                <div key={i} className="text-xs text-gray-600 flex items-start gap-1">
                  <span className="text-orange-400 flex-shrink-0">•</span>
                  <span className="font-mono text-gray-500 flex-shrink-0">{iss.file_path}</span>
                  <span>{iss.message}</span>
                </div>
              ))}
            </div>
            <div className="text-xs text-gray-400 mt-1">
              可在下方文件列表中点击橙色标记的文件，打开整改对话窗口进行修复。
            </div>
          </div>
        )}
      </Modal>

      {/* 说明 */}
      <Alert
        type="info"
        showIcon
        message={
          <span className="text-sm">
            <strong>output/</strong> 目录存放执行日志和中间记录，不代表 Agent 成功、质检通过或项目可运行。
            复制到项目根目录只是文件操作，不能替代最终整体质检。
          </span>
        }
      />

      {/* 统计 */}
      <Row gutter={12}>
        <Col span={6}>
          <Card size="small">
            <Statistic title="文件总数" value={totalFiles} prefix={<FileOutlined />} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="执行记录"
              value={outputFiles.length}
              prefix={<FileTextOutlined />}
            />
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small">
            <div className="text-xs text-gray-500">
              <strong>目录说明：</strong>
              output/ = 执行记录（非验收结果） &nbsp;|&nbsp;
              src/ = 源代码 &nbsp;|&nbsp;
              docs/ = 文档 &nbsp;|&nbsp;
              tests/ = 测试 &nbsp;|&nbsp;
              uploads/ = 上传文件
            </div>
          </Card>
        </Col>
      </Row>

      {/* ── 暂存区 / 提交 / 版本历史工具栏 ── */}
      <Card
        size="small"
        title={
          <Space>
            <SaveOutlined />
            <span className="font-medium">版本控制</span>
            {Object.keys(stagingIndex).length > 0 && (
              <Tag color="orange">{Object.keys(stagingIndex).length} 个文件待提交</Tag>
            )}
          </Space>
        }
        extra={
          <Space>
            <Button
              size="small"
              icon={<HistoryOutlined />}
              onClick={() => { fetchVersions(); setVersionModal(true); }}
            >
              版本历史 ({versions.length})
            </Button>
          </Space>
        }
      >
        {Object.keys(stagingIndex).length === 0 ? (
          <div className="text-xs text-gray-400">
            暂存区为空。在文件预览中编辑并保存后，文件会进入暂存区，提交后生成版本快照。
          </div>
        ) : (
          <div className="space-y-2">
            <div className="text-xs text-gray-500 mb-1">暂存区文件：</div>
            {Object.entries(stagingIndex).map(([path, info]: [string, any]) => (
              <div key={path} className="flex items-center gap-2 text-xs">
                <Tag color="orange" className="font-mono">{path}</Tag>
                <Text type="secondary">{info.size ? `${(info.size / 1024).toFixed(1)}KB` : ''}</Text>
              </div>
            ))}
            <Divider className="my-2" />
            <div className="flex gap-2 items-start">
              <TextArea
                placeholder="提交说明（可选）"
                value={commitMsg}
                onChange={e => setCommitMsg(e.target.value)}
                rows={2}
                className="flex-1 text-xs"
              />
              <Button
                type="primary"
                icon={<CloudUploadOutlined />}
                loading={committing}
                onClick={handleCommit}
              >
                提交
              </Button>
            </div>
          </div>
        )}
      </Card>

      <Spin spinning={loading}>
        {fileTree.length === 0 ? (
          <Empty description="项目工作区暂无文件，请先启动项目并执行 Agent" />
        ) : (
          <div>
            {/* 优先展示 output（执行记录，不作为交付验收依据） */}
            {renderDirSection('output')}
            {renderDirSection('src')}
            {renderDirSection('docs')}
            {renderDirSection('tests')}
            {renderDirSection('uploads')}
            {rootFiles.length > 0 && (
              <Card
                size="small"
                className="mb-3"
                title={
                  <div className="flex items-center gap-2">
                    <FolderOpenOutlined className="text-purple-500" />
                    <span className="font-medium">项目根目录</span>
                    <Tag color="purple" className="text-xs">项目根文件</Tag>
                    <span className="text-xs text-gray-400">是否可交付须以最终整体质检与运行验收为准</span>
                  </div>
                }
                extra={
                  <Space>
                    <span className="text-xs text-gray-400">{rootFiles.length} 个文件</span>
                    {rootDefectCount > 0 && (
                      <Tag color="orange">{rootDefectCount} 个缺陷文件</Tag>
                    )}
                  </Space>
                }
              >
                <List
                  size="small"
                  dataSource={rootFiles}
                  renderItem={(file: any) => {
                    const normalizedFile = canonicalUiPath(file.key);
                    const fileDefects = (defectMap[normalizedFile] || []) as DefectInfo[];
                    const presentation = defectStatusPresentation(fileDefects);
                    return (
                      <List.Item
                        className="py-1"
                        actions={[
                          fileDefects.length > 0
                            ? <Tooltip key="defect" title={presentation.tooltip}>
                                <Button size="small" type="text" icon={<ToolOutlined />}
                                  onClick={() => openFileChat(file.key, file.title)} />
                              </Tooltip>
                            : <Tooltip key="preview" title="预览">
                                <Button size="small" type="text" icon={<EyeOutlined />}
                                  onClick={() => previewFile(file.key, file.title)} />
                              </Tooltip>,
                          <Tooltip key="download" title="下载">
                            <a href={`${API}/projects/${projectId}/files/download?path=${encodeURIComponent(file.key)}`}
                              target="_blank" rel="noreferrer">
                              <Button size="small" type="text" icon={<DownloadOutlined />} />
                            </a>
                          </Tooltip>,
                        ]}
                      >
                        <div className="flex items-center gap-2 cursor-pointer hover:opacity-80"
                          onClick={() => fileDefects.length > 0
                            ? openFileChat(file.key, file.title)
                            : previewFile(file.key, file.title)}>
                          <span>{getFileIcon(file.title)}</span>
                          <span className="text-xs font-mono text-gray-700">{normalizedFile}</span>
                          {file.size && <span className="text-xs text-gray-300">{(file.size / 1024).toFixed(1)}KB</span>}
                          {fileDefects.length > 0 && (
                            <Tag color={presentation.color}>{presentation.label}</Tag>
                          )}
                        </div>
                      </List.Item>
                    );
                  }}
                />
              </Card>
            )}
            {/* 其他目录 */}
            {fileTree
              .filter(n => n.type === 'folder' && !Object.keys(DIR_META).includes(n.title))
              .map(n => renderDirSection(n.title))}
          </div>
        )}
      </Spin>

      {/* 文件预览弹窗 */}
      <Modal
        title={
          <Space>
            <FileTextOutlined />
            <span className="font-mono text-sm">{previewModal.name}</span>
          </Space>
        }
        open={previewModal.open}
        onCancel={() => setPreviewModal({ open: false, path: '', content: '', name: '' })}
        footer={[
          <a
            key="dl"
            href={`${API}/projects/${projectId}/files/download?path=${encodeURIComponent(previewModal.path)}`}
            target="_blank"
            rel="noreferrer"
          >
            <Button icon={<DownloadOutlined />}>下载</Button>
          </a>,
          <Button key="close" onClick={() => setPreviewModal({ open: false, path: '', content: '', name: '' })}>关闭</Button>,
        ]}
        width={800}
      >
        <div
          className="bg-gray-900 text-green-300 p-4 rounded font-mono text-xs overflow-auto"
          style={{ maxHeight: '60vh', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}
        >
          {previewModal.content || '（空文件）'}
        </div>
      </Modal>

      {/* 版本历史弹窗 */}
      <Modal
        title={<Space><HistoryOutlined /><span>版本历史</span></Space>}
        open={versionModal}
        onCancel={() => setVersionModal(false)}
        footer={<Button onClick={() => setVersionModal(false)}>关闭</Button>}
        width={640}
      >
        {versions.length === 0 ? (
          <Empty description="暂无提交记录" />
        ) : (
          <List
            size="small"
            dataSource={[...versions].reverse()}
            renderItem={(v: any) => (
              <List.Item
                actions={[
                  <Popconfirm
                    key="rollback"
                    title={`确认回滚到版本 v${v.version}？当前工作区文件将被覆盖。`}
                    onConfirm={() => handleRollback(v.version)}
                    okText="确认回滚"
                    cancelText="取消"
                  >
                    <Button size="small" icon={<RollbackOutlined />} loading={rollingBack} danger>
                      回滚
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Tag color="blue">v{v.version}</Tag>
                    <Text className="text-xs font-mono text-gray-400">{v.commit_id}</Text>
                    <Text type="secondary" className="text-xs">
                      {v.committed_at ? new Date(v.committed_at * 1000).toLocaleString('zh-CN') : ''}
                    </Text>
                  </div>
                  {v.message && <div className="text-xs text-gray-600">{v.message}</div>}
                  <div className="text-xs text-gray-400">
                    {v.files?.length || 0} 个文件：{(v.files || []).slice(0, 3).join('、')}{(v.files?.length || 0) > 3 ? '...' : ''}
                  </div>
                </div>
              </List.Item>
            )}
          />
        )}
      </Modal>

      {/* 文件复制确认弹窗 */}
      <Modal
        title="📋 复制执行记录到项目根目录"
        open={archiveModal}
        onCancel={() => { setArchiveModal(false); setSelectedForArchive(new Set()); }}
        onOk={archiveFiles}
        okText={archiving ? '复制中...' : `复制 ${selectedForArchive.size} 个文件`}
        okButtonProps={{ loading: archiving }}
        width={560}
      >
        <Alert
          type="warning"
          showIcon
          message="以下文件将从 output/ 复制到项目根目录；这不会使文件成为合格交付物"
          className="mb-3"
        />
        <List
          size="small"
          dataSource={[...selectedForArchive]}
          renderItem={(path: string) => (
            <List.Item>
              <Space>
                <FileTextOutlined className="text-gray-500" />
                <span className="font-mono text-xs">{path}</span>
                <span className="text-xs text-gray-400">→ {path.replace(/^output\//, '')}</span>
              </Space>
            </List.Item>
          )}
        />
      </Modal>
      {/* ── 问答对话弹窗（正常文件）─────────────────────────────────────────── */}
      <Modal
        title={
          <Space>
            <MessageOutlined style={{ color: '#3b82f6' }} />
            <span className="font-mono text-sm">{qaModal.fileName}</span>
            <Tag color="blue" className="text-xs">与全栈工程师对话</Tag>
          </Space>
        }
        open={qaModal.open}
        onCancel={() => setQaModal(m => ({ ...m, open: false }))}
        footer={null}
        width={680}
        styles={{ body: { padding: '12px 16px' } }}
      >
        {/* 提示：修改文件要弹确认 */}
        <Alert
          type="info"
          showIcon
          className="mb-3"
          message={
            <span className="text-xs">
              此文件暂无质检缺陷，可自由提问。若需修改文件内容，请点击
              <Button
                size="small"
                type="link"
                className="px-1 text-xs"
                onClick={() => setEditConfirmModal({ open: true, filePath: qaModal.filePath, content: '' })}
              >
                修改文件
              </Button>
              按钮进行二次确认。
            </span>
          }
        />
        {/* 对话区 */}
        <div
          className="bg-gray-50 rounded-lg p-3 overflow-y-auto space-y-2 mb-3"
          style={{ minHeight: 200, maxHeight: '40vh' }}
        >
          {qaMsgs.length === 0 && (
            <div className="text-xs text-gray-400 text-center py-6">
              可以问关于此文件的任何问题：逻辑说明、修改建议、潜在风险...
            </div>
          )}
          {qaMsgs.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap shadow-sm
                ${m.role === 'user' ? 'bg-blue-500 text-white' : 'bg-white border border-gray-200 text-gray-800'}`}>
                {m.content}
              </div>
            </div>
          ))}
          <div ref={qaMsgEndRef} />
        </div>
        {/* 输入区 */}
        <div className="flex gap-2">
          <Input
            placeholder="输入问题，如：这个函数的作用是什么？"
            value={qaInput}
            onChange={e => setQaInput(e.target.value)}
            onPressEnter={() => sendQAMsg(qaInput)}
            disabled={qaLoading}
            className="flex-1 text-xs"
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={qaLoading}
            disabled={!qaInput.trim()}
            onClick={() => sendQAMsg(qaInput)}
          >
            发送
          </Button>
        </div>
      </Modal>

      {/* ── 修改文件二次确认弹窗 ────────────────────────────────────────────── */}
      <Modal
        title={<Space><WarningOutlined style={{ color: '#f97316' }} /><span>确认修改文件</span></Space>}
        open={editConfirmModal.open}
        onCancel={() => setEditConfirmModal(m => ({ ...m, open: false }))}
        onOk={async () => {
          if (!editConfirmModal.content.trim()) {
            message.warning('请填写修改内容');
            return;
          }
          try {
            await axios.post(`${API}/projects/${projectId}/files/write`, {
              path: editConfirmModal.filePath,
              content: editConfirmModal.content,
            });
            message.success('文件已更新');
            setEditConfirmModal({ open: false, filePath: '', content: '' });
            fetchFiles();
          } catch (e: any) {
            message.error(e.response?.data?.detail || '写入失败');
          }
        }}
        okText="确认写入"
        okButtonProps={{ danger: true }}
        width={560}
      >
        <Alert type="warning" showIcon message="直接写入将覆盖当前文件内容，操作不可撤销（可通过版本历史回滚）" className="mb-3" />
        <div className="text-xs text-gray-500 mb-1 font-mono">{editConfirmModal.filePath}</div>
        <TextArea
          placeholder="粘贴新的文件内容..."
          rows={10}
          value={editConfirmModal.content}
          onChange={e => setEditConfirmModal(m => ({ ...m, content: e.target.value }))}
          className="font-mono text-xs"
        />
      </Modal>

      {/* ── 整改对话弹窗（有缺陷文件）──────────────────────────────────────── */}
      <Modal
        title={
          <Space>
            <ToolOutlined style={{ color: '#f97316' }} />
            <span className="font-mono text-sm">{repairModal.fileName}</span>
            <Tag color="orange" className="text-xs">⚠ {repairModal.defects.length} 个缺陷</Tag>
          </Space>
        }
        open={repairModal.open}
        onCancel={() => setRepairModal(m => ({ ...m, open: false }))}
        footer={null}
        width={720}
        styles={{ body: { padding: '12px 16px' } }}
      >
        {/* 缺陷切换 tabs */}
        {repairModal.defects.length > 1 && (
          <div className="flex gap-1 mb-3 flex-wrap">
            {repairModal.defects.map((d, i) => (
              <button
                key={d.id}
                onClick={() => setRepairModal(m => ({ ...m, activeDef: d }))}
                className={`text-xs px-2 py-0.5 rounded-full border transition
                  ${repairModal.activeDef?.id === d.id
                    ? 'bg-orange-100 border-orange-400 text-orange-700'
                    : 'border-gray-200 text-gray-500 hover:bg-gray-50'}`}
              >
                缺陷{i + 1}·{d.severity}
              </button>
            ))}
          </div>
        )}
        {/* 当前缺陷信息 */}
        {repairModal.activeDef && (
          <div className="bg-orange-50 border border-orange-200 rounded-lg p-3 mb-3">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="text-xs font-semibold text-orange-800 mb-1">
                  [{repairModal.activeDef.severity?.toUpperCase()}] {repairModal.activeDef.message}
                </div>
                {repairModal.activeDef.needs_manual_reason && (
                  <div className="text-xs text-orange-600 mb-1">⚠ {repairModal.activeDef.needs_manual_reason}</div>
                )}
                {repairModal.activeDef.fix_hint && (
                  <div className="text-xs text-blue-600">💡 {repairModal.activeDef.fix_hint}</div>
                )}
                {!canRepairDefect(repairModal.activeDef) && (
                  <div className="text-xs text-gray-600 mt-1">
                    {repairModal.activeDef.requires_identity_review
                      || repairModal.activeDef.identity_confidence !== 'high'
                      ? '等待身份复核：低置信缺陷不可直接整改'
                      : repairModal.activeDef.status === 'fixing'
                        ? '整改任务正在执行，不能重复提交'
                        : '整改已写入，正在等待权威复检'}
                  </div>
                )}
              </div>
              {/* 一键修改文件错误按钮 */}
              {canRepairDefect(repairModal.activeDef)
                && !confirmedPlanVersions[repairModal.activeDef.id]
                && fixProposals[repairModal.activeDef.id] && (
                <Button
                  size="small"
                  loading={repairLoading}
                  disabled={!fixProposals[repairModal.activeDef.id].confirmable}
                  title={fixProposals[repairModal.activeDef.id].error}
                  onClick={() => handleConfirmFixProposal(repairModal.activeDef!)}
                >
                  独立确认方案
                </Button>
              )}
              {canRepairDefect(repairModal.activeDef) && (
                <Button
                  type="primary"
                  size="small"
                  icon={<ToolOutlined />}
                  loading={fixingId === repairModal.activeDef.id}
                  disabled={!confirmedPlanVersions[repairModal.activeDef.id]}
                  onClick={() => repairModal.activeDef && handleOneClickFix(repairModal.activeDef)}
                  style={{ backgroundColor: '#f97316', borderColor: '#f97316', flexShrink: 0 }}
                >
                  {fixingId === repairModal.activeDef.id
                    ? '修复中...'
                    : confirmedPlanVersions[repairModal.activeDef.id]
                      ? '🔧 按已确认方案修复'
                      : '请先独立确认整改方案'}
                </Button>
              )}
            </div>
          </div>
        )}
        {/* 对话区 */}
        <div
          className="bg-gray-50 rounded-lg p-3 overflow-y-auto space-y-2 mb-3"
          style={{ minHeight: 180, maxHeight: '35vh' }}
        >
          {repairModal.activeDef && (repairMsgs[repairModal.activeDef.id] || []).length === 0 && (
            <div className="text-xs text-gray-400 text-center py-6">
              {repairLoading
                ? '全栈工程师正在分析缺陷...'
                : canRepairDefect(repairModal.activeDef)
                  ? '请先讨论并独立确认整改方案'
                  : '当前状态只读，请等待身份复核或权威质检'}
            </div>
          )}
          {repairModal.activeDef && (repairMsgs[repairModal.activeDef.id] || []).map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap shadow-sm
                ${m.role === 'user' ? 'bg-orange-500 text-white' : 'bg-white border border-gray-200 text-gray-800'}`}>
                {m.content}
              </div>
            </div>
          ))}
          {repairLoading && (
            <div className="flex justify-start">
              <div className="bg-white border border-gray-200 rounded-2xl px-3 py-2 text-xs text-gray-400">
                🤔 全栈工程师分析中...
              </div>
            </div>
          )}
          <div ref={repairMsgEndRef} />
        </div>
        {/* 输入区 */}
        <div className="flex gap-2">
          <Input
            placeholder="描述整改需求，或询问缺陷原因..."
            value={repairInput}
            onChange={e => setRepairInput(e.target.value)}
            onPressEnter={() => sendRepairMsg(repairInput)}
            disabled={repairLoading || !canRepairDefect(repairModal.activeDef)}
            className="flex-1 text-xs"
          />
          <Button
            icon={<SendOutlined />}
            loading={repairLoading}
            disabled={!repairInput.trim() || !canRepairDefect(repairModal.activeDef)}
            onClick={() => sendRepairMsg(repairInput)}
          >
            发送
          </Button>
        </div>
      </Modal>
    </div>
  );
};

export default FileBrowser;
