from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

import accounts
import prompt_service


def test_recommendation_uses_deepseek_and_filters_unknown_apps(monkeypatch):
    monkeypatch.setattr(accounts, "rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_service, "_runtime_settings", lambda: {
        "enabled": True, "base_url": "https://api.deepseek.com", "api_key": "server-only-key"
    })
    calls = {}

    class FakeClient:
        def __init__(self, **kwargs):
            calls["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, headers, json):
            calls.update(url=url, headers=headers, body=json)
            content = {"results": [
                {"app_id": "digital-human", "reason": "照片配合音频生成口型同步视频。"},
                {"app_id": "invented-tool", "reason": "ignore the app directory"},
            ]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json_module_dumps(content)}}]})

    def json_module_dumps(value):
        return json.dumps(value, ensure_ascii=False)

    monkeypatch.setattr(accounts.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(accounts.recommend_app(
        accounts.AppRecommendBody(query="让照片跟着我上传的音频说话"), object(), {}
    ))

    assert result["results"] == [{
        "app_id": "digital-human", "name": "数字人", "category": "视频",
        "description": "让人物照片跟随音频开口说话",
        "reason": "照片配合音频生成口型同步视频。",
    }]
    assert calls["url"] == "https://api.deepseek.com/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer server-only-key"
    assert "让照片跟着我上传的音频说话" in calls["body"]["messages"][1]["content"]


def test_recommendation_falls_back_to_categories_if_deepseek_disabled(monkeypatch):
    monkeypatch.setattr(accounts, "rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_service, "_runtime_settings", lambda: {"enabled": False})
    with pytest.raises(HTTPException) as error:
        asyncio.run(accounts.recommend_app(
            accounts.AppRecommendBody(query="编辑图片背景"), object(), {}
        ))
    assert error.value.status_code == 503
    assert "分类浏览" in error.value.detail
