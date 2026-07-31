# DevOps 与部署

- **版本**: 1.0.0
- **描述**: 负责 CI/CD 流水线搭建、容器化部署、监控告警、基础设施即代码
- **标签**: pg, devops, docker, kubernetes
- **分配给**: pg

---

## DevOps 与部署 Skill

### 技术栈
- 容器：Docker / Docker Compose
- 编排：Kubernetes / Docker Swarm
- CI/CD：GitHub Actions / GitLab CI / Jenkins
- 监控：Prometheus + Grafana / ELK Stack
- IaC：Terraform / Ansible

### 流水线规范
1. 代码提交触发：lint → test → build → deploy
2. 环境隔离：dev / staging / production
3. 蓝绿部署或滚动更新，零停机
4. 自动回滚：健康检查失败时自动回滚
5. 密钥管理：不在代码中硬编码，使用 Vault/Secrets

### 输出
- Dockerfile + docker-compose.yml
- CI/CD 配置文件
- Kubernetes Manifests / Helm Chart
- 监控告警规则