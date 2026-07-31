import React, { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import {
  Card, Button, Input, Form, message, Spin, Tag, Alert,
  Typography, Space, Divider, List, Empty,
} from "antd";
import {
  GithubOutlined, PushpinOutlined, ReloadOutlined,
  HistoryOutlined, CheckCircleOutlined, WarningOutlined,
} from "@ant-design/icons";
import { API_BASE_URL } from '../services/apiBase';

const { Text, Title } = Typography;
const API = API_BASE_URL;
const authFetch = (input: RequestInfo | URL, init: RequestInit = {}) =>
  fetch(input, { ...init, credentials: "include" });

interface GiteeStatus {
  configured: boolean;
  owner?: string;
  repo?: string;
  api_base?: string;
  last_sync?: number;
  error?: string;
}

const GiteePage: React.FC = () => {
  const { projectId } = useParams<{ projectId: string }>();
  const [status, setStatus] = useState<GiteeStatus>({ configured: false });
  const [loading, setLoading] = useState(true);
  const [pushing, setPushing] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [commitMsg, setCommitMsg] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [token, setToken] = useState("");
  const [versions, setVersions] = useState<any[]>([]);

  useEffect(() => { loadStatus(); }, []);

  const loadStatus = async () => {
    setLoading(true);
    try {
      const r = await authFetch(API + "/gitee/status");
      const data = await r.json();
      if (data.configured) {
        setStatus(data);
        setRepoUrl(data.api_base || "");
      }
      setStatus(data);
      // Load versions
      const vr = await authFetch(API + `/projects/${projectId}/files/versions`);
      if (vr.ok) setVersions(await vr.json() || []);
    } catch { message.error("\u65e0\u6cd5\u8fde\u63a5\u5230\u540e\u7aef"); }
    setLoading(false);
  };

  const configureGitee = async () => {
    if (!repoUrl || !token) { message.warning("\u8bf7\u586b\u5199 repo_url \u548c token"); return; }
    try {
      const r = await authFetch(API + "/gitee/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl, token }),
      });
      if (r.ok) { message.success("Gitee \u914d\u7f6e\u6210\u529f"); loadStatus(); }
      else message.error("\u914d\u7f6e\u5931\u8d25");
    } catch { message.error("\u8bf7\u6c42\u5931\u8d25"); }
  };

  const pushToGitee = async () => {
    setPushing(true);
    try {
      const r = await authFetch(API + "/gitee/push", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commit_message: commitMsg || undefined }),
      });
      if (r.ok) { message.success("\u63d0\u4ea4\u6210\u529f"); setCommitMsg(""); loadStatus(); }
      else { const e = await r.json(); message.error(e.detail || "\u63d0\u4ea4\u5931\u8d25"); }
    } catch { message.error("\u8bf7\u6c42\u5931\u8d25"); }
    setPushing(false);
  };

  const pullFromGitee = async () => {
    setPulling(true);
    try {
      const r = await authFetch(API + "/gitee/pull", { method: "POST" });
      if (r.ok) { message.success("\u62c9\u53d6\u6210\u529f"); loadStatus(); }
      else message.error("\u62c9\u53d6\u5931\u8d25");
    } catch { message.error("\u8bf7\u6c42\u5931\u8d25"); }
    setPulling(false);
  };

  if (loading) return <Spin style={{ display: "block", margin: "80px auto" }} />;

  return (
    <div style={{ padding: "16px 20px", maxWidth: 700, margin: "0 auto" }}>
      <Card title={<span><GithubOutlined /> Gitee \u540c\u6b65</span>}>
        {!status.configured ? (
          <>
            <Alert message="\u5c1a\u672a\u914d\u7f6e Gitee \u8fde\u63a5" type="warning" showIcon style={{ marginBottom: 16 }} />
            <Form layout="vertical">
              <Form.Item label="\u4ed3\u5e93 URL">
                <Input placeholder="https://gitee.com/username/repo.git" value={repoUrl} onChange={e => setRepoUrl(e.target.value)} />
              </Form.Item>
              <Form.Item label="Access Token">
                <Input.Password placeholder="\u8f93\u5165 Gitee Token" value={token} onChange={e => setToken(e.target.value)} />
              </Form.Item>
              <Button type="primary" icon={<PushpinOutlined />} onClick={configureGitee}> \u7ed1\u5b9a\u4ed3\u5e93</Button>
            </Form>
          </>
        ) : (
          <>
            <Space direction="vertical" style={{ width: "100%" }}>
              <div><Text strong>\u4ed3\u5e93\uff1a</Text> {status.owner}/{status.repo}</div>
              <div><Text strong>\u72b6\u6001\uff1a</Text> <Tag color="green"><CheckCircleOutlined /> \u5df2\u914d\u7f6e</Tag></div>
              {status.last_sync && <div><Text strong>\u4e0a\u6b21\u540c\u6b65\uff1a</Text> {new Date(status.last_sync * 1000).toLocaleString()}</div>}
            </Space>
            <Divider />
            <Space direction="vertical" style={{ width: "100%" }}>
              <Text strong>\u63d0\u4ea4\u5230 Gitee</Text>
              <Input.TextArea rows={2} placeholder="\u8f93\u5165\u63d0\u4ea4\u8bf4\u660e" value={commitMsg} onChange={e => setCommitMsg(e.target.value)} />
              <Space>
                <Button type="primary" icon={<GithubOutlined />} loading={pushing} onClick={pushToGitee}> \u63d0\u4ea4\u63a8\u9001</Button>
                <Button icon={<ReloadOutlined />} loading={pulling} onClick={pullFromGitee}> \u4ece Gitee \u62c9\u53d6</Button>
              </Space>
            </Space>
          </>
        )}
      </Card>

      {versions.length > 0 && (
        <Card title={<span><HistoryOutlined /> \u7248\u672c\u5386\u53f2</span>} style={{ marginTop: 16 }}>
          <List
            dataSource={versions}
            renderItem={(v: any, idx) => (
              <List.Item>
                <Text>v{v.version || versions.length - idx}</Text>
                <Text style={{ flex: 1, marginLeft: 12 }}>{v.message || "\u65e0\u63d0\u4ea4\u8bf4\u660e"}</Text>
                <Text type="secondary">{v.timestamp ? new Date(v.timestamp * 1000).toLocaleString() : ""}</Text>
              </List.Item>
            )}
          />
        </Card>
      )}
    </div>
  );
};

export default GiteePage;
