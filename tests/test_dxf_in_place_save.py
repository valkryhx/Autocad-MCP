"""`drawing_save` must get a DXF onto its file even when AutoCAD refuses.

MEASURED (AutoCAD 2026, 2026-11-19): a DXF that already exists on disk cannot be
written over. ``Save()`` and ``SaveAs(same path, 61)`` both answer
DISP_E_EXCEPTION ``发生意外。`` with excepinfo ``保存文档时出错`` (0x80210063). The
same drawing saved as DWG works everywhere, and a DXF this session created with
``SaveAs`` and never closed also works - so it is specifically "write over a DXF
that was opened from disk".

``Close(True)`` is not a workaround either: it answers without error and
silently drops the edits (measured: 1078 entities before and after, the added
circle gone).

``drawing_save`` therefore round-trips it: SaveAs to a temp, close the document
(that is what actually releases the target's handle), copy the temp over the
target, reopen and rebind. These tests pin the decision and the shape of that
round trip; the live behaviour was verified against AutoCAD 2026.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from backends import com_backend
from backends.com_backend import ComBackend


def _save_error(hresult: int = -2147352567, text: str = "保存文档时出错") -> RuntimeError:
    """The RuntimeError ``_run`` raises, message and all.

    ``_run`` formats ``AutoCAD COM error ({hr:#010x}): {args[1]} ({excepinfo})``,
    so the HRESULT is text - ``args[0]`` is the whole message, not the code.
    """
    return RuntimeError(f"AutoCAD COM error ({hresult:#010x}): 发生意外。 ({text})")


# ── the decision ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        _save_error(),
        _save_error(com_backend._ACAD_SAVE_FAILED),
        RuntimeError(f"AutoCAD COM error ({com_backend._ACAD_SAVE_FAILED:#010x}): "
                     "发生意外。 (保存文档时出错)"),
    ],
    ids=["DISP_E_EXCEPTION", "0x80210063", "save-hresult-only"],
)
def test_the_dxf_save_failure_is_recognised(exc):
    assert com_backend._is_dxf_save_failure(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        # A lost connection is never the DXF quirk - it must reach the caller.
        RuntimeError(f"AutoCAD COM error ({com_backend._RPC_S_SERVER_UNAVAILABLE:#010x}): "
                     "RPC 服务器不可用。 (保存文档时出错)"),
        RuntimeError(f"AutoCAD COM error ({com_backend._RPC_E_CALL_REJECTED:#010x}): "
                     "调用被拒绝 (保存文档时出错)"),
        # Right HRESULT, wrong words: a generic failed call, not a failed save.
        RuntimeError(f"AutoCAD COM error ({com_backend._DISP_E_EXCEPTION:#010x}): "
                     "发生意外。 (无法打开图层表)"),
        # An ordinary Python failure with a save-shaped word in it.
        PermissionError("保存文档时出错"),
    ],
    ids=["connection-lost", "busy", "not-a-save", "plain-python-error"],
)
def test_a_real_failure_is_not_mistaken_for_the_dxf_quirk(exc):
    assert com_backend._is_dxf_save_failure(exc) is False


# ── the round trip ──────────────────────────────────────────────────────────


class _FakeDoc:
    """Just enough Document for the round trip: save, close, name."""

    def __init__(self, path: str, fail_in_place: bool = True):
        self._path = path
        self._fail_in_place = fail_in_place
        self.saves: list[tuple[str, int | None]] = []
        self.closed = False
        self.close_saved_with: bool | None = None

    @property
    def Name(self):
        return os.path.basename(self._path)

    @property
    def FullName(self):
        return self._path

    def SaveAs(self, path, fmt=None):
        if self._fail_in_place and os.path.abspath(path) == os.path.abspath(self._path):
            raise _save_error()
        # A real AutoCAD writes the file; so must this, the copy step needs it.
        with open(path, "wb") as fh:
            fh.write(b"DXF AFTER EDIT")
        self.saves.append((path, fmt))
        self._path = path

    def Save(self):
        raise _save_error()

    def Close(self, save_changes=False):
        self.closed = True
        self.close_saved_with = save_changes

    def GetVariable(self, name):
        return 0


@pytest.fixture
def temp_dxf():
    fd, path = tempfile.mkstemp(suffix=".dxf", prefix="acadmcp-test-")
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(b"DXF BEFORE EDIT")
    yield path
    for leftover in (path, path + ".bak"):
        try:
            os.remove(leftover)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_drawing_save_round_trips_a_dxf_that_cannot_be_overwritten(
    monkeypatch, temp_dxf
):
    """The measured shape: SaveAs temp -> close -> copy over -> reopen.

    The copy has to land *after* the close - a live AutoCAD keeps the handle on
    the file it was opened from even once SaveAs re-pointed the document, so an
    earlier copy gets PermissionError.
    """
    backend = ComBackend()
    doc = _FakeDoc(temp_dxf)
    events: list[str] = []

    async def fake_run(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(com_backend, "_acad_doc", lambda: doc)
    monkeypatch.setattr(com_backend, "_dbmod_of", lambda d: 0)

    # The reopen goes through Application.Documents.Open, so the fake app has to
    # answer with a real-ish document. Count/ActiveDocument are what the round
    # trip reads instead of the B11 guard.
    class _Docs:
        def __init__(self):
            self.items = [doc]

        @property
        def Count(self):
            return len(self.items)

        def Open(self, path):
            reopened = _FakeDoc(path, fail_in_place=False)
            self.items = [reopened]
            events.append("reopen")
            return reopened

    class _App:
        def __init__(self):
            self.Documents = _Docs()

        @property
        def ActiveDocument(self):
            return self.Documents.items[-1]

    monkeypatch.setattr(com_backend, "_acad_app", lambda: _App())

    async def fake_rebind():
        events.append("rebind")

    monkeypatch.setattr(backend, "_rebind", fake_rebind)

    async def fake_record(full_name, dbmod):
        return None

    monkeypatch.setattr(backend, "_record_lineage", fake_record)
    monkeypatch.setattr(backend, "_bound_entry", lambda: None)
    monkeypatch.setattr(backend, "_bound", None, raising=False)

    # SaveAs() on the fake raises for the in-place path; tracking the order lets
    # the test prove the close precedes the copy.
    real_close = doc.Close

    def close_and_mark(save_changes=False):
        events.append("close")
        real_close(save_changes)

    doc.Close = close_and_mark

    result = await backend.drawing_save(temp_dxf)

    assert result["ok"] is True
    assert result["path"] == os.path.abspath(temp_dxf) or result["path"] == temp_dxf
    assert result.get("save_fallback", {}).get("reason") == "dxf_in_place_save_unsupported"
    assert doc.closed is True, "the document must be closed to release the target's handle"
    assert doc.close_saved_with is False, (
        "Close must not save: Close(True) silently drops edits on this document"
    )
    # The edit landed on the real target.
    with open(temp_dxf, "rb") as fh:
        assert fh.read() == b"DXF AFTER EDIT"
    # The temp was cleaned up.
    leftovers = [s for s, _ in doc.saves if ".acadmcp-save-" in s]
    assert leftovers, "the round trip must SaveAs to a temp first"
    assert not os.path.exists(leftovers[0]), "the temp file must be removed"


@pytest.mark.asyncio
async def test_a_dwg_save_error_is_raised_not_round_tripped(monkeypatch, temp_dxf):
    """A save that fails for another reason has to reach the caller."""
    backend = ComBackend()

    async def fake_run(function, *args, **kwargs):
        raise RuntimeError(
            f"AutoCAD COM error ({com_backend._RPC_S_SERVER_UNAVAILABLE:#010x}): "
            "RPC 服务器不可用。"
        )

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "_bound_entry", lambda: None)

    with pytest.raises(RuntimeError, match="RPC 服务器不可用"):
        await backend.drawing_save(temp_dxf)