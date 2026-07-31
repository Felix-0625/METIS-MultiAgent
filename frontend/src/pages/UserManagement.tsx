/**
 * 用户管理页面（仅管理员可见）
 * - 查看所有用户列表
 * - 创建新用户
 * - 删除用户
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Table, Button, Space, Tag, message, Modal, Form, Input, Select, Popconfirm, Typography,
} from 'antd';
import {
  UserAddOutlined, DeleteOutlined, ReloadOutlined,
  CrownOutlined, UserOutlined,
} from '@ant-design/icons';
import { authApi } from '../services/api';

const { Title } = Typography;

interface UserInfo {
  user_id: string;
  username: string;
  role: string;
  created_at: number;
}

const UserManagement: React.FC = () => {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [form] = Form.useForm();

  const fetchUsers = async () => {
    setLoading(true);
    try {
      const data: any = await authApi.listUsers();
      setUsers(Array.isArray(data) ? data : []);
    } catch (err: any) {
      message.error('获取用户列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const handleCreate = async (values: { username: string; password: string; role: string }) => {
    setCreateLoading(true);
    try {
      await authApi.register(values.username, values.password, values.role);
      message.success(`用户 "${values.username}" 创建成功`);
      setCreateModalOpen(false);
      form.resetFields();
      fetchUsers();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '创建失败');
    } finally {
      setCreateLoading(false);
    }
  };

  const handleDelete = async (userId: string) => {
    try {
      await authApi.deleteUser(userId);
      message.success('用户已删除');
      fetchUsers();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '删除失败');
    }
  };

  const formatTime = (ts: number) => {
    if (!ts) return '-';
    return new Date(ts * 1000).toLocaleString('zh-CN');
  };

  const columns = [
    {
      title: '用户名',
      dataIndex: 'username',
      key: 'username',
      render: (text: string, record: UserInfo) => (
        <Space>
          {record.role === 'admin' ? (
            <CrownOutlined style={{ color: '#faad14' }} />
          ) : (
            <UserOutlined style={{ color: '#8c8c8c' }} />
          )}
          <span>{text}</span>
        </Space>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      render: (role: string) => (
        <Tag color={role === 'admin' ? 'gold' : 'blue'}>
          {role === 'admin' ? '管理员' : '普通用户'}
        </Tag>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (ts: number) => formatTime(ts),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_: any, record: UserInfo) => {
        const currentUser = JSON.parse(localStorage.getItem('auth_user') || '{}');
        if (record.user_id === currentUser.user_id) {
          return <Tag color="default">当前用户</Tag>;
        }
        return (
          <Popconfirm
            title={`确定要删除用户 "${record.username}" 吗？`}
            onConfirm={() => handleDelete(record.user_id)}
            okText="确定删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" danger icon={<DeleteOutlined />} size="small">
              删除
            </Button>
          </Popconfirm>
        );
      },
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Card
        title={
          <Space>
            <Title level={4} style={{ margin: 0 }}>用户管理</Title>
            <Tag color="gold">管理员功能</Tag>
          </Space>
        }
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchUsers} loading={loading}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<UserAddOutlined />}
              onClick={() => setCreateModalOpen(true)}
            >
              创建用户
            </Button>
          </Space>
        }
      >
        <Table
          columns={columns}
          dataSource={users}
          rowKey="user_id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: '暂无用户' }}
        />
      </Card>

      {/* 创建用户弹窗 */}
      <Modal
        title="创建新用户"
        open={createModalOpen}
        onCancel={() => {
          setCreateModalOpen(false);
          form.resetFields();
        }}
        footer={null}
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleCreate}
          initialValues={{ role: 'user' }}
        >
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 2, message: '至少2个字符' },
              { max: 64, message: '最多64个字符' },
            ]}
          >
            <Input placeholder="登录用户名" autoFocus />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '至少6个字符' },
            ]}
          >
            <Input.Password placeholder="登录密码（至少6位）" />
          </Form.Item>

          <Form.Item name="role" label="角色">
            <Select>
              <Select.Option value="user">普通用户</Select.Option>
              <Select.Option value="admin">管理员</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setCreateModalOpen(false)}>取消</Button>
              <Button type="primary" htmlType="submit" loading={createLoading}>
                创建
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default UserManagement;