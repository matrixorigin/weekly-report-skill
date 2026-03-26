"""GitHub 数据采集 CLI，采集 PR/Issue 数据并输出结构化 JSON。"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests


GITHUB_API = "https://api.github.com"
RATE_LIMIT_BUFFER = 5  # 剩余次数低于此值时主动等待
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".weekly-report")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
REQUIRED_FIELDS = ["token", "username", "role", "scopes"]


def load_config():
    """读取配置文件，返回字典。"""
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    """保存配置到文件。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def search_prs(user, scope, since, until, token, query_prefix="author"):
    """搜索 GitHub PR，返回列表或错误字典。

    scope: 搜索范围，如 "org:matrixorigin" 或 "repo:user/repo-name"
    query_prefix: "author" 或 "reviewed-by"
    token: 必需，调用者负责传入
    """
    if not token:
        return {"error": "auth_failed", "message": "未提供 GitHub token"}

    # until +1 天，转为左闭右开区间
    until_exclusive = (datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"{query_prefix}:{user} {scope} type:pr updated:{since}T00:00:00+08:00..{until_exclusive}T00:00:00+08:00"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    all_items = []
    page = 1

    while True:
        for attempt in range(3):
            resp = requests.get(
                f"{GITHUB_API}/search/issues",
                params={"q": query, "per_page": 100, "page": page, "sort": "updated", "order": "desc"},
                headers=headers,
            )

            # 缓存 JSON 解析结果，避免重复调用 + 防止非 JSON 响应崩溃
            try:
                body = resp.json()
            except Exception:
                body = {}
            msg = body.get("message", "")

            if resp.status_code == 200:
                # 主动检查 rate limit 余量
                remaining = int(resp.headers.get("X-RateLimit-Remaining", "99"))
                if remaining < RATE_LIMIT_BUFFER:
                    reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                    wait = max(1, reset_at - int(time.time()))
                    time.sleep(min(wait, 30))
                break
            elif resp.status_code in (401, 403) and "rate limit" not in msg.lower():
                return {"error": "auth_failed", "message": msg or "认证失败"}
            elif resp.status_code == 429 or (resp.status_code == 403 and "rate limit" in msg.lower()):
                reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(1, reset_at - int(time.time()))
                time.sleep(min(wait * (2 ** attempt), 60))
            else:
                return {"error": "github_unreachable", "message": f"GitHub API 返回 {resp.status_code}: {resp.text[:200]}"}
        else:
            return {"error": "github_unreachable", "message": "GitHub API 请求失败，已重试 3 次"}

        items = body.get("items", [])
        for item in items:
            # 从 repository_url 提取 owner/repo 全路径
            repo_url_parts = item["repository_url"].rsplit("/", 2)
            full_repo = f"{repo_url_parts[-2]}/{repo_url_parts[-1]}"
            all_items.append({
                "repo": full_repo,
                "pr_number": item["number"],
                "title": item["title"],
                "state": "merged" if item.get("pull_request", {}).get("merged_at") else item["state"],
                "role": [query_prefix.replace("-", "_")],  # "author" 或 "reviewed_by"
                "created_at": item["created_at"][:10],
                "merged_at": (item.get("pull_request", {}).get("merged_at") or "")[:10] or None,
                "url": item["html_url"],
                "body": clean_text(item.get("body")),
            })

        if len(items) < 100:
            break
        page += 1

    return all_items


def search_issues(user, scope, since, until, token):
    """搜索用户参与的 issue（创建、评论、被 assign、被 mention），返回列表或错误字典。

    scope: 搜索范围，如 "org:matrixorigin" 或 "repo:user/repo-name"
    """
    if not token:
        return {"error": "auth_failed", "message": "未提供 GitHub token"}

    until_exclusive = (datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"involves:{user} {scope} type:issue updated:{since}T00:00:00+08:00..{until_exclusive}T00:00:00+08:00"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    all_items = []
    page = 1

    while True:
        for attempt in range(3):
            resp = requests.get(
                f"{GITHUB_API}/search/issues",
                params={"q": query, "per_page": 100, "page": page, "sort": "updated", "order": "desc"},
                headers=headers,
            )

            try:
                body = resp.json()
            except Exception:
                body = {}
            msg = body.get("message", "")

            if resp.status_code == 200:
                remaining = int(resp.headers.get("X-RateLimit-Remaining", "99"))
                if remaining < RATE_LIMIT_BUFFER:
                    reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                    wait = max(1, reset_at - int(time.time()))
                    time.sleep(min(wait, 30))
                break
            elif resp.status_code in (401, 403) and "rate limit" not in msg.lower():
                return {"error": "auth_failed", "message": msg or "认证失败"}
            elif resp.status_code == 429 or (resp.status_code == 403 and "rate limit" in msg.lower()):
                reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(1, reset_at - int(time.time()))
                time.sleep(min(wait * (2 ** attempt), 60))
            else:
                return {"error": "github_unreachable", "message": f"GitHub API 返回 {resp.status_code}: {resp.text[:200]}"}
        else:
            return {"error": "github_unreachable", "message": "GitHub API 请求失败，已重试 3 次"}

        items = body.get("items", [])
        for item in items:
            repo_url_parts = item["repository_url"].rsplit("/", 2)
            full_repo = f"{repo_url_parts[-2]}/{repo_url_parts[-1]}"
            all_items.append({
                "type": "issue",
                "repo": full_repo,
                "issue_number": item["number"],
                "title": item["title"],
                "state": item["state"],
                "created_at": item["created_at"][:10],
                "updated_at": item["updated_at"][:10],
                "labels": [l["name"] for l in item.get("labels", [])],
                "assignees": [a["login"] for a in item.get("assignees", [])],
                "url": item["html_url"],
                "body": clean_text(item.get("body")),
                "comments_count": item.get("comments", 0),
            })

        if len(items) < 100:
            break
        page += 1

    return all_items


MAX_WORKERS = 10  # 并行请求数

# 匹配 HTML img 标签和 markdown 图片语法
_IMG_PATTERNS = re.compile(
    r'<img[^>]*>|!\[[^\]]*\]\([^)]+\)',
    re.IGNORECASE | re.DOTALL,
)


def clean_text(text):
    """去掉图片标签和多余空行。"""
    if not text:
        return ""
    text = _IMG_PATTERNS.sub("", text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def api_get(url, token):
    """通用 GitHub API GET 请求，带重试。"""
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429 or (resp.status_code == 403 and "rate limit" in resp.text.lower()):
                reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                time.sleep(max(1, min(reset_at - int(time.time()), 30)))
            else:
                return None
        except Exception:
            pass
    return None


def fetch_pr_details(pr, token):
    """并行获取单个 PR 的详情、reviews、comments。"""
    repo = pr["repo"]
    number = pr["pr_number"]

    # PR 详情（additions/deletions）
    detail = api_get(f"{GITHUB_API}/repos/{repo}/pulls/{number}", token)
    if detail:
        pr["additions"] = detail.get("additions", 0)
        pr["deletions"] = detail.get("deletions", 0)
        pr["changed_files"] = detail.get("changed_files", 0)

    # PR reviews
    reviews_data = api_get(f"{GITHUB_API}/repos/{repo}/pulls/{number}/reviews", token)
    if reviews_data:
        pr["reviews"] = [
            {"user": r["user"]["login"], "state": r["state"], "body": clean_text(r.get("body"))}
            for r in reviews_data
        ]

    # PR comments（review comments）
    comments_data = api_get(f"{GITHUB_API}/repos/{repo}/pulls/{number}/comments", token)
    if comments_data:
        pr["review_comments"] = [
            {"user": c["user"]["login"], "body": clean_text(c["body"]), "created_at": c["created_at"][:10]}
            for c in comments_data
        ]

    # Issue comments（PR 下的普通讨论）
    issue_comments = api_get(f"{GITHUB_API}/repos/{repo}/issues/{number}/comments", token)
    if issue_comments:
        pr["comments"] = [
            {"user": c["user"]["login"], "body": clean_text(c["body"]), "created_at": c["created_at"][:10]}
            for c in issue_comments
        ]

    return pr


def fetch_issue_comments(issue, token):
    """并行获取单个 Issue 的 comments。"""
    if issue.get("comments_count", 0) == 0:
        return issue

    repo = issue["repo"]
    number = issue["issue_number"]
    comments_data = api_get(f"{GITHUB_API}/repos/{repo}/issues/{number}/comments", token)
    if comments_data:
        issue["comments_detail"] = [
            {"user": c["user"]["login"], "body": clean_text(c["body"]), "created_at": c["created_at"][:10]}
            for c in comments_data
        ]

    return issue


def merge_and_dedupe(authored, reviewed):
    """合并 authored 和 reviewed 的 PR 列表，按 repo+pr_number 去重，合并角色。"""
    index = {}
    for pr in authored + reviewed:
        key = (pr["repo"], pr["pr_number"])
        if key in index:
            existing_roles = set(index[key]["role"])
            existing_roles.update(pr["role"])
            index[key]["role"] = sorted(existing_roles)
        else:
            index[key] = dict(pr)
    return sorted(index.values(), key=lambda x: x["created_at"], reverse=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="GitHub 数据采集 CLI")
    subparsers = parser.add_subparsers(dest="command")

    # config 子命令
    config_parser = subparsers.add_parser("config", help="管理用户配置")
    config_parser.add_argument("--set", nargs=2, action="append", metavar=("KEY", "VALUE"), help="设置配置项，如 --set token ghp_xxx")
    config_parser.add_argument("--get", action="store_true", help="输出当前配置（JSON）")

    # scopes 子命令
    scopes_parser = subparsers.add_parser("scopes", help="列出用户可用的组织和仓库")

    # fetch 子命令
    fetch_parser = subparsers.add_parser("fetch", help="采集 PR 和 Issue 数据")
    fetch_parser.add_argument("--since", required=True, help="开始日期（含），格式 YYYY-MM-DD")
    fetch_parser.add_argument("--until", required=True, help="结束日期（含），格式 YYYY-MM-DD")

    raw = argv if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    return args


def cmd_config(args):
    """处理 config 子命令。"""
    config = load_config()

    if args.set:
        for key, value in args.set:
            if key == "scopes":
                # scopes 存为列表，如 "org:matrixorigin,repo:user/repo"
                config["scopes"] = [s.strip() for s in value.split(",") if s.strip()]
            else:
                config[key] = value
        save_config(config)

    # 始终输出当前配置和缺失字段
    missing = [f for f in REQUIRED_FIELDS if not config.get(f)]
    result = {"config": config, "missing": missing, "config_file": CONFIG_FILE}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_scopes(args):
    """列出用户可访问的组织和仓库。"""
    config = load_config()
    token = config.get("token")
    if not token:
        json.dump({"error": "config_incomplete", "message": "请先配置 token"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    # 获取用户所属的组织
    orgs = []
    resp = requests.get(f"{GITHUB_API}/user/orgs", headers=headers, params={"per_page": 100})
    if resp.status_code == 200:
        orgs = [org["login"] for org in resp.json()]

    # 获取用户自己的仓库（非 fork，近一年有更新的）
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/user/repos",
            headers=headers,
            params={"per_page": 100, "page": page, "sort": "updated", "affiliation": "owner"},
        )
        if resp.status_code != 200:
            break
        items = resp.json()
        for r in items:
            if not r.get("fork"):
                repos.append(r["full_name"])
        if len(items) < 100:
            break
        page += 1

    result = {"orgs": orgs, "repos": repos[:30]}  # 仓库最多展示 30 个
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_fetch(args):
    """处理 fetch 子命令。"""
    config = load_config()

    # 检查必填字段
    missing = [f for f in REQUIRED_FIELDS if not config.get(f)]
    if missing:
        json.dump({
            "error": "config_incomplete",
            "missing": missing,
            "message": f"配置不完整，缺少: {', '.join(missing)}。请先运行 config --set 补全配置。",
            "config_file": CONFIG_FILE,
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    token = config["token"]
    user = config["username"]
    scopes = config["scopes"]

    # 第一阶段：并行搜索所有 scope 的 PR 和 Issue
    all_authored = []
    all_reviewed = []
    all_issues = []

    def search_scope(scope):
        authored = search_prs(user, scope, args.since, args.until, token, query_prefix="author")
        reviewed = search_prs(user, scope, args.since, args.until, token, query_prefix="reviewed-by")
        issues = search_issues(user, scope, args.since, args.until, token)
        return authored, reviewed, issues

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(search_scope, s): s for s in scopes}
        for future in as_completed(futures):
            authored, reviewed, issues = future.result()
            for result in (authored, reviewed, issues):
                if isinstance(result, dict) and "error" in result:
                    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
                    print()
                    sys.exit(1)
            all_authored.extend(authored)
            all_reviewed.extend(reviewed)
            all_issues.extend(issues)

    # 合并去重 PR
    prs = merge_and_dedupe(all_authored, all_reviewed)

    # Issue 去重
    seen_issues = {}
    for issue in all_issues:
        key = (issue["repo"], issue["issue_number"])
        if key not in seen_issues:
            seen_issues[key] = issue
    unique_issues = sorted(seen_issues.values(), key=lambda x: x["updated_at"], reverse=True)

    # 第二阶段：并行获取 PR 详情和 Issue comments
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pr_futures = [pool.submit(fetch_pr_details, pr, token) for pr in prs]
        issue_futures = [pool.submit(fetch_issue_comments, issue, token) for issue in unique_issues]
        for f in as_completed(pr_futures + issue_futures):
            f.result()

    # 输出结果到文件
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"prs": prs, "issues": unique_issues}, f, ensure_ascii=False, indent=2)

    summary = {
        "status": "ok",
        "output_file": output_path,
        "pr_count": len(prs),
        "issue_count": len(unique_issues),
        "role": config.get("role", ""),
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print()


def main(argv=None):
    args = parse_args(argv)
    if args.command == "config":
        cmd_config(args)
    elif args.command == "scopes":
        cmd_scopes(args)
    elif args.command == "fetch":
        cmd_fetch(args)


if __name__ == "__main__":
    main()
