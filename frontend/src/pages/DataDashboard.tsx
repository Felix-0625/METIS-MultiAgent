import React, { useEffect, useState } from 'react';
import { Button, Card, Col, Empty, Row, Space, Spin, Statistic, Table, Tag } from 'antd';
import { MinusOutlined, PlusOutlined } from '@ant-design/icons';
import {
  Bar, BarChart, CartesianGrid, LabelList, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { apiClient } from '../services/api';

const DataDashboard: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [tokenPointWidth, setTokenPointWidth] = useState(90);
  const [durationBarWidth, setDurationBarWidth] = useState(90);

  const load = async () => {
    try { setData(await apiClient.get('/dashboard/overview')); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, []);

  if (loading) return <div className="flex justify-center p-16"><Spin /></div>;
  const projects = data?.projects || [];
  const tokenProjects = data?.token_projects || projects;
  const summary = data?.summary || {};
  const sortedProjects = [...tokenProjects].sort((a: any, b: any) => b.total_tokens - a.total_tokens);
  const duplicateNames = sortedProjects.reduce((counts: Record<string, number>, project: any) => {
    counts[project.project_name] = (counts[project.project_name] || 0) + 1;
    return counts;
  }, {});
  const tokenChartProjects = sortedProjects.map((project: any) => ({
    ...project,
    display_name: duplicateNames[project.project_name] > 1
      ? `${project.project_name} (${String(project.project_id).replace(/^proj-/, '')})`
      : project.project_name,
  }));
  const compactNumber = (value: number) => {
    if (value >= 10000) return `${Math.round(value / 10000)}万`;
    if (value >= 1000) return `${Math.round(value / 1000)}千`;
    return String(value);
  };
  const metricCardStyles = {
    body: {
      minHeight: 110,
      height: '100%',
      display: 'flex',
      flexDirection: 'column' as const,
      justifyContent: 'center',
    },
  };
  const zoomControls = (value: number, setValue: (next: number) => void) => (
    <Space.Compact size="small">
      <Button aria-label="缩小" icon={<MinusOutlined />} disabled={value <= 60} onClick={() => setValue(Math.max(60, value - 20))} />
      <Button disabled style={{ width: 58, color: '#595959' }}>{Math.round(value / 90 * 100)}%</Button>
      <Button aria-label="放大" icon={<PlusOutlined />} disabled={value >= 180} onClick={() => setValue(Math.min(180, value + 20))} />
    </Space.Compact>
  );

  return <div className="space-y-4">
    <style>{`.dashboard-chart-scroll::-webkit-scrollbar{display:none}`}</style>
    <div>
      <h2 className="text-xl font-semibold mb-1">数据看板</h2>
      <div className="text-sm text-gray-500">Token 按项目累计；删除项目后仅保留 Token 历史，其他项目数据同步清除。每 5 秒刷新。</div>
    </div>
    <Row gutter={[16, 16]}>
      <Col xs={12} lg={6}><Card style={{ height: '100%' }} styles={metricCardStyles}><Statistic title="用户 Token 总消耗" value={summary.total_tokens ?? 0} /></Card></Col>
      <Col xs={12} lg={6}><Card style={{ height: '100%' }} styles={metricCardStyles}><Statistic title="模型请求总数" value={summary.total_model_requests ?? 0} suffix="次" /></Card></Col>
      <Col xs={12} lg={6}><Card style={{ height: '100%' }} styles={metricCardStyles}><Statistic title="项目平均 Agent 执行时长" value={summary.average_agent_duration_seconds ?? '-'} suffix={summary.average_agent_duration_seconds == null ? undefined : '秒'} /></Card></Col>
      <Col xs={12} lg={6}><Card style={{ height: '100%' }} styles={metricCardStyles}><Statistic title="项目平均交付文件" value={summary.average_delivery_files_per_project ?? '-'} suffix={summary.average_delivery_files_per_project == null ? undefined : '个'} /></Card></Col>
    </Row>
    {projects.length === 0 ? <Card><Empty description="暂无项目数据" /></Card> : <>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}><Card title="各项目 Token 消耗" extra={zoomControls(tokenPointWidth, setTokenPointWidth)}><div className="dashboard-chart-scroll" style={{ width: '100%', overflowX: 'auto', scrollbarWidth: 'none' }}><div style={{ width: Math.max(480, tokenChartProjects.length * tokenPointWidth), height: 340 }}><ResponsiveContainer><LineChart data={tokenChartProjects} margin={{ left: 42, right: 42, bottom: 26, top: 28 }}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="display_name" interval={0} angle={-18} textAnchor="end" height={72}/><YAxis width={64} domain={[0, 'auto']} tickFormatter={compactNumber}/><Tooltip formatter={(value: any) => Number(value).toLocaleString()}/><Line type="monotone" dataKey="total_tokens" name="Token" stroke="#7c3aed" strokeWidth={3} dot={{ r: 5 }} activeDot={{ r: 7 }}><LabelList dataKey="total_tokens" position="top" formatter={(value: any) => Number(value).toLocaleString()}/></Line></LineChart></ResponsiveContainer></div></div></Card></Col>
        <Col xs={24} xl={12}><Card title="各项目 Agent 平均执行时长" extra={zoomControls(durationBarWidth, setDurationBarWidth)}><div className="dashboard-chart-scroll" style={{ width: '100%', overflowX: 'auto', scrollbarWidth: 'none' }}><div style={{ width: Math.max(480, projects.length * durationBarWidth), height: 320 }}><ResponsiveContainer><BarChart data={projects} margin={{ bottom: 18 }}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="project_name"/><YAxis/><Tooltip/><Legend/><Bar dataKey="average_agent_duration_seconds" name="平均时长 秒" fill="#1677ff" barSize={36} maxBarSize={40}/></BarChart></ResponsiveContainer></div></div></Card></Col>
      </Row>
      <Card title="项目明细"><Table rowKey="project_id" pagination={{ pageSize: 10, showSizeChanger: true }} dataSource={projects} columns={[
        { title:'项目', dataIndex:'project_name' },
        { title:'状态', dataIndex:'status', render:(v:string)=><Tag>{v}</Tag> },
        { title:'Token', dataIndex:'total_tokens' },
        { title:'平均执行时长', dataIndex:'average_agent_duration_seconds', render:(v:number|null)=>v == null ? '-' : `${v} 秒` },
        { title:'交付文件', dataIndex:'delivery_file_count' },
      ]}/></Card>
    </>}
  </div>;
};

export default DataDashboard;
