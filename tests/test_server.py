"""Tests for the MCP tool layer: session retry and the tools added for the audit."""

import asyncio
from types import SimpleNamespace

from brightspace_mcp import server
from brightspace_mcp.api import SessionExpiredError
from brightspace_mcp.server import AppContext


class _Ctx:
    def __init__(self, app):
        self.request_context = SimpleNamespace(lifespan_context=app)

    async def info(self, _msg):
        pass


class _FakeApi:
    def __init__(self, overview=None, attachment=(200, "Outline.pdf", 2048)):
        self.overview = overview
        self.attachment = attachment
        self.closed = False

    async def get_overview(self, course_id):
        return self.overview

    async def overview_attachment_info(self, course_id):
        return self.attachment

    async def test_auth(self):
        return True

    async def close(self):
        self.closed = True


def _app(api):
    return AppContext(config=SimpleNamespace(), api=api)


def test_retry_gives_up_with_a_login_message(monkeypatch):
    # Restore works, whoami works, the call still says expired: report it, never raise.
    async def restore(_cfg):
        return [{"name": "c", "value": "v"}]

    monkeypatch.setattr(server, "try_restore_session", restore)
    monkeypatch.setattr(server, "BrightspaceAPI", lambda cfg, cookies: _FakeApi())
    app = _app(_FakeApi())

    async def always_expired():
        raise SessionExpiredError()

    out = asyncio.run(server._with_auth_retry(app, always_expired))
    assert "Session expired" in out and "login" in out
    assert app.api is None


def test_retry_succeeds_after_restore(monkeypatch):
    async def restore(_cfg):
        return [{"name": "c", "value": "v"}]

    monkeypatch.setattr(server, "try_restore_session", restore)
    monkeypatch.setattr(server, "BrightspaceAPI", lambda cfg, cookies: _FakeApi())
    app = _app(_FakeApi())
    calls = []

    async def once_expired():
        calls.append(1)
        if len(calls) == 1:
            raise SessionExpiredError()
        return "ok"

    assert asyncio.run(server._with_auth_retry(app, once_expired)) == "ok"


def test_get_course_overview_reports_unreadable_attachment():
    ov = {"HasAttachment": True, "Description": {"Html": '<a href="/x.pdf?d2lSessionVal=S">outline</a>'}}
    app = _app(_FakeApi(overview=ov, attachment=(403, "", 0)))
    out = asyncio.run(server.get_course_overview(1, ctx=_Ctx(app)))
    assert out["attachment"] == {"status": "attachment present, unreadable (HTTP 403)"}
    assert out["links"] == [{"text": "outline", "href": "/x.pdf"}]


def test_get_course_overview_without_overview():
    app = _app(_FakeApi(overview=None))
    assert asyncio.run(server.get_course_overview(1, ctx=_Ctx(app))) == {"has_overview": False}


def test_tools_refuse_without_session():
    app = _app(None)
    for call in (server.get_course_overview(1, ctx=_Ctx(app)),
                 server.download_course_overview(1, ctx=_Ctx(app)),
                 server.download_linked_file("/x.pdf", ctx=_Ctx(app)),
                 server.download_announcement_file(1, 2, 3, ctx=_Ctx(app)),
                 server.audit_course(1, ctx=_Ctx(app))):
        assert asyncio.run(call) == "Not authenticated. Call 'login' first."
