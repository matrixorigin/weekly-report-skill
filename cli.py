"""GitHub PR 数据采集 CLI，输出结构化 JSON。"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests


GITHUB_API = "https://api.github.com"
RATE_LIMIT_BUFFER = 5  # 剩余次数低于此值时主动等待


def search_prs(user, org, since, until, token, query_prefix="author"):
    """搜索 GitHub PR，返回列表或错误字典。

    query_prefix: "author" 或 "reviewed-by"
    token: 必需，调用者负责传入
    """
    if not token:
        return {"error": "auth_failed", "message": "未提供 GitHub token"}

    # until +1 天，转为左闭右开区间
    until_exclusive = (datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"{query_prefix}:{user} org:{org} type:pr updated:{since}T00:00:00+08:00..{until_exclusive}T00:00:00+08:00"
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
            repo_name = item["repository_url"].rsplit("/", 1)[-1]
            all_items.append({
                "repo": repo_name,
                "pr_number": item["number"],
                "title": item["title"],
                "state": "merged" if item.get("pull_request", {}).get("merged_at") else item["state"],
                "role": [query_prefix.replace("-", "_")],  # "author" 或 "reviewed_by"
                "created_at": item["created_at"][:10],
                "merged_at": (item.get("pull_request", {}).get("merged_at") or "")[:10] or None,
                "url": item["html_url"],
            })

        if len(items) < 100:
            break
        page += 1

    return all_items


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
    parser = argparse.ArgumentParser(description="采集 GitHub PR 数据，输出 JSON")
    parser.add_argument("--user", required=True, help="GitHub 用户名")
    parser.add_argument("--org", required=True, help="GitHub 组织名")
    parser.add_argument("--since", required=True, help="开始日期（含），格式 YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="结束日期（含），格式 YYYY-MM-DD")
    parser.add_argument("--token", default=None, help="GitHub token，默认读取 GITHUB_TOKEN 环境变量")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # TODO: 实现数据采集
    json.dump([], sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
