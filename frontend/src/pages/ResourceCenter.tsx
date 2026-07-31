/**
 * 资源中心
 * Tab 模式：Agent 池 | 专家池 | Skill 池
 */

import React from 'react';
import { Tabs } from 'antd';
import { TeamOutlined, UserOutlined, ApiOutlined } from '@ant-design/icons';
import AgentPool from './AgentPool';
import ExpertPool from './ExpertPool';
import SkillPool from './SkillPool';

const ResourceCenter: React.FC = () => {
  return (
    <div style={{ padding: 24, minHeight: '100vh' }}>
      <Tabs
        defaultActiveKey="agents"
        size="large"
        items={[
          {
            key: 'agents',
            label: (
              <span>
                <TeamOutlined /> Agent 池
              </span>
            ),
            children: <AgentPool />,
          },
          {
            key: 'experts',
            label: (
              <span>
                <UserOutlined /> 专家池
              </span>
            ),
            children: <ExpertPool />,
          },
          {
            key: 'skills',
            label: (
              <span>
                <ApiOutlined /> Skill 池
              </span>
            ),
            children: <SkillPool />,
          },
        ]}
      />
    </div>
  );
};

export default ResourceCenter;
