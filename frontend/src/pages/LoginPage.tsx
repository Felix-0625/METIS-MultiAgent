/**
 * 登录页面 — 三 Tab 切换：登录 / 注册 / 忘记密码
 * 使用 HttpOnly Cookie 存储 Token（前端 JS 不可读取，防止 XSS 窃取）
 */

import React, { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Button, Card, Form, Input, message, Typography, Divider, Tabs } from 'antd';
import { UserOutlined, LockOutlined, MailOutlined, LoginOutlined, SafetyOutlined, KeyOutlined } from '@ant-design/icons';
import { authApi } from '../services/api';

const { Text } = Typography;

type TabKey = 'login' | 'register' | 'forgot';
const USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/;
const USERNAME_RULE_MESSAGE = '用户名须为3-64位，以字母或数字开头，且只能包含字母、数字、点、下划线和连字符';

const apiErrorMessage = (error: any, fallback: string): string => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => String(item?.msg || '').replace(/^Value error,\s*/, ''))
      .filter(Boolean)
      .join('；') || fallback;
  }
  return fallback;
};

const LoginPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('login');

  // 注册流程状态
  const [registerEmail, setRegisterEmail] = useState('');
  const [showVerifyCode, setShowVerifyCode] = useState(false);

  // 忘记密码流程状态
  const [forgotEmailSent, setForgotEmailSent] = useState(false);
  const [resetEmail, setResetEmail] = useState('');

  // 检查是否已登录
  useEffect(() => {
    authApi.me()
      .then(() => navigate('/projects', { replace: true }))
      .catch(() => {/* 未登录，保持登录页 */});
  }, []);

  // ═══════════════ 登录 ═══════════════
  const handleLogin = async (values: { login: string; password: string }) => {
    setLoading(true);
    try {
      const res: any = await authApi.login(values.login, values.password);
      sessionStorage.setItem('current_user', JSON.stringify({
        user_id: res.user_id,
        username: res.username,
        role: res.role,
      }));
      window.dispatchEvent(new CustomEvent('metis:authenticated'));
      message.success(`欢迎回来，${res.username}！`);
      const from = (location.state as any)?.from || '/projects';
      navigate(from, { replace: true });
    } catch (err: any) {
      message.error(apiErrorMessage(err, '登录失败，请检查用户名和密码'));
    } finally {
      setLoading(false);
    }
  };

  // ═══════════════ 注册 ═══════════════
  const handleRegister = async (values: { username: string; password: string; email: string }) => {
    setLoading(true);
    try {
      const res: any = await authApi.register(values.username, values.password, values.email);
      if (res?.email_verification_required) {
        setRegisterEmail(values.email);
        setShowVerifyCode(true);
        message.success('注册成功！验证邮件已发送');
      } else {
        message.success('注册成功，现在可以登录了');
        setActiveTab('login');
      }
    } catch (err: any) {
      message.error(apiErrorMessage(err, '注册失败，请重试'));
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyEmail = async (values: { code: string }) => {
    setLoading(true);
    try {
      await authApi.verifyEmail(registerEmail, values.code);
      message.success('邮箱验证成功，现在可以登录了');
      setActiveTab('login');
      setShowVerifyCode(false);
      setRegisterEmail('');
    } catch (err: any) {
      const detail = err?.response?.data?.detail || '验证失败';
      message.error(detail);
    } finally {
      setLoading(false);
    }
  };

  const handleResendCode = async () => {
    try {
      await authApi.resendVerification(registerEmail);
      message.success('验证邮件已重新发送');
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '重发失败，请稍后重试');
    }
  };

  // ═══════════════ 忘记密码 ═══════════════
  const handleForgotPassword = async (values: { email: string }) => {
    setLoading(true);
    try {
      await authApi.forgotPassword(values.email);
      setResetEmail(values.email);
      setForgotEmailSent(true);
      message.info('如果该邮箱已注册，重置邮件已发送');
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '请求失败');
    } finally {
      setLoading(false);
    }
  };

  const handleResetPassword = async (values: { code: string; new_password: string }) => {
    setLoading(true);
    try {
      await authApi.resetPassword(resetEmail, values.code, values.new_password);
      message.success('密码重置成功，请使用新密码登录');
      setActiveTab('login');
      setForgotEmailSent(false);
      setResetEmail('');
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '重置失败');
    } finally {
      setLoading(false);
    }
  };

  // ═══════════════ 渲染 ═══════════════

  const tabItems = [
    {
      key: 'login',
      label: '登录',
      children: (
        <Form name="login" onFinish={handleLogin} size="large" autoComplete="off">
          <Form.Item name="login" rules={[{ required: true, message: '请输入用户名或邮箱' }]}>
            <Input prefix={<UserOutlined style={{ color: '#bfbfbf' }} />} placeholder="用户名或邮箱" autoFocus />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined style={{ color: '#bfbfbf' }} />} placeholder="密码" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" loading={loading} block icon={<LoginOutlined />}
              style={{ height: 44, borderRadius: 8, background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', border: 'none', fontSize: 15, fontWeight: 600 }}>
              登 录
            </Button>
          </Form.Item>
        </Form>
      ),
    },
    {
      key: 'register',
      label: '注册',
      children: !showVerifyCode ? (
        <Form name="register" onFinish={handleRegister} size="large" autoComplete="off">
          <Form.Item
            name="username"
            extra={USERNAME_RULE_MESSAGE}
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: USERNAME_PATTERN, message: USERNAME_RULE_MESSAGE },
            ]}
          >
            <Input
              prefix={<UserOutlined style={{ color: '#bfbfbf' }} />}
              placeholder="用户名"
              minLength={3}
              maxLength={64}
            />
          </Form.Item>
          <Form.Item name="email" rules={[
            { required: true, message: '请输入邮箱' },
            { type: 'email', message: '邮箱格式不正确' },
          ]}>
            <Input prefix={<MailOutlined style={{ color: '#bfbfbf' }} />} placeholder="邮箱" />
          </Form.Item>
          <Form.Item name="password" rules={[
            { required: true, message: '请输入密码' },
            { min: 6, message: '密码至少6位' },
          ]}>
            <Input.Password prefix={<LockOutlined style={{ color: '#bfbfbf' }} />} placeholder="密码（至少6位）" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" loading={loading} block icon={<LoginOutlined />}
              style={{ height: 44, borderRadius: 8, background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', border: 'none', fontSize: 15, fontWeight: 600 }}>
              注 册
            </Button>
          </Form.Item>
        </Form>
      ) : (
        <div>
          <div style={{ textAlign: 'center', marginBottom: 16 }}>
            <SafetyOutlined style={{ fontSize: 40, color: '#667eea' }} />
            <p style={{ marginTop: 8, color: '#666' }}>验证邮件已发送至 <strong>{registerEmail}</strong></p>
          </div>
          <Form name="verifyEmail" onFinish={handleVerifyEmail} size="large">
            <Form.Item name="code" rules={[{ required: true, message: '请输入6位验证码' }]}>
              <Input prefix={<KeyOutlined style={{ color: '#bfbfbf' }} />} placeholder="6位验证码" maxLength={6} autoFocus />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading} block
                style={{ height: 44, borderRadius: 8 }}>
                验证邮箱
              </Button>
            </Form.Item>
          </Form>
          <div style={{ textAlign: 'center' }}>
            <Button type="link" onClick={handleResendCode}>未收到邮件？重新发送</Button>
            <br />
            <Button type="link" onClick={() => { setShowVerifyCode(false); setRegisterEmail(''); }}>
              返回注册
            </Button>
          </div>
        </div>
      ),
    },
    {
      key: 'forgot',
      label: '忘记密码',
      children: !forgotEmailSent ? (
        <Form name="forgotPassword" onFinish={handleForgotPassword} size="large">
          <Form.Item name="email" rules={[
            { required: true, message: '请输入注册邮箱' },
            { type: 'email', message: '邮箱格式不正确' },
          ]}>
            <Input prefix={<MailOutlined style={{ color: '#bfbfbf' }} />} placeholder="注册邮箱" autoFocus />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block icon={<SafetyOutlined />}
              style={{ height: 44, borderRadius: 8, background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', border: 'none', fontSize: 15, fontWeight: 600 }}>
              发送重置邮件
            </Button>
          </Form.Item>
        </Form>
      ) : (
        <div>
          <div style={{ textAlign: 'center', marginBottom: 16 }}>
            <SafetyOutlined style={{ fontSize: 40, color: '#667eea' }} />
            <p style={{ marginTop: 8, color: '#666' }}>
              验证码已发送至 <strong>{resetEmail}</strong>，请输入验证码和新密码
            </p>
          </div>
          <Form name="resetPassword" onFinish={handleResetPassword} size="large">
            <Form.Item name="code" rules={[{ required: true, message: '请输入验证码' }]}>
              <Input prefix={<KeyOutlined style={{ color: '#bfbfbf' }} />} placeholder="6位验证码" maxLength={6} autoFocus />
            </Form.Item>
            <Form.Item name="new_password" rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '密码至少6位' },
            ]}>
              <Input.Password prefix={<LockOutlined style={{ color: '#bfbfbf' }} />} placeholder="新密码（至少6位）" />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading} block
                style={{ height: 44, borderRadius: 8, background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', border: 'none', fontSize: 15, fontWeight: 600 }}>
                重置密码
              </Button>
            </Form.Item>
          </Form>
          <div style={{ textAlign: 'center' }}>
            <Button type="link" onClick={() => { setForgotEmailSent(false); setResetEmail(''); }}>
              返回重新发送
            </Button>
          </div>
        </div>
      ),
    },
  ];

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%)',
      padding: 24,
    }}>
      <Card
        style={{
          width: 420,
          borderRadius: 16,
          boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
          border: '1px solid rgba(255,255,255,0.1)',
          background: 'rgba(255,255,255,0.95)',
        }}
        styles={{ body: { padding: '40px 32px' } }}
      >
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div style={{
            fontSize: 32, fontWeight: 800, letterSpacing: 6,
            background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
            marginBottom: 8,
          }}>
            M E T I S
          </div>
          <Text type="secondary" style={{ fontSize: 12, letterSpacing: 2 }}>
            AI MULTI-AGENT SYSTEM
          </Text>
        </div>

        <Divider style={{ margin: '0 0 20px' }} />

        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key as TabKey);
            setShowVerifyCode(false);
            setForgotEmailSent(false);
          }}
          centered
          items={tabItems}
        />
      </Card>
    </div>
  );
};

export default LoginPage;
