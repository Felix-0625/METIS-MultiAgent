import { useState, useEffect } from 'react';
import axios from 'axios';
import { PhaseInfo, ReviewResult, FileRecord, ChatMsg, API } from '../types';

interface UsePhaseDataReturn {
  phases: PhaseInfo[];
  loading: boolean;
  allAgents: Record<string, any>;
  phaseFiles: Record<string, FileRecord[]>;
  pmChats: Record<string, ChatMsg[]>;
  supChats: Record<string, ChatMsg[]>;
  reviewResults: Record<string, ReviewResult>;
  projectMetrics: any;
  qcResultsSummary: any;
  loadPhases: () => Promise<void>;
  loadAgents: () => Promise<void>;
  loadPhaseFiles: (phaseId: string) => Promise<void>;
  setPmChats: React.Dispatch<React.SetStateAction<Record<string, ChatMsg[]>>>;
  setSupChats: React.Dispatch<React.SetStateAction<Record<string, ChatMsg[]>>>;
  setReviewResults: React.Dispatch<React.SetStateAction<Record<string, ReviewResult>>>;
  savePmHistory: (phaseId: string, msgs: ChatMsg[]) => Promise<void>;
  saveSupHistory: (phaseId: string, msgs: ChatMsg[]) => Promise<void>;
}

export const usePhaseData = (projectId: string | undefined): UsePhaseDataReturn => {
  const [phases, setPhases] = useState<PhaseInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [allAgents, setAllAgents] = useState<Record<string, any>>({});
  const [phaseFiles, setPhaseFiles] = useState<Record<string, FileRecord[]>>({});
  const [pmChats, setPmChats] = useState<Record<string, ChatMsg[]>>({});
  const [supChats, setSupChats] = useState<Record<string, ChatMsg[]>>({});
  const [projectMetrics, setProjectMetrics] = useState<any>(null);
  const [qcResultsSummary, setQcResultsSummary] = useState<any>(null);

  const [reviewResults, setReviewResults] = useState<Record<string, ReviewResult>>(() => {
    try {
      const v = sessionStorage.getItem(`phase_reviews_${projectId}`);
      return v ? JSON.parse(v) : {};
    } catch {
      return {};
    }
  });

  // 自动保存 reviewResults 到 sessionStorage
  useEffect(() => {
    try {
      sessionStorage.setItem(`phase_reviews_${projectId}`, JSON.stringify(reviewResults));
    } catch {}
  }, [reviewResults, projectId]);

  const loadAllPhaseHistories = async (phaseList: PhaseInfo[]) => {
    for (const phase of phaseList) {
      const pid = phase.phase_id;
      try {
        const [pmRes, supRes]: any[] = await Promise.all([
          axios.get(`${API}/projects/${projectId}/chat-history/pm_phase_${pid}`).catch(() => ({ data: { messages: [] } })),
          axios.get(`${API}/projects/${projectId}/chat-history/sup_phase_${pid}`).catch(() => ({ data: { messages: [] } })),
        ]);
        if (pmRes.data.messages?.length > 0) setPmChats(c => ({ ...c, [pid]: pmRes.data.messages }));
        if (supRes.data.messages?.length > 0) setSupChats(c => ({ ...c, [pid]: supRes.data.messages }));
      } catch {}
    }
  };

  const loadPhases = async () => {
    if (!projectId) return;
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/phases`);
      const phaseList = res.data.phases || [];
      setPhases(phaseList);
      if (phaseList.length > 0 && Object.keys(pmChats).length === 0) {
        loadAllPhaseHistories(phaseList);
      }
      // 同步 reviewResults 中的 issues
      for (const phase of phaseList) {
        const pid = phase.phase_id;
        if (reviewResults[pid]) {
          try {
            const issRes: any = await axios.get(`${API}/projects/${projectId}/phases/${pid}/issues`);
            if (issRes.data.issues?.length > 0) {
              setReviewResults(prev => {
                const old = prev[pid];
                if (!old) return prev;
                return {
                  ...prev,
                  [pid]: {
                    ...old,
                    issues: issRes.data.issues,
                    passed: issRes.data.passed,
                    error_count: issRes.data.open_count || 0,
                  },
                };
              });
            }
          } catch {}
        }
      }
    } catch {
    } finally {
      setLoading(false);
    }
  };

  const savePmHistory = async (phaseId: string, msgs: ChatMsg[]) => {
    if (!projectId) return;
    try {
      await axios.post(`${API}/projects/${projectId}/chat-history/pm_phase_${phaseId}`, {
        messages: msgs.map(m => ({
          role: m.role,
          content: m.content,
          ts: m.ts,
          ...(m.type ? { type: m.type } : {}),
        })),
      });
    } catch {}
  };

  const saveSupHistory = async (phaseId: string, msgs: ChatMsg[]) => {
    if (!projectId) return;
    try {
      await axios.post(`${API}/projects/${projectId}/chat-history/sup_phase_${phaseId}`, {
        messages: msgs.map(m => ({ role: m.role, content: m.content, ts: m.ts })),
      });
    } catch {}
  };

  const loadAgents = async () => {
    if (!projectId) return;
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/agents`);
      const m: Record<string, any> = {};
      (res.data.agents || []).forEach((a: any) => {
        m[a.id] = a;
      });
      setAllAgents(m);
    } catch {}
  };

  const loadPhaseFiles = async (phaseId: string) => {
    if (!projectId) return;
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/phases/${phaseId}/files`);
      setPhaseFiles(prev => ({ ...prev, [phaseId]: res.data.files || [] }));
    } catch {}
  };

  const loadProjectMetrics = async () => {
    if (!projectId) return;
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/metrics`);
      setProjectMetrics(res.data);
    } catch {}
  };

  const loadQcResults = async () => {
    if (!projectId) return;
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/qc/results`);
      setQcResultsSummary(res.data);
    } catch {}
  };

  // 初始加载
  useEffect(() => {
    if (!projectId) return;
    loadPhases();
    loadAgents();
    loadProjectMetrics();
    loadQcResults();
  }, [projectId]);

  return {
    phases,
    loading,
    allAgents,
    phaseFiles,
    pmChats,
    supChats,
    reviewResults,
    projectMetrics,
    qcResultsSummary,
    loadPhases,
    loadAgents,
    loadPhaseFiles,
    setPmChats,
    setSupChats,
    setReviewResults,
    savePmHistory,
    saveSupHistory,
  };
};
