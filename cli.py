"""GitHub 数据采集 CLI，采集 PR/Issue 数据并输出结构化 JSON。支持企微组织架构对接。"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests


GITHUB_API = "https://api.github.com"
WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
RATE_LIMIT_BUFFER = 5  # 剩余次数低于此值时主动等待
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".weekly-report")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
WECOM_FILE = os.path.join(CONFIG_DIR, "wecom.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
CACHE_TTL_SECONDS = 24 * 3600  # 24 小时
REQUIRED_FIELDS = ["token", "username", "role", "scopes"]


def compute_missing(config):
    """计算缺失的配置项。

    规则：
    - token / username / scopes 必填
    - role：如果同时配了 wecom_corpid + wecom_secret，说明部署时能从企微 whoami 拿到
      position，role 不再强制要求（即使本地也有 config.role 兜底）；
      否则（纯本地无企微场景）仍必填。
    """
    base_required = ["token", "username", "scopes"]
    missing = [f for f in base_required if not config.get(f)]
    has_wecom = bool(config.get("wecom_corpid") and config.get("wecom_secret"))
    if not has_wecom and not config.get("role"):
        missing.append("role")
    return missing


def cache_path(dept_id, since, until):
    """按部门 + 日期范围的缓存文件路径。"""
    return os.path.join(CACHE_DIR, f"dept_{dept_id}_{since}_{until}.json")


def cache_load(dept_id, since, until):
    """读取缓存。过期或不存在返回 None。"""
    path = cache_path(dept_id, since, until)
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    if time.time() - mtime > CACHE_TTL_SECONDS:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_save(dept_id, since, until, data):
    """保存缓存。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(dept_id, since, until), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cache_clear():
    """清空缓存（wecom-sync 后调用，因为人员可能变了）。"""
    if os.path.isdir(CACHE_DIR):
        for fn in os.listdir(CACHE_DIR):
            if fn.startswith("dept_") and fn.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, fn))
                except Exception:
                    pass


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


# ── 企微 API 相关 ──────────────────────────────────────────────

def wecom_api_call(path, params=None, proxy=None):
    """调用企微 API。

    如果配置了 proxy（SSH 代理机），通过 sshpass+ssh+curl 远程调用；
    否则直接本地 requests 调用。

    path: API 路径，如 "/department/list"
    params: URL 查询参数 dict
    proxy: {"host": "...", "user": "...", "password": "..."} 或 None
    返回: 解析后的 JSON dict，或 {"error": ..., "message": ...}
    """
    url = f"{WECOM_API}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    if proxy:
        return _wecom_via_ssh(url, proxy)
    else:
        return _wecom_direct(url)


def _wecom_direct(url):
    """直接本地调用企微 API。"""
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("errcode", 0) != 0:
            return {"error": "wecom_api_error", "message": f"errcode={data['errcode']}, errmsg={data.get('errmsg', '')}"}
        return data
    except Exception as e:
        return {"error": "wecom_unreachable", "message": str(e)}


def _wecom_via_ssh(url, proxy):
    """通过 SSH 代理机远程 curl 调用企微 API。"""
    host = proxy["host"]
    user = proxy.get("user", "root")
    password = proxy.get("password", "")

    cmd = [
        "sshpass", "-p", password,
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        f"curl -s '{url}'"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"error": "ssh_failed", "message": f"SSH 连接失败: {result.stderr.strip()}"}
        data = json.loads(result.stdout)
        if data.get("errcode", 0) != 0:
            return {"error": "wecom_api_error", "message": f"errcode={data['errcode']}, errmsg={data.get('errmsg', '')}"}
        return data
    except subprocess.TimeoutExpired:
        return {"error": "ssh_timeout", "message": "SSH 连接超时"}
    except json.JSONDecodeError:
        return {"error": "wecom_parse_error", "message": f"无法解析响应: {result.stdout[:200]}"}
    except FileNotFoundError:
        return {"error": "sshpass_missing", "message": "未安装 sshpass。macOS: brew install sshpass 或 brew install hudochenkov/sshpass/sshpass"}
    except Exception as e:
        return {"error": "wecom_unreachable", "message": str(e)}


def wecom_get_token(corpid, secret, proxy=None):
    """获取企微 access_token。gettoken 不受 IP 白名单限制，始终直连。"""
    url = f"{WECOM_API}/gettoken?corpid={corpid}&corpsecret={secret}"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("errcode", 0) != 0:
            return {"error": "wecom_auth_failed", "message": f"获取 token 失败: {data.get('errmsg', '')}"}
        return data["access_token"]
    except Exception as e:
        return {"error": "wecom_unreachable", "message": str(e)}


def wecom_get_departments(access_token, proxy=None):
    """获取企微完整部门树。"""
    return wecom_api_call("/department/list", {"access_token": access_token}, proxy=proxy)


def wecom_get_department_users(access_token, department_id, proxy=None):
    """获取指定部门的成员详情（含别名 alias）。"""
    return wecom_api_call("/user/list", {
        "access_token": access_token,
        "department_id": str(department_id),
        "fetch_child": "0",  # 不递归，逐层拉
    }, proxy=proxy)


def wecom_get_department_users_recursive(access_token, department_id, proxy=None):
    """递归获取部门及子部门的所有成员。"""
    return wecom_api_call("/user/list", {
        "access_token": access_token,
        "department_id": str(department_id),
        "fetch_child": "1",
    }, proxy=proxy)


def build_department_tree(departments):
    """将扁平部门列表构建为树结构。"""
    by_id = {d["id"]: {**d, "children": []} for d in departments}
    roots = []
    for d in departments:
        node = by_id[d["id"]]
        pid = d.get("parentid", 0)
        if pid == 0 or pid not in by_id:
            roots.append(node)
        else:
            by_id[pid]["children"].append(node)
    return roots


def flatten_tree_paths(nodes, prefix=""):
    """将部门树展平为 id → 全路径名 的映射。"""
    result = {}
    for node in nodes:
        path = f"{prefix}/{node['name']}" if prefix else node["name"]
        result[node["id"]] = path
        result.update(flatten_tree_paths(node["children"], path))
    return result


def load_wecom_data():
    """读取 wecom.json。"""
    if not os.path.exists(WECOM_FILE):
        return None
    with open(WECOM_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_wecom_data(data):
    """保存 wecom.json。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(WECOM_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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

    # team-discover 子命令
    subparsers.add_parser("team-discover", help="自动发现用户所在的 GitHub teams")

    # fetch-team 子命令
    fetch_team_parser = subparsers.add_parser("fetch-team", help="采集整个团队的 PR 和 Issue 数据")
    fetch_team_parser.add_argument("--since", required=True, help="开始日期（含），格式 YYYY-MM-DD")
    fetch_team_parser.add_argument("--until", required=True, help="结束日期（含），格式 YYYY-MM-DD")
    fetch_team_parser.add_argument("--source", choices=["github", "wecom"], default=None,
                                   help="团队成员来源：github（GitHub team）或 wecom（企微部门）。默认自动检测。")
    fetch_team_parser.add_argument("--department-id", type=int, default=None,
                                   help="企微部门 ID。不传则用 config.wecom_department。")
    fetch_team_parser.add_argument("--refresh", action="store_true",
                                   help="强制重拉，忽略缓存。")

    # wecom-sync 子命令
    subparsers.add_parser("wecom-sync", help="同步企微组织架构：拉取部门树和成员别名映射")

    # wecom-team 子命令
    wecom_team_parser = subparsers.add_parser("wecom-team", help="列出企微部门，供用户选择团队")
    wecom_team_parser.add_argument("--department-id", type=int, default=None, help="指定部门 ID 查看子部门")

    # wecom-set-team 子命令
    wecom_set_team_parser = subparsers.add_parser("wecom-set-team", help="设置企微部门作为团队")
    wecom_set_team_parser.add_argument("--department-id", type=int, required=True, help="企微部门 ID")

    # whoami 子命令
    whoami_parser = subparsers.add_parser("whoami", help="识别当前用户身份。优先使用显式 userid，其次环境变量，最后 token 反查")
    whoami_parser.add_argument("--wecom-userid", help="显式传入企微 userid（例如 claw 平台从消息元数据 sender_id 拿到的值）")
    whoami_parser.add_argument("--github-login", help="显式传入 GitHub login（跳过 token 反查）")

    # list-report-targets 子命令：按深度枚举应出周报的部门（用于批量自动模式）
    lrt_parser = subparsers.add_parser("list-report-targets",
                                        help="列出自动批量模式下应生成周报的部门清单（按深度过滤）")
    lrt_parser.add_argument("--max-depth", type=int, default=None,
                             help="最大部门深度（根部门=0，一级=1，二级=2...）。不传则读 config.report_depth")
    lrt_parser.add_argument("--only-with-leader", action="store_true",
                             help="只列出有 department_leader（或 leader_overrides）的部门")
    lrt_parser.add_argument("--only-with-members", action="store_true",
                             help="只列出至少有 1 个成员的部门（过滤空部门）")

    # leader-override 子命令：补企微 department_leader 字段未维护的汇报关系
    lo_parser = subparsers.add_parser("leader-override", help="维护本地 leader 映射表，补企微 department_leader 未配置的情况")
    lo_group = lo_parser.add_mutually_exclusive_group(required=True)
    lo_group.add_argument("--set", nargs=2, metavar=("USERID", "DEPT_IDS"),
                          help='声明 USERID 是哪些部门的 leader，DEPT_IDS 为逗号分隔的部门 ID，如 --set zx 63,69')
    lo_group.add_argument("--unset", metavar="USERID", help="移除某人的 override")
    lo_group.add_argument("--list", action="store_true", help="列出所有 override")

    raw = argv if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    return args


def cmd_config(args):
    """处理 config 子命令。"""
    config = load_config()

    auto_username_filled = None

    if args.set:
        for key, value in args.set:
            if key == "token":
                config["token"] = value
                # 自动反查 GitHub login 填 username，不用再问用户
                try:
                    resp = requests.get(f"{GITHUB_API}/user",
                                         headers={"Authorization": f"token {value}", "Accept": "application/vnd.github.v3+json"},
                                         timeout=10)
                    if resp.status_code == 200:
                        login = resp.json().get("login", "")
                        if login:
                            config["username"] = login
                            auto_username_filled = login
                except Exception:
                    pass  # 失败不阻塞，后续用户可以手动 set
            elif key == "scopes":
                # scopes 存为列表，如 "org:matrixorigin,repo:user/repo"
                config["scopes"] = [s.strip() for s in value.split(",") if s.strip()]
            elif key == "team":
                # team 格式："org/slug"，存为 dict
                if "/" not in value:
                    json.dump({"error": "invalid_team", "message": "team 必须为 org/slug 格式"}, sys.stdout, ensure_ascii=False, indent=2)
                    print()
                    sys.exit(1)
                org, slug = value.split("/", 1)
                config["team"] = {"org": org.strip(), "slug": slug.strip()}
            elif key == "wecom_corpid":
                config["wecom_corpid"] = value
            elif key == "wecom_secret":
                config["wecom_secret"] = value
            elif key == "wecom_proxy":
                # 格式: "user:password@host" 或 "host"（默认 root，无密码）
                if "@" in value:
                    userpass, host = value.rsplit("@", 1)
                    if ":" in userpass:
                        user, password = userpass.split(":", 1)
                    else:
                        user, password = userpass, ""
                else:
                    host, user, password = value, "root", ""
                config["wecom_proxy"] = {"host": host, "user": user, "password": password}
            elif key == "wecom_department":
                # 设置企微部门作为团队来源，值为部门 ID
                config["wecom_department"] = int(value)
            elif key == "report_depth":
                config["report_depth"] = int(value)
            else:
                config[key] = value
        save_config(config)

    # 始终输出当前配置和缺失字段
    missing = compute_missing(config)
    result = {"config": config, "missing": missing, "config_file": CONFIG_FILE}
    if auto_username_filled:
        result["auto_username_filled"] = auto_username_filled
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
    missing = compute_missing(config)
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


def cmd_team_discover(args):
    """自动发现用户所在的 GitHub teams。

    优先级：
    1. /user/teams 拿当前用户直接归属的 teams
    2. 若为空，枚举用户所在 orgs 的 teams（/orgs/{org}/teams），便于用户选择
    """
    config = load_config()
    token = config.get("token")
    if not token:
        json.dump({"error": "config_incomplete", "message": "请先配置 token"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    # 1. /user/teams
    my_teams = []
    page = 1
    while True:
        resp = requests.get(f"{GITHUB_API}/user/teams", headers=headers, params={"per_page": 100, "page": page})
        if resp.status_code == 401 or resp.status_code == 403:
            try:
                msg = resp.json().get("message", "")
            except Exception:
                msg = ""
            if "scope" in msg.lower() or "read:org" in msg.lower() or resp.status_code == 403:
                json.dump({
                    "error": "insufficient_scope",
                    "message": "token 缺少 read:org 权限。请到 https://github.com/settings/tokens 勾选 read:org 后重新生成 token。",
                }, sys.stdout, ensure_ascii=False, indent=2)
                print()
                sys.exit(1)
            json.dump({"error": "auth_failed", "message": msg or "认证失败"}, sys.stdout, ensure_ascii=False, indent=2)
            print()
            sys.exit(1)
        if resp.status_code != 200:
            break
        items = resp.json()
        for t in items:
            org_login = (t.get("organization") or {}).get("login", "")
            my_teams.append({
                "org": org_login,
                "slug": t.get("slug", ""),
                "name": t.get("name", ""),
                "members_count": t.get("members_count"),
                "source": "user_teams",
            })
        if len(items) < 100:
            break
        page += 1

    result = {"source": "user_teams", "teams": my_teams}

    # 2. 若 /user/teams 为空，退回枚举 orgs 里的 teams 让用户挑
    if not my_teams:
        org_resp = requests.get(f"{GITHUB_API}/user/orgs", headers=headers, params={"per_page": 100})
        orgs = [o["login"] for o in org_resp.json()] if org_resp.status_code == 200 else []

        all_org_teams = []
        for org in orgs:
            page = 1
            while True:
                resp = requests.get(
                    f"{GITHUB_API}/orgs/{org}/teams",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                if resp.status_code != 200:
                    break
                items = resp.json()
                for t in items:
                    all_org_teams.append({
                        "org": org,
                        "slug": t.get("slug", ""),
                        "name": t.get("name", ""),
                        "members_count": None,
                        "source": "org_teams",
                    })
                if len(items) < 100:
                    break
                page += 1

        result = {"source": "org_teams", "teams": all_org_teams}

    # 为已发现的每个 team 补一份 members 预览（限制前 20 条，避免爆炸）
    preview_limit = 10
    for t in result["teams"][:preview_limit]:
        resp = requests.get(
            f"{GITHUB_API}/orgs/{t['org']}/teams/{t['slug']}/members",
            headers=headers,
            params={"per_page": 100},
        )
        if resp.status_code == 200:
            t["members_preview"] = [m["login"] for m in resp.json()]

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def fetch_team_members(org, slug, token):
    """拉取指定 GitHub team 的成员列表（login 列表）。"""
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    members = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/orgs/{org}/teams/{slug}/members",
            headers=headers,
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200:
            return {"error": "team_fetch_failed", "message": f"拉取团队成员失败: {resp.status_code} {resp.text[:200]}"}
        items = resp.json()
        members.extend([m["login"] for m in items])
        if len(items) < 100:
            break
        page += 1
    return members


def _resolve_wecom_members(config, department_id):
    """从企微数据解析团队成员的 GitHub 账号列表。

    返回 (github_logins: list[str], team_info: dict) 或 (error_dict, None)。
    """
    wecom_data = load_wecom_data()
    if not wecom_data:
        return {"error": "wecom_not_synced", "message": "企微数据未同步，请先运行 wecom-sync"}, None

    dept_id = department_id or config.get("wecom_department")
    if not dept_id:
        return {"error": "no_wecom_department", "message": "未设置企微部门，请先运行 wecom-set-team"}, None

    # 从 wecom.json 的 members 中筛选属于该部门（含子部门）的成员
    all_dept_ids = _get_sub_department_ids(wecom_data.get("departments", []), dept_id)
    all_dept_ids.add(dept_id)

    github_logins = []
    member_details = []
    for m in wecom_data.get("members", []):
        # 成员可能属于多个部门
        member_depts = set(m.get("department", []))
        if member_depts & all_dept_ids:
            gh = m.get("github_login", "")
            if gh:
                github_logins.append(gh)
                member_details.append({"name": m.get("name", ""), "github": gh})
            else:
                member_details.append({"name": m.get("name", ""), "github": None})

    dept_name = ""
    for d in wecom_data.get("departments", []):
        if d["id"] == dept_id:
            dept_name = d.get("name", "")
            break

    team_info = {
        "source": "wecom",
        "department_id": dept_id,
        "department_name": dept_name,
        "total_members": len(member_details),
        "mapped_members": len(github_logins),
        "unmapped_members": [m["name"] for m in member_details if not m["github"]],
    }

    return github_logins, team_info


def _get_sub_department_ids(departments, parent_id):
    """递归获取所有子部门 ID。"""
    children = set()
    for d in departments:
        if d.get("parentid") == parent_id:
            children.add(d["id"])
            children.update(_get_sub_department_ids(departments, d["id"]))
    return children


def cmd_wecom_sync(args):
    """同步企微组织架构：拉取部门树 + 成员（含别名/GitHub 映射）。"""
    config = load_config()
    corpid = config.get("wecom_corpid")
    secret = config.get("wecom_secret")

    if not corpid or not secret:
        json.dump({
            "error": "wecom_config_incomplete",
            "message": "请先配置企微凭据：config --set wecom_corpid xxx --set wecom_secret xxx",
            "missing": [k for k in ("wecom_corpid", "wecom_secret") if not config.get(k)],
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    proxy = config.get("wecom_proxy")

    # 1. 获取 access_token（不受 IP 白名单限制，直连）
    token_result = wecom_get_token(corpid, secret)
    if isinstance(token_result, dict) and "error" in token_result:
        json.dump(token_result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)
    access_token = token_result

    # 2. 拉取部门列表（可能需要代理）
    dept_result = wecom_get_departments(access_token, proxy=proxy)
    if "error" in dept_result:
        # 如果直连失败且没配代理，提示配置代理
        if not proxy and dept_result.get("message", "").find("60020") >= 0:
            json.dump({
                "error": "wecom_ip_whitelist",
                "message": "企微 API 受 IP 白名单限制。请配置代理机：config --set wecom_proxy user:password@host",
                "detail": dept_result["message"],
            }, sys.stdout, ensure_ascii=False, indent=2)
            print()
            sys.exit(1)
        json.dump(dept_result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    departments = dept_result.get("department", [])

    # 3. 逐部门拉取成员详情（含 alias）
    all_members = {}  # userid → member dict
    for dept in departments:
        users_result = wecom_get_department_users(access_token, dept["id"], proxy=proxy)
        if "error" in users_result:
            # 某个部门拉取失败不中断，记录错误继续
            continue
        for u in users_result.get("userlist", []):
            uid = u.get("userid", "")
            if uid and uid not in all_members:
                all_members[uid] = {
                    "userid": uid,
                    "name": u.get("name", ""),
                    "alias": u.get("alias", ""),
                    "department": u.get("department", []),
                    "position": u.get("position", ""),
                    "email": u.get("email", ""),
                    "github_login": u.get("alias", ""),  # 约定：别名字段存 GitHub login
                }

    # 4. 保存
    wecom_data = {
        "synced_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "corpid": corpid,
        "departments": departments,
        "department_tree": build_department_tree(departments),
        "members": list(all_members.values()),
        "member_count": len(all_members),
    }
    save_wecom_data(wecom_data)

    # 人员可能变了，清掉所有团队数据缓存
    cache_clear()

    # 5. 统计
    mapped = sum(1 for m in all_members.values() if m.get("github_login"))
    summary = {
        "status": "ok",
        "wecom_file": WECOM_FILE,
        "department_count": len(departments),
        "member_count": len(all_members),
        "github_mapped": mapped,
        "github_unmapped": len(all_members) - mapped,
        "message": f"同步完成。{len(departments)} 个部门，{len(all_members)} 名成员，其中 {mapped} 人有 GitHub 映射。",
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_wecom_team(args):
    """列出企微部门，供用户选择作为团队。"""
    wecom_data = load_wecom_data()
    if not wecom_data:
        json.dump({"error": "wecom_not_synced", "message": "企微数据未同步，请先运行 wecom-sync"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    departments = wecom_data.get("departments", [])
    members = wecom_data.get("members", [])
    dept_id = getattr(args, "department_id", None)

    # 构建部门路径映射
    tree = build_department_tree(departments)
    paths = flatten_tree_paths(tree)

    # 统计每个部门的直属成员数
    dept_member_count = {}
    for m in members:
        for did in m.get("department", []):
            dept_member_count[did] = dept_member_count.get(did, 0) + 1

    if dept_id is not None:
        # 显示指定部门的子部门和成员
        children = [d for d in departments if d.get("parentid") == dept_id]
        dept_members = [m for m in members if dept_id in m.get("department", [])]
        result = {
            "department_id": dept_id,
            "department_path": paths.get(dept_id, ""),
            "children": [
                {"id": c["id"], "name": c["name"], "path": paths.get(c["id"], ""), "member_count": dept_member_count.get(c["id"], 0)}
                for c in sorted(children, key=lambda x: x.get("order", 0))
            ],
            "members": [
                {"name": m["name"], "github_login": m.get("github_login", ""), "position": m.get("position", "")}
                for m in dept_members
            ],
        }
    else:
        # 显示顶层部门
        top_depts = [d for d in departments if d.get("parentid", 0) == 0 or d.get("parentid") not in {dd["id"] for dd in departments}]
        # 如果顶层只有一个根节点，展开一层
        if len(top_depts) == 1:
            root_id = top_depts[0]["id"]
            children = [d for d in departments if d.get("parentid") == root_id]
            result = {
                "root": {"id": root_id, "name": top_depts[0]["name"]},
                "departments": [
                    {"id": c["id"], "name": c["name"], "path": paths.get(c["id"], ""), "member_count": dept_member_count.get(c["id"], 0)}
                    for c in sorted(children, key=lambda x: x.get("order", 0))
                ],
            }
        else:
            result = {
                "departments": [
                    {"id": d["id"], "name": d["name"], "path": paths.get(d["id"], ""), "member_count": dept_member_count.get(d["id"], 0)}
                    for d in sorted(top_depts, key=lambda x: x.get("order", 0))
                ],
            }

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_wecom_set_team(args):
    """设置企微部门作为团队来源。"""
    wecom_data = load_wecom_data()
    if not wecom_data:
        json.dump({"error": "wecom_not_synced", "message": "企微数据未同步，请先运行 wecom-sync"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    dept_id = args.department_id
    departments = wecom_data.get("departments", [])
    members = wecom_data.get("members", [])

    # 验证部门存在
    dept = next((d for d in departments if d["id"] == dept_id), None)
    if not dept:
        json.dump({"error": "department_not_found", "message": f"部门 ID {dept_id} 不存在"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    # 保存到配置
    config = load_config()
    config["wecom_department"] = dept_id
    save_config(config)

    # 统计该部门（含子部门）的成员
    all_dept_ids = _get_sub_department_ids(departments, dept_id)
    all_dept_ids.add(dept_id)

    dept_members = [m for m in members if set(m.get("department", [])) & all_dept_ids]
    mapped = [m for m in dept_members if m.get("github_login")]
    unmapped = [m for m in dept_members if not m.get("github_login")]

    tree = build_department_tree(departments)
    paths = flatten_tree_paths(tree)

    result = {
        "status": "ok",
        "department_id": dept_id,
        "department_name": dept.get("name", ""),
        "department_path": paths.get(dept_id, ""),
        "total_members": len(dept_members),
        "github_mapped": len(mapped),
        "mapped_list": [{"name": m["name"], "github": m["github_login"]} for m in mapped],
        "unmapped_list": [m["name"] for m in unmapped],
        "message": f"已设置「{dept.get('name', '')}」为团队。{len(dept_members)} 人，{len(mapped)} 人有 GitHub 映射。",
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_fetch_team(args):
    """采集整个团队的数据。支持 GitHub team 和企微部门两种来源。"""
    config = load_config()

    # 基础配置校验（fetch-team 下 role 不强制，可由 SKILL.md 从 whoami.position 拿）
    missing = [f for f in ("token", "scopes") if not config.get(f)]
    if not (config.get("wecom_corpid") and config.get("wecom_secret")) and not config.get("role"):
        missing.append("role")
    if missing:
        json.dump({
            "error": "config_incomplete",
            "missing": missing,
            "message": f"团队周报配置不完整，缺少: {', '.join(missing)}",
            "config_file": CONFIG_FILE,
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    token = config["token"]
    scopes = config["scopes"]

    # 决定成员来源
    source = getattr(args, "source", None)
    requested_dept = getattr(args, "department_id", None)
    refresh = getattr(args, "refresh", False)
    wecom_dept = requested_dept or config.get("wecom_department")
    team = config.get("team") or {}

    if source == "wecom" or (source is None and wecom_dept):
        # 企微来源
        members, team_info = _resolve_wecom_members(config, wecom_dept)
    elif source == "github" or (source is None and team.get("org")):
        # GitHub team 来源
        if not team.get("org") or not team.get("slug"):
            json.dump({"error": "config_incomplete", "missing": ["team"],
                        "message": "未配置 GitHub team，请先运行 team-discover"}, sys.stdout, ensure_ascii=False, indent=2)
            print()
            sys.exit(1)
        members = fetch_team_members(team["org"], team["slug"], token)
        if isinstance(members, dict) and "error" in members:
            json.dump(members, sys.stdout, ensure_ascii=False, indent=2)
            print()
            sys.exit(1)
        team_info = {"source": "github", "org": team["org"], "slug": team["slug"]}
    else:
        json.dump({
            "error": "no_team_source",
            "message": "未配置团队来源。请先运行 wecom-sync + wecom-set-team（企微）或 team-discover（GitHub）",
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    if isinstance(members, dict) and "error" in members:
        json.dump(members, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)
    if not members:
        json.dump({"error": "empty_team", "message": "团队成员为空（或无人有 GitHub 账号映射）"}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    # 缓存命中（企微来源才做缓存，key=dept_id+since+until）
    cache_dept_id = wecom_dept if team_info.get("source") == "wecom" else None
    cache_hit = False
    if cache_dept_id and not refresh:
        cached = cache_load(cache_dept_id, args.since, args.until)
        if cached:
            cache_hit = True
            output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_output.json")
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(cached, f, ensure_ascii=False, indent=2)
            summary = {
                "status": "ok",
                "output_file": output_path,
                "team": cached.get("team"),
                "member_count": len(cached.get("members", [])),
                "pr_total": sum(len(v.get("prs", [])) for v in cached.get("data", {}).values()),
                "issue_total": sum(len(v.get("issues", [])) for v in cached.get("data", {}).values()),
                "errors": cached.get("errors", []),
                "cache_hit": True,
                "cached_at": cached.get("cached_at", ""),
                "role": config.get("role", ""),
            }
            if "subtree_groups" in cached:
                summary["subtree_groups"] = [{"dept_id": g["dept_id"], "dept_name": g["dept_name"], "member_count": len(g["members"])} for g in cached["subtree_groups"]]
            json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return

    # 2. 并行采集：每个 (member, scope) 组合调用一次 search
    def collect_for_member(user):
        authored_all, reviewed_all, issues_all = [], [], []
        for scope in scopes:
            a = search_prs(user, scope, args.since, args.until, token, query_prefix="author")
            r = search_prs(user, scope, args.since, args.until, token, query_prefix="reviewed-by")
            i = search_issues(user, scope, args.since, args.until, token)
            for result in (a, r, i):
                if isinstance(result, dict) and "error" in result:
                    return {"user": user, "error": result}
            authored_all.extend(a)
            reviewed_all.extend(r)
            issues_all.extend(i)
        prs = merge_and_dedupe(authored_all, reviewed_all)

        seen = {}
        for issue in issues_all:
            key = (issue["repo"], issue["issue_number"])
            if key not in seen:
                seen[key] = issue
        issues = sorted(seen.values(), key=lambda x: x["updated_at"], reverse=True)

        # PR 详情 + Issue comments
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for f in as_completed(
                [pool.submit(fetch_pr_details, pr, token) for pr in prs]
                + [pool.submit(fetch_issue_comments, iss, token) for iss in issues]
            ):
                f.result()

        return {"user": user, "prs": prs, "issues": issues}

    # 成员数可能较多，降低外层并发以规避 secondary rate limit
    team_data = {}
    errors = []
    with ThreadPoolExecutor(max_workers=min(5, len(members))) as pool:
        futures = {pool.submit(collect_for_member, m): m for m in members}
        for future in as_completed(futures):
            result = future.result()
            user = result["user"]
            if "error" in result:
                errors.append({"user": user, "error": result["error"]})
                continue
            team_data[user] = {"prs": result["prs"], "issues": result["issues"]}

    # 生成子部门分组元信息（企微来源时，按"顶层部门的直接子部门"划分成员，供周报分层呈现）
    subtree_groups = []
    if team_info.get("source") == "wecom" and cache_dept_id:
        wecom_data = load_wecom_data() or {}
        all_departments = wecom_data.get("departments", [])
        wecom_members = wecom_data.get("members", [])

        # 直接子部门
        direct_children = [d for d in all_departments if d.get("parentid") == cache_dept_id]

        # 建立 github_login → member 的快速索引
        login_to_member = {m.get("github_login", "").lower(): m for m in wecom_members if m.get("github_login")}

        def members_of_subtree(root_id):
            ids = _get_sub_department_ids(all_departments, root_id)
            ids.add(root_id)
            logins = []
            for m in wecom_members:
                if set(m.get("department", [])) & ids and m.get("github_login"):
                    logins.append(m["github_login"])
            return logins

        covered = set()
        for child in direct_children:
            logins = members_of_subtree(child["id"])
            if logins:
                subtree_groups.append({
                    "dept_id": child["id"],
                    "dept_name": child.get("name", ""),
                    "members": logins,
                })
                covered.update(logins)

        # 顶层部门的直属成员（不在任何子部门里的）单独一组
        direct_members = [m["github_login"] for m in wecom_members
                           if cache_dept_id in m.get("department", []) and m.get("github_login")
                           and m["github_login"] not in covered]
        if direct_members:
            top_name = next((d["name"] for d in all_departments if d["id"] == cache_dept_id), "")
            subtree_groups.insert(0, {
                "dept_id": cache_dept_id,
                "dept_name": f"{top_name}（直属）",
                "members": direct_members,
            })

    output_data = {
        "team": team_info,
        "members": members,
        "data": team_data,
        "errors": errors,
        "subtree_groups": subtree_groups,
        "cached_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # 写缓存
    if cache_dept_id:
        cache_save(cache_dept_id, args.since, args.until, output_data)

    summary = {
        "status": "ok",
        "output_file": output_path,
        "team": team_info,
        "member_count": len(members),
        "pr_total": sum(len(v["prs"]) for v in team_data.values()),
        "issue_total": sum(len(v["issues"]) for v in team_data.values()),
        "errors": errors,
        "role": config.get("role", ""),
    }
    # 企微来源时，附加未映射成员信息和子部门分组
    if team_info.get("source") == "wecom" and team_info.get("unmapped_members"):
        summary["unmapped_members"] = team_info["unmapped_members"]
    if subtree_groups:
        summary["subtree_groups"] = [{"dept_id": g["dept_id"], "dept_name": g["dept_name"], "member_count": len(g["members"])} for g in subtree_groups]
    summary["cache_hit"] = False
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_list_report_targets(args):
    """按深度枚举应出周报的部门清单，供 Claude 在批量模式下遍历。

    深度定义：根部门（id=1）= 0，它下面一级 = 1，再下一级 = 2，以此类推。
    """
    config = load_config()
    wecom_data = load_wecom_data()
    if not wecom_data:
        json.dump({"error": "wecom_not_synced", "message": "企微数据未同步，请先运行 wecom-sync"},
                   sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    max_depth = args.max_depth
    if max_depth is None:
        max_depth = config.get("report_depth")
    if max_depth is None:
        json.dump({
            "error": "report_depth_not_set",
            "message": "未设置 report_depth。请先跑 config --set report_depth N（N 是最大部门层级数，通常 2 或 3）。",
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    departments = wecom_data.get("departments", [])
    members = wecom_data.get("members", [])
    dept_by_id = {d["id"]: d for d in departments}

    # 计算每个部门的深度
    def depth_of(did):
        n = 0
        d = dept_by_id.get(did)
        while d and d.get("parentid") and d["parentid"] in dept_by_id:
            n += 1
            d = dept_by_id[d["parentid"]]
            if n > 20:
                break
        return n

    # 每个部门子树成员数（含子部门）
    def members_in_subtree(root_id):
        ids = _get_sub_department_ids(departments, root_id)
        ids.add(root_id)
        return sum(1 for m in members if set(m.get("department", [])) & ids)

    # 路径
    tree = build_department_tree(departments)
    paths = flatten_tree_paths(tree)

    # leader_overrides 合并进判断
    overrides = config.get("leader_overrides") or {}
    overrides_inv = {}  # dept_id → userid list
    for uid, dept_ids in overrides.items():
        for did in dept_ids:
            overrides_inv.setdefault(did, []).append(uid)

    def leader_names(did):
        d = dept_by_id.get(did, {})
        leader_uids = list(d.get("department_leader") or [])
        leader_uids.extend(overrides_inv.get(did, []))
        names = []
        for uid in leader_uids:
            m = next((x for x in members if x.get("userid") == uid), None)
            names.append(m.get("name", uid) if m else uid)
        return names

    results = []
    for d in departments:
        depth = depth_of(d["id"])
        if depth == 0:
            continue  # 跳过根节点（公司整体一般不单独出周报，由 CEO 视角覆盖）
        if depth > max_depth:
            continue

        leaders = leader_names(d["id"])
        subtree_member_count = members_in_subtree(d["id"])

        if args.only_with_leader and not leaders:
            continue
        if args.only_with_members and subtree_member_count == 0:
            continue

        results.append({
            "dept_id": d["id"],
            "name": d.get("name", ""),
            "path": paths.get(d["id"], ""),
            "depth": depth,
            "parent_id": d.get("parentid"),
            "leaders": leaders,
            "member_count_subtree": subtree_member_count,
        })

    # 按深度 + 顺序排序，便于 Claude 遍历
    results.sort(key=lambda x: (x["depth"], x["dept_id"]))

    json.dump({"max_depth": max_depth, "total": len(results), "departments": results},
               sys.stdout, ensure_ascii=False, indent=2)
    print()


def cmd_leader_override(args):
    """维护本地 leader_overrides 映射表。

    用于补企微 department_leader 字段未维护的情况，如"张旭是 AI平台下所有开发的 leader，
    但企微里没写"。
    """
    config = load_config()
    overrides = config.get("leader_overrides") or {}
    wecom_data = load_wecom_data()

    def dept_name(did):
        if wecom_data:
            d = next((x for x in wecom_data.get("departments", []) if x["id"] == did), None)
            return d.get("name", "") if d else ""
        return ""

    if args.set:
        userid, dept_ids_str = args.set
        try:
            dept_ids = [int(x.strip()) for x in dept_ids_str.split(",") if x.strip()]
        except ValueError:
            json.dump({"error": "invalid_dept_ids", "message": "DEPT_IDS 必须是逗号分隔的整数，如 63,69"},
                       sys.stdout, ensure_ascii=False, indent=2)
            print()
            sys.exit(1)

        # 校验 userid 和部门 ID 存在
        member = None
        if wecom_data:
            member = next((m for m in wecom_data.get("members", []) if m.get("userid") == userid), None)
            if not member:
                json.dump({"error": "userid_not_found",
                            "message": f"企微通讯录里没有 userid={userid} 的成员。请先 wecom-sync 或确认 userid。"},
                           sys.stdout, ensure_ascii=False, indent=2)
                print()
                sys.exit(1)
            unknown = [did for did in dept_ids if dept_name(did) == ""]
            if unknown:
                json.dump({"error": "dept_not_found",
                            "message": f"以下部门 ID 不存在: {unknown}"},
                           sys.stdout, ensure_ascii=False, indent=2)
                print()
                sys.exit(1)

        overrides[userid] = dept_ids
        config["leader_overrides"] = overrides
        save_config(config)

        result = {
            "status": "ok",
            "action": "set",
            "userid": userid,
            "name": member.get("name", "") if member else "",
            "departments": [{"id": did, "name": dept_name(did)} for did in dept_ids],
            "message": f"已将 {userid}{('（' + member['name'] + '）') if member else ''} 设为以下部门的 leader：{', '.join(dept_name(d) for d in dept_ids)}",
        }
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    if args.unset:
        userid = args.unset
        if userid in overrides:
            del overrides[userid]
            config["leader_overrides"] = overrides
            save_config(config)
            json.dump({"status": "ok", "action": "unset", "userid": userid, "message": f"已移除 {userid} 的 override"},
                       sys.stdout, ensure_ascii=False, indent=2)
        else:
            json.dump({"status": "noop", "message": f"{userid} 没有 override"},
                       sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    if args.list:
        items = []
        for uid, dept_ids in overrides.items():
            m = None
            if wecom_data:
                m = next((x for x in wecom_data.get("members", []) if x.get("userid") == uid), None)
            items.append({
                "userid": uid,
                "name": m.get("name", "") if m else "",
                "departments": [{"id": did, "name": dept_name(did)} for did in dept_ids],
            })
        json.dump({"overrides": items, "count": len(items)}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return


def cmd_whoami(args):
    """识别当前用户身份。

    身份来源优先级（从高到低）：
    1. CLI 参数 --wecom-userid（显式传入，如 claw 平台把消息元数据 sender_id 传过来）
    2. CLI 参数 --github-login（显式传入）
    3. 环境变量 WECOM_USERID / CALLER_USERID / QCLAW_CALLER_USERID / CLAW_SENDER_ID
    4. config.token → 调 GitHub /user 反查 login（兜底）

    流程：
    A. 解析出 userid 或 github_login
    B. 去 wecom.json 匹配企微成员
    C. 判断是否 leader（基于 department_leader 字段 + 高信号职位关键词 + overrides）
    D. 返回身份 JSON
    """
    config = load_config()
    wecom_data = load_wecom_data()
    if not wecom_data:
        json.dump({
            "error": "wecom_not_synced",
            "message": "企微数据未同步，请先运行 wecom-sync",
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    members = wecom_data.get("members", [])
    me = None
    github_login = ""

    # 1. 优先：显式 --wecom-userid
    wecom_userid = getattr(args, "wecom_userid", None)
    # 2. 环境变量兜底
    if not wecom_userid:
        for env_key in ("WECOM_USERID", "CALLER_USERID", "QCLAW_CALLER_USERID", "CLAW_SENDER_ID"):
            if os.environ.get(env_key):
                wecom_userid = os.environ[env_key]
                break

    if wecom_userid:
        me = next((m for m in members if m.get("userid") == wecom_userid), None)
        if me:
            github_login = me.get("github_login", "")

    # 3. 其次：显式 --github-login
    if not me:
        gh = getattr(args, "github_login", None)
        if gh:
            github_login = gh
            me = next((m for m in members if (m.get("github_login") or "").lower() == gh.lower()), None)

    # 4. 兜底：用 config.token 反查 GitHub login
    if not me:
        token = config.get("token")
        if token:
            try:
                resp = requests.get(f"{GITHUB_API}/user",
                                     headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
                                     timeout=10)
                if resp.status_code == 200:
                    github_login = resp.json().get("login", "")
                    me = next((m for m in members if (m.get("github_login") or "").lower() == github_login.lower()), None)
                elif resp.status_code in (401, 403):
                    json.dump({"error": "auth_failed",
                                "message": f"GitHub token 无效或过期: {resp.status_code} {resp.text[:200]}"},
                               sys.stdout, ensure_ascii=False, indent=2)
                    print()
                    sys.exit(1)
            except Exception as e:
                json.dump({"error": "github_unreachable", "message": str(e)},
                           sys.stdout, ensure_ascii=False, indent=2)
                print()
                sys.exit(1)

    if not me:
        # 所有路径都没找到
        if wecom_userid:
            msg = f"企微通讯录里没找到 userid={wecom_userid} 的成员。请先运行 wecom-sync 同步最新数据。"
        elif github_login:
            msg = f"企微通讯录里没找到 github_login={github_login} 的成员。请联系 HR 在企微通讯录中把你的 GitHub 账号（{github_login}）填到『别名』字段，然后重新运行 wecom-sync。"
        else:
            msg = "无法识别当前用户身份：没有提供 --wecom-userid / --github-login，环境变量和 GitHub token 也都不可用。请显式传入其中一项。"
        json.dump({
            "error": "identity_unresolved",
            "github_login": github_login,
            "wecom_userid": wecom_userid or "",
            "message": msg,
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        sys.exit(1)

    # 3. 判断是否 leader
    departments = wecom_data.get("departments", [])
    my_userid = me.get("userid", "")
    my_dept_ids = set(me.get("department", []))

    # 基于 department_leader 字段
    leader_of = []
    for d in departments:
        leaders = d.get("department_leader") or []  # WeCom API 字段，可能是 userid 列表
        if my_userid in leaders:
            leader_of.append(d["id"])

    # 高信号职位关键词兜底：企微 department_leader 未配置时的补救
    # 只保留中文/英文语境下几乎不歧义的高管头衔，避免"产品经理/运营经理"误判
    position = me.get("position", "")
    HIGH_SIGNAL_TITLES = [
        "CEO", "CTO", "CFO", "COO", "CPO", "CIO",
        "VP", "SVP", "EVP",
        "总裁", "副总裁", "董事长",
        "总监", "Director",
    ]
    position_suggests_leader = any(kw in position for kw in HIGH_SIGNAL_TITLES)

    # 本地 overrides：管理员可在 config.leader_overrides = {userid: [dept_ids]} 里手动补企微缺失
    overrides = config.get("leader_overrides") or {}
    override_depts = overrides.get(my_userid, [])
    for did in override_depts:
        if did not in leader_of:
            leader_of.append(did)

    is_leader = bool(leader_of) or position_suggests_leader

    # 推荐默认范围
    # - 有 leader_of → 取最顶层的那个部门
    # - 职位是高管（CEO/VP 等）但没命中 department_leader → 取本人所属部门中最顶层的
    # - 非 leader → 个人周报
    default_team_dept_id = None

    def dept_depth(did):
        d = next((x for x in departments if x["id"] == did), None)
        n = 0
        while d and d.get("parentid") and d["parentid"] != 0:
            n += 1
            d = next((x for x in departments if x["id"] == d["parentid"]), None)
            if n > 20:
                break
        return n

    if leader_of:
        default_team_dept_id = min(leader_of, key=dept_depth)
    elif position_suggests_leader and my_dept_ids:
        default_team_dept_id = min(my_dept_ids, key=dept_depth)

    # 部门路径辅助信息
    tree = build_department_tree(departments)
    paths = flatten_tree_paths(tree)

    result = {
        "status": "ok",
        "github_login": github_login,
        "wecom_userid": my_userid,
        "wecom_name": me.get("name", ""),
        "position": position,
        "email": me.get("email", ""),
        "departments": [{"id": did, "path": paths.get(did, "")} for did in sorted(my_dept_ids)],
        "is_leader": is_leader,
        "leader_of": [{"id": did, "path": paths.get(did, "")} for did in leader_of],
        "leader_source": (
            "department_leader" if any(did not in override_depts for did in leader_of)
            else ("overrides" if override_depts
            else ("title_keyword" if position_suggests_leader else None))
        ),
        "default_team": {
            "department_id": default_team_dept_id,
            "department_path": paths.get(default_team_dept_id, "") if default_team_dept_id else "",
        } if default_team_dept_id else None,
        "recommendation": (
            "team" if is_leader and default_team_dept_id else "personal"
        ),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


def main(argv=None):
    args = parse_args(argv)
    if args.command == "config":
        cmd_config(args)
    elif args.command == "scopes":
        cmd_scopes(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "team-discover":
        cmd_team_discover(args)
    elif args.command == "fetch-team":
        cmd_fetch_team(args)
    elif args.command == "wecom-sync":
        cmd_wecom_sync(args)
    elif args.command == "wecom-team":
        cmd_wecom_team(args)
    elif args.command == "wecom-set-team":
        cmd_wecom_set_team(args)
    elif args.command == "whoami":
        cmd_whoami(args)
    elif args.command == "leader-override":
        cmd_leader_override(args)
    elif args.command == "list-report-targets":
        cmd_list_report_targets(args)


if __name__ == "__main__":
    main()
