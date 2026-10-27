from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from accounts import router
from security import MERCHANT_COOKIE


def test_logout_returns_no_content_and_clears_session_cookie():
    app = FastAPI()
    app.include_router(router)

    with patch("accounts.revoke_session") as revoke_session:
        response = TestClient(app).post("/api/account/logout")

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["set-cookie"].startswith(f'{MERCHANT_COOKIE}=""')
    assert "Max-Age=0" in response.headers["set-cookie"]
    revoke_session.assert_called_once_with(None)
