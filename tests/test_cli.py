import subprocess
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli

def test_cli_missing_required_args():
    """缺少必需参数时应退出码 2"""
    result = subprocess.run(
        ["python3", "cli.py"],
        capture_output=True, text=True
    )
    assert result.returncode == 2

def test_cli_help():
    """--help 应正常输出"""
    result = subprocess.run(
        ["python3", "cli.py", "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--user" in result.stdout
    assert "--org" in result.stdout
    assert "--since" in result.stdout
    assert "--until" in result.stdout

def _mock_response(status_code, json_data, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = json.dumps(json_data)
    resp.headers = headers or {"X-RateLimit-Remaining": "29", "X-RateLimit-Reset": "0"}
    return resp

@patch("cli.requests.get")
def test_search_prs_basic(mock_get):
    """正常返回 PR 列表"""
    mock_get.return_value = _mock_response(200, {
        "total_count": 1,
        "items": [{
            "number": 100,
            "title": "feat: add feature",
            "state": "open",
            "created_at": "2026-03-17T10:00:00Z",
            "pull_request": {"merged_at": None},
            "html_url": "https://github.com/org/repo/pull/100",
            "repository_url": "https://api.github.com/repos/org/repo",
        }]
    })
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", "fake-token")
    assert len(results) == 1
    assert results[0]["pr_number"] == 100
    assert results[0]["repo"] == "repo"

@patch("cli.requests.get")
def test_search_prs_auth_error(mock_get):
    """401 应返回错误字典"""
    mock_get.return_value = _mock_response(401, {"message": "Bad credentials"})
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", "bad-token")
    assert "error" in results
    assert results["error"] == "auth_failed"

@patch("cli.requests.get")
def test_search_prs_no_token(mock_get):
    """未提供 token 时应返回 auth_failed"""
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", token=None)
    assert results["error"] == "auth_failed"

@patch("cli.requests.get")
def test_search_prs_empty_result(mock_get):
    """无结果时返回空列表"""
    mock_get.return_value = _mock_response(200, {"total_count": 0, "items": []})
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", "fake-token")
    assert results == []

@patch("cli.requests.get")
def test_search_prs_pagination(mock_get):
    """分页：第一页 100 条，第二页剩余"""
    page1_items = [{"number": i, "title": f"pr-{i}", "state": "open",
                    "created_at": "2026-03-17T10:00:00Z",
                    "pull_request": {"merged_at": None},
                    "html_url": f"https://github.com/org/repo/pull/{i}",
                    "repository_url": "https://api.github.com/repos/org/repo"}
                   for i in range(100)]
    page2_items = [{"number": 100, "title": "pr-100", "state": "open",
                    "created_at": "2026-03-17T10:00:00Z",
                    "pull_request": {"merged_at": None},
                    "html_url": "https://github.com/org/repo/pull/100",
                    "repository_url": "https://api.github.com/repos/org/repo"}]
    mock_get.side_effect = [
        _mock_response(200, {"total_count": 101, "items": page1_items}),
        _mock_response(200, {"total_count": 101, "items": page2_items}),
    ]
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", "fake-token")
    assert len(results) == 101

@patch("cli.time.sleep")
@patch("cli.requests.get")
def test_search_prs_rate_limit_retry(mock_get, mock_sleep):
    """429 限流后重试成功"""
    mock_get.side_effect = [
        _mock_response(429, {"message": "rate limit"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
        _mock_response(200, {"total_count": 1, "items": [{
            "number": 1, "title": "pr", "state": "open",
            "created_at": "2026-03-17T10:00:00Z",
            "pull_request": {"merged_at": None},
            "html_url": "https://github.com/org/repo/pull/1",
            "repository_url": "https://api.github.com/repos/org/repo"}]}),
    ]
    results = cli.search_prs("testuser", "org", "2026-03-17", "2026-03-21", "fake-token")
    assert len(results) == 1
    mock_sleep.assert_called_once()

def test_merge_and_dedupe():
    """同一 PR 既是 author 又是 reviewer 时，合并角色"""
    authored = [
        {"repo": "matrixflow", "pr_number": 100, "title": "feat: x", "state": "open",
         "role": ["author"], "created_at": "2026-03-17", "merged_at": None,
         "url": "https://github.com/org/matrixflow/pull/100"},
    ]
    reviewed = [
        {"repo": "matrixflow", "pr_number": 100, "title": "feat: x", "state": "open",
         "role": ["reviewed_by"], "created_at": "2026-03-17", "merged_at": None,
         "url": "https://github.com/org/matrixflow/pull/100"},
        {"repo": "matrixflow", "pr_number": 200, "title": "fix: y", "state": "merged",
         "role": ["reviewed_by"], "created_at": "2026-03-18", "merged_at": "2026-03-19",
         "url": "https://github.com/org/matrixflow/pull/200"},
    ]
    result = cli.merge_and_dedupe(authored, reviewed)
    assert len(result) == 2
    pr100 = next(r for r in result if r["pr_number"] == 100)
    assert sorted(pr100["role"]) == ["author", "reviewed_by"]
    pr200 = next(r for r in result if r["pr_number"] == 200)
    assert pr200["role"] == ["reviewed_by"]
