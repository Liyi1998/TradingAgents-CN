"""CSRF Cookie deployment-mode tests."""

from starlette.responses import Response

from app.core.config import settings
from app.middleware.csrf import set_csrf_cookie


def test_csrf_cookie_can_disable_secure_for_local_http(monkeypatch):
    monkeypatch.setattr(settings, "CSRF_COOKIE_SECURE", False)
    response = Response()

    set_csrf_cookie(response, "local-token")

    cookie = response.headers["set-cookie"]
    assert "csrf_token=local-token" in cookie
    assert "Secure" not in cookie


def test_csrf_cookie_defaults_to_secure_outside_debug(monkeypatch):
    monkeypatch.setattr(settings, "CSRF_COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "DEBUG", False)
    response = Response()

    set_csrf_cookie(response, "production-token")

    assert "Secure" in response.headers["set-cookie"]
