# 周报助手设计文档

## 概述

基于 NanoClaw 的周报生成工具。核心逻辑用 Python CLI 实现 GitHub PR 数据采集，NanoClaw Container Skill 负责对话交互、AI 总结和输出格式化。最终通过企业微信工作台提供给团队成员使用，每人与机器人单聊，数据隔离。

## 架构

```
┌─────────────────────────────────┐
│       企业微信工作台              │
│  用户 ←单聊→ 智能机器人（长连接）  │
└──────────┬──────────────────────┘
           │ WebSocket
   ┌───────▼────────┐
   │   NanoClaw     │
   │ (中心化单实例)   │
   ├────────────────┤
   │ wecom channel  │  收发消息
   │ container pool │  每人独立容器
   │ daily-report   │  Container Skill
   │   skill        │
   └───────┬────────┘
           │ GitHub API (组织级 token)
   ┌───────▼────────┐
   │   GitHub API   │
   └────────────────┘
```

## 项目结构

```
daily-report/
├── cli.py              # GitHub PR 数据采集，输出 JSON
├── requirements.txt    # Python 依赖
└── SKILL.md            # NanoClaw Container Skill 描述
```

## 模块设计

### 1. cli.py — GitHub 数据采集

**职责**：调用 GitHub Search API，采集指定用户在指定日期范围内的 PR 数据，输出结构化 JSON。

**参数**：

| 参数 | 说明 | 示例 |
|------|------|------|
| `--user` | GitHub 用户名 | `aqqi666` |
| `--org` | GitHub 组织名 | `matrixorigin` |
| `--since` | 开始日期（含） | `2026-03-17` |
| `--until` | 结束日期（含） | `2026-03-21` |
| `--token` | GitHub token（组织级） | 环境变量 `GITHUB_TOKEN` |

**采集内容**：

- 用户创建的 PR（`author:{user} org:{org} type:pr`）
- 用户 review 的 PR（`reviewed-by:{user} org:{org} type:pr`）
- 去重：同一 PR 既是 author 又 review 过，合并角色

**输出格式**（JSON，stdout）：

```json
[
  {
    "repo": "matrixflow",
    "pr_number": 8740,
    "title": "test(moi-core/tests): add standalone mowl workflow execution E2E tests",
    "state": "open",
    "role": ["author"],
    "created_at": "2026-03-24",
    "merged_at": null,
    "url": "https://github.com/matrixorigin/matrixflow/pull/8740"
  }
]
```

**实现**：用 `subprocess` 调用本机 `gh api` 或直接用 `urllib` 请求 GitHub REST API。优先 `urllib`（标准库，无外部依赖，且中心化部署时不依赖 `gh` CLI）。

**依赖**：仅标准库（`urllib`、`json`、`argparse`、`datetime`）。`requirements.txt` 为空或不需要。

### 2. SKILL.md — NanoClaw Container Skill

**职责**：定义 agent 的行为指令，包括对话交互、日期推断、AI 总结、输出格式化。

**核心指令**：

#### 日期范围推断

- 周四、周五：默认本周一 ~ 今天 → 本周周报
- 周一、周二、周三：默认上周一 ~ 上周五 → 补上周周报
- 用户可通过对话修正（如"我要写上周的"）

#### 用户身份

- 首次使用时询问 GitHub 用户名，记到 group 的 CLAUDE.md 记忆中
- 后续自动读取，不再询问

#### 数据采集

- 调用 `python cli.py --user {username} --org matrixorigin --since {since} --until {until}`
- 解析 JSON 输出

#### AI 总结

- 将每个 PR 标题用中文重新描述，通俗易懂
- 技术术语保留英文（如 S3、gRPC、MCP 等）
- 按 PR 维度逐个列出，标注仓库、PR 编号、状态、角色

#### 补充内容

- 默认不主动询问补充内容，直接输出周报
- 用户可通过对话更改偏好为"每次询问"，偏好记到记忆
- 用户随时可以主动补充（如"加上周三的需求评审会"）

#### 输出

- **聊天展示**：中文自然语言周报
- **企业微信文档**：通过文档 MCP 创建企业微信文档（需通道接通后可用）
- **企业微信智能表格**：通过表格 MCP 逐行写入（需通道接通后可用）
- **本地文件**：无企业微信通道时，保存为本地文件

输出方式由 agent 根据当前可用能力和用户指令决定。

### 3. GitHub Token 管理

采用组织级 token，两种方案：

- **GitHub App**（推荐）：安装在 matrixorigin 组织上，自动获得仓库读权限，token 自动轮转
- **Service Account PAT**：创建 bot 账号，生成 PAT，需手动续期

Token 作为环境变量 `GITHUB_TOKEN` 注入 NanoClaw 容器。

### 4. 企业微信接入

- 管理员在企业微信后台创建长连接模式的智能机器人
- NanoClaw 通过 `wecom` channel 连接 `wss://openws.work.weixin.qq.com`
- 每个用户单聊 = NanoClaw 的一个独立 group，记忆和文件系统隔离
- 文档 MCP 能力随长连接机器人自带

**注意**：wecom channel 是独立的 Feature Skill，不在本次 daily-report skill 的范围内。

## 数据流

```
用户: "周报"
  → agent 读记忆，获取 GitHub 用户名
  → agent 根据星期几推断日期范围
  → agent 执行: python cli.py --user aqqi666 --org matrixorigin --since 2026-03-17 --until 2026-03-21
  → CLI 返回 PR JSON 列表
  → agent 用中文重新描述每个 PR
  → agent 组织成周报格式输出
  → 根据用户指令/可用能力：聊天展示 / 创建企业微信文档 / 保存本地文件
```

## 范围界定

**本次做**：
- cli.py：GitHub PR 数据采集
- SKILL.md：NanoClaw Container Skill
- 本地验证：CLI 独立可用

**不做**：
- 企业微信 wecom channel（单独的 Feature Skill）
- 定时触发（纯对话触发）
- 发送给指定用户
- Commit / Issue / Code Review 等非 PR 数据
