# weekly-report-skill

周报生成 Skill，支持各类 Claw 容器（NanoClaw、OpenClaw、QClaw 等）及 Claude Code。从 GitHub 采集 PR 和 Issue 数据，自动生成给 leader 看的工作周报。

## 功能

- 采集指定用户在组织下的 PR（authored + reviewed）和 Issue（involved）
- PR 自动关联 Issue（通过 body 中的 `fixes #xxx` / `closes #xxx`）
- 已合并 PR 附带代码变更统计（additions / deletions / changed_files）
- 按业务主题自动分类，生成结构化周报
- 支持对话式补充非 GitHub 内容（会议、评审等）

## 安装

把下面这句话发给你使用的 AI 工具，它会自动完成安装：

> 帮我安装这个 skill：https://github.com/matrixorigin/weekly-report-skill

安装完成后说"周报"即可开始使用。详细使用说明见 [用户手册](USER_GUIDE.md)。

## 文件说明

| 文件 | 说明 |
|------|------|
| `SKILL.md` | Skill 定义，描述 agent 的行为指令 |
| `cli.py` | GitHub 数据采集 CLI，输出 JSON |
| `USER_GUIDE.md` | 用户使用手册 |
| `requirements.txt` | Python 依赖（requests） |
