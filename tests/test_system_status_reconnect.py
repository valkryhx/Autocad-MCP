"""P0: a dead Application proxy must not be cached by ``system_status``.

``system_status`` is the tool a caller reaches for *first* after a failure, and
it is the one that runs on a connection the caller already suspects. It used to
catch every COM error, put it in the reply as ``connected: false`` and return -
without dropping the cached Application proxy.

That made one lost connection permanent. ``_acad_app()`` only re-Dispatches when
``"app" not in _COM_STATE``, so a proxy left behind by a killed ``acad.exe`` was
re-used by every later call and answered ``RPC_S_SERVER_UNAVAILABLE`` forever.
The only recovery left was killing the server process, which the caller reads as
"MCP keeps disconnecting".

Measured on 2026-11-19: after ``taskkill /F /IM acad.exe``, three consecutive
``system_status`` calls all answered ``(-2147023174, 'RPC 服务器不可用。')``, and a
manual ``_drop_app()`` made the very next call reconnect and report
``connected: True`` again.

These tests run without AutoCAD: they patch ``_acad_app`` and assert on the
cache, so they fail on the old code and pass on the fix.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from backends import com_backend
from backends.com_backend import ComBackend

#: The two answers a dead out-of-process server gives. Both are in
#: ``_CONNECTION_LOST`` and both must drop the proxy.
_SERVER_UNAVAILABLE = -2147023174  # RPC_S_SERVER_UNAVAILABLE "RPC 服务器不可用"
_DISCONNECTED = -2147417848  # RPC_E_DISCONNECTED "已为另一线程整理的接口"

needs_pywin32 = pytest.mark.skipif(
    importlib.util.find_spec("win32com") is None, reason="pywin32 not installed"
)


def _com_error(hresult: int, text: str = "RPC 服务器不可用。"):
    """A pywin32-shaped com_error carrying ``hresult`` as ``args[0]``."""
    if importlib.util.find_spec("win32com") is not None:
        import pywintypes

        return pywintypes.com_error(hresult, text, None, None)
    # Mirror the attribute the backend reads when pywin32 is absent.
    return SimpleNamespace(args=(hresult, text, None, None), hresult=hresult)


@pytest.fixture
def clean_state(monkeypatch):
    """Start and end with an empty proxy cache - no live AutoCAD needed."""
    com_backend._drop_app()
    yield
    com_backend._drop_app()


@pytest.fixture
def inline_run(monkeypatch):
    """Run ``_run``'s callable on the calling thread instead of the STA worker.

    Patched onto the *class* as a two-argument function, so ``self`` binds as
    usual; patching the bound method would not survive ``monkeypatch`` on an
    instance that is built inside the test.
    """

    async def run_inline(self, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ComBackend, "_run", run_inline)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hresult",
    [_SERVER_UNAVAILABLE, _DISCONNECTED],
    ids=["RPC_S_SERVER_UNAVAILABLE", "RPC_E_DISCONNECTED"],
)
async def test_system_status_drops_a_dead_proxy(
    monkeypatch, clean_state, inline_run, hresult
):
    """The P0: a connection-level answer has to evict the cached proxy.

    Without the drop the cache keeps the dead object, ``_acad_app`` never
    re-Dispatches, and every later call fails the same way for the life of the
    server process.
    """
    backend = ComBackend()
    com_backend._COM_STATE["app"] = object()  # a proxy that is about to die

    def dead_app():
        raise _com_error(hresult)

    monkeypatch.setattr(com_backend, "_acad_app", dead_app)

    status = await backend.system_status()

    assert status["connected"] is False
    assert "error" in status
    assert "app" not in com_backend._COM_STATE, (
        "a connection-level failure must evict the dead Application proxy; "
        "keeping it makes the outage permanent until the server is killed"
    )


@pytest.mark.asyncio
async def test_system_status_reconnects_on_the_next_call(
    monkeypatch, clean_state, inline_run
):
    """After the drop, the next call re-Dispatches and reports a live app.

    This is the behaviour the operator wants: one failed status read, then
    recovery - not a server restart.
    """
    backend = ComBackend()
    com_backend._COM_STATE["app"] = object()

    calls = {"n": 0}

    def flaky_app():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _com_error(_SERVER_UNAVAILABLE)
        return SimpleNamespace(
            Version="25.1s (LMS Tech)",
            Documents=SimpleNamespace(Count=0),
            ActiveDocument=None,
        )

    monkeypatch.setattr(com_backend, "_acad_app", flaky_app)

    first = await backend.system_status()
    assert first["connected"] is False

    second = await backend.system_status()
    assert second["connected"] is True, (
        "the second status call must reconnect after the proxy was dropped"
    )
    assert calls["n"] == 2, "the proxy has to be asked for again, not re-used"


@pytest.mark.asyncio
async def test_system_status_keeps_the_proxy_on_a_document_scoped_error(
    monkeypatch, clean_state, inline_run
):
    """A failure that is not connection-level must not evict a good proxy.

    Dropping on every exception would turn a harmless unreadable property into
    a full re-Dispatch (and a re-launch of AutoCAD under ``CAD_LAUNCH=auto``),
    which is exactly the churn this fix must not introduce.
    """
    backend = ComBackend()
    proxy = SimpleNamespace(
        Version="25.1s (LMS Tech)",
        # Documents.Count raises - the proxy itself is fine.
        Documents=SimpleNamespace(Count=None),
    )
    com_backend._COM_STATE["app"] = proxy

    def app_with_broken_documents():
        class Documents:
            @property
            def Count(self):
                raise _com_error(-2147352567, "发生意外。")  # DISP_E_EXCEPTION

        return SimpleNamespace(Version="25.1s (LMS Tech)", Documents=Documents(),
                               ActiveDocument=None)

    monkeypatch.setattr(com_backend, "_acad_app", app_with_broken_documents)

    status = await backend.system_status()

    assert status["connected"] is False
    assert com_backend._COM_STATE.get("app") is proxy, (
        "a non-connection error must leave the working proxy in place"
    )


@pytest.mark.asyncio
async def test_connection_lost_set_matches_what_system_status_acts_on(
    monkeypatch, clean_state, inline_run
):
    """The set is the contract: every member must evict, one from outside must not.

    Guards against the two places drifting apart again - the same class of bug
    ``9654e01`` fixed for ``_run`` and ``_handle_to_object``.
    """
    backend = ComBackend()

    for hresult in sorted(com_backend._CONNECTION_LOST):
        com_backend._drop_app()
        com_backend._COM_STATE["app"] = object()
        monkeypatch.setattr(
            com_backend,
            "_acad_app",
            lambda hr=hresult: (_ for _ in ()).throw(_com_error(hr)),
        )
        await backend.system_status()
        assert "app" not in com_backend._COM_STATE, (
            f"{hresult:#010x} is in _CONNECTION_LOST but the proxy survived it"
        )

    # RPC_E_CALL_REJECTED is deliberately NOT in the set: the server is alive,
    # just busy. Dropping there would turn a press-ESC retry into a re-Dispatch.
    assert com_backend._RPC_E_CALL_REJECTED not in com_backend._CONNECTION_LOST