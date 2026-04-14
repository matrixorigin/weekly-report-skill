# 周报 Skill 部署指南

面向"为全公司部署一个共享周报 bot"的场景。个人本机使用见 `USER_GUIDE.md`。

## 部署时间线（严格顺序）

### 🔴 阶段 1：前置准备（可并行，必须全部完成才能进阶段 2）

#### 1a. GitHub 管理员

- 建一个机器账号（推荐命名 `{公司}-weekly-bot`），或挑一个给周报用的高权限账号
  - 避免用真人账号（离职失效）
- 机器账号加进所有要覆盖的 org，Member 级别即可
- 生成 fine-grained PAT（Personal Access Token）：
  - Scopes：`repo:read` + `org:read` + `team:read`
  - Expiration 按公司安全要求
- **把 token 交给运维**，阶段 3 配置时用

#### 1b. HR（企微通讯录治理，工作量最大）

**工作一：员工"别名"字段填 GitHub 账号**

对每个需要用 skill 的员工（leader + 写代码/写周报的员工）：

1. 企微管理后台 → 通讯录 → 选中成员 → 编辑
2. "别名" 字段填入他的 **GitHub 用户名**（login，不是昵称）

**做法建议**：HR 发一张表让大家自己填报 GitHub 账号，HR 批量录入。

**工作二：每个部门设"部门负责人"**

对**每个要出周报的部门**（至少覆盖汇报深度 N 级，默认 3）：

1. 企微管理后台 → 通讯录 → 组织架构 → 选中部门 → 编辑
2. "部门负责人" 字段填入负责人的 userid（或姓名选择）

**矩阵起源当前状态示例**（已确认部分）：

| 部门 | 负责人 | 状态 |
|---|---|---|
| 研发 | 田丰 | ✅ |
| 产品 | 邓楠 | ✅ |
| 生态及市场运营 | 李慧静 | ✅ |
| 人事行政 | 李慧静 | ✅ |
| 财务管理 | 郑岩 | ✅ |
| 数据平台 | 田丰 | ✅ |
| AI平台 | 赵晨阳 | ✅ |
| 引擎开发-存储/计算 | 徐鹏 | ✅ |
| 平台开发 | 谢泽雄 | ✅ |
| 前端开发 | 郭朋飞 | ✅ |
| 后端开发 | 彭振 | ✅ |
| 应用开发 | （空）| ❌ 建议填张旭 |
| MOI-测试 | （空）| ❌ 建议填张旭 |
| 项目交付与支持 | （空）| ❌ 建议填刘博 |
| 海外业务拓展 | （空）| ❌ 待确认负责人 |

#### 1c. 企微管理员

确认自建应用（例如 matrixclaw）的：

- **可信 IP**：包含 skill 调 WeCom API 的出口 IP（如跳板机 IP）
- **权限**：通讯录读取
- **可见范围**：全公司（不然拉不全组织架构）

### 🟡 阶段 2：部署基础设施

由运维负责：

1. 部署 claw 后端（接入企微 bot）
2. 在 claw 机器上 clone 并安装 skill：
   ```bash
   git clone https://github.com/{org}/weekly-report-skill-origin.git
   cd weekly-report-skill-origin
   pip install -r requirements.txt
   ```

**注意：bot 暂时不要加进目标周报群**，用测试群或私聊 bot 来做后续联调，避免未完成版本出现在生产环境。

### 🟢 阶段 3：首次配置（对话式）

任何人在 claw 里私聊 bot 或在测试群 @ bot，说"**初始化周报 skill**"。Claude 按 `SKILL.md` 流程引导：

1. 问 **GitHub token** → 粘机器账号 PAT
   - skill 自动调 `/user` 反查 GitHub login，同时填入 `username`，不再单独问
2. 问 **scopes** → `cli.py scopes` 自动列出候选 → 确认用 `org:matrixorigin`（或加其他 org）
3. 问 **企微凭据**三项（一次性）：
   - `wecom_corpid`
   - `wecom_secret`
   - `wecom_proxy`（格式 `user:password@host`）
4. 跑 `wecom-sync` → 拉到阶段 1 治理好的干净数据
5. 首次触发批量时问 **report_depth** → 回 `3`

**role 字段**不再强制：有企微凭据时自动从 whoami 的 `position` 字段动态取。

### 🔵 阶段 4：内部联调（测试群 / 私聊）

**不要**在王龙的周报群测。用测试群或直接私聊 bot，依次验证：

| 测试用例 | 输入 | 期望 |
|---|---|---|
| 身份识别 | `@bot 我是谁` | 返回姓名、部门、是否 leader |
| 显式团队 | `@bot 产品部周报` | 生成一份产品部团队周报 |
| Leader 触发 | 某 leader @ bot 说"我们部门的周报" | 自动识别其部门，生成对应团队周报 |
| 批量模式 | `@bot 生成所有部门周报` | 按 report_depth=3 遍历，依次发出 13 份 |
| 管理指令 | `@bot 刷新企微` | 调 wecom-sync，返回摘要 |

**全部通过**才能进阶段 5。

### 🟣 阶段 5：正式上线

1. 群管理员把 bot 加进王龙的周报群
2. 通知各 leader：以后在周报群 @ 这个 bot 就行
3. 王龙第一次看到的就是能用的版本

## 责任 & 依赖总表

| 阶段 | 负责人 | 动作 | 前置依赖 |
|---|---|---|---|
| 1a | GitHub admin | 机器账号 + PAT | — |
| 1b | HR | 别名 + 部门负责人 | — |
| 1c | 企微管理员 | matrixclaw 应用权限 / IP / 可见范围 | — |
| 2 | 运维 | 部署 claw + 装 skill | — |
| 3 | 运维（或任何人） | 对话式配置 + wecom-sync | 阶段 1 + 阶段 2 |
| 4 | 运维 | 测试群 / 私聊联调 | 阶段 3 |
| 5 | 群管理员 | bot 进王龙群 + 通知 leader | **阶段 4 全部通过** |

## 阶段 1 未做完就进阶段 3 会怎样

- github_login 空 → whoami 报 `wecom_member_not_found`，该用户无法使用
- department_leader 空 → 该部门 leader 被识别为非 leader，说"周报"走个人分支
- **补救**：HR 补完后在 claw 说"@bot 刷新企微"重跑 wecom-sync，数据立即更新

**但最好不要指望补救，一次到位治理最省事**。

## 维护动作（上线后）

### 组织变动（入职 / 离职 / 转岗 / 升职）

触发方式有三种，任选：

- **对话触发**（推荐给日常用户）：群里 `@bot 刷新企微`
- **管理员手动**：`python cli.py wecom-sync`
- **定时任务**：claw 机器上加 cron，每天凌晨 3 点自动刷新：
  ```
  0 3 * * * cd /opt/weekly-report && python cli.py wecom-sync
  ```

### HR 没及时补 department_leader 的临时补丁

skill 管理员可以在 claw 本地加 override：

```bash
python cli.py leader-override --set {userid} {dept_id_1},{dept_id_2}
python cli.py leader-override --list
python cli.py leader-override --unset {userid}
```

HR 补完企微数据后移除 override。

### 改汇报层级

说 "@bot 改成 N 级汇报" 或跑 `config --set report_depth N`。

## 故障排查

| 现象 | 可能原因 | 排查 |
|---|---|---|
| whoami 报 `wecom_not_synced` | 从未跑过 wecom-sync | `cli.py wecom-sync` |
| whoami 报 `wecom_member_not_found` | 企微别名字段为空，或拼错 | HR 补齐后 `@bot 刷新企微` |
| 团队周报某人数据缺失 | 该成员 GitHub login 未映射 | 对比 wecom.json 里成员的 `github_login` 是否正确 |
| 部门 leader 没被识别 | 企微 department_leader 字段空 | HR 补齐，或用 leader-override 兜底 |
| 跑 wecom-sync 报 60020 | IP 白名单限制 | 企微管理员把跳板机 IP 加进 matrixclaw 应用的可信 IP |
| GitHub 搜索返回 401 | token 过期 | 重新生成 PAT，`@bot 换个 token` |

## 配置文件位置

- 配置：`~/.weekly-report/config.json`
- 企微数据缓存：`~/.weekly-report/wecom.json`
- 团队周报缓存：`~/.weekly-report/cache/dept_{id}_{since}_{until}.json`（24h TTL）
- skill 代码目录：clone 时选择的路径
