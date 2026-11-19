# Saving DXF and DWG

A save that reports success is not a save. This file exists because on
AutoCAD 2026 a DXF save **silently depends on how the document got to its
path**, and the failure looks like a corrupt drawing rather than a refusal.

Everything below was measured on AutoCAD 2026 (`25.1s`) through COM; the
matrix is reproduced by the repo script `scripts/save_matrix.py`.

## 1. The measurement

| # | Path the document took | `Save()` | On disk |
|---|---|---|---|
| C1 | `Documents.Add()` → `SaveAs(a.dxf, 61)` → edit → `Save()` | ✅ | written |
| C2 | `SaveAs(a.dxf)` → **close** → `Open(a.dxf)` → edit → `Save()` | ❌ `0x80210063` | unchanged |
| C3 | copy → `Open(a.dxf)` → edit → `Save()` | ❌ `0x80210063` | unchanged |
| C4 | copy → `Open(a.dxf)` → edit → `SaveAs(a.dxf)` (same path) | ❌ `0x80210063` | unchanged |
| C5 | copy → `Open(a.dxf)` → edit → `SaveAs(b.dxf)` (**new path**) | ✅ | written |
| C6 | copy → `Open(a.dwg)` → edit → `Save()` | ✅ | written |
| C7 | `Add()` → `SaveAs(a.dwg, 64)` → edit → `Save()` | ✅ | written |
| C8 | copy → `Open(a.dxf)` → `SaveAs(b.dxf)` → edit → `Save()` | ✅ | written |

### The rule it gives

> **A DXF can be saved only to a path this document acquired in the current
> session by `SaveAs`. A path the document was `Open`ed from cannot be written,
> by `Save()` or by `SaveAs` to that same path.**

DWG has no such restriction: any path, any number of times.

### How long the rescue lasts

`SaveAs` to a new name rescues the document **for the rest of this session**
(C5, C8, and repeated `Save()` afterwards all work). It does **not** survive a
close: reopen the rescued file and `Save()` fails again. Measured:

```text
D0  Open(existing DXF) -> edit -> Save()        FAIL  0x80210063
D1  SaveAs(new name) -> edit -> Save()          OK
    Save() again                                 OK
D2  reopen that rescued file -> edit -> Save()  FAIL  0x80210063   <- returns
D3  SaveAs(yet another new name) -> Save()      OK
```

So "SaveAs to a new name" is a **per-session ritual**, not a one-time fix.

## 2. What the failure looks like

```text
hresult  : -2147352567  (0x80020009, DISP_E_EXCEPTION)
strerror : 发生意外。
excepinfo: (0, 'AutoCAD', '保存文档时出错',
            'D:\\autocad_26\\AutoCAD 2026\\HELP\\OLE_ERR.CHM',
            -2145320861, -2145320861)
                                    ^ 0x80210063 - AutoCAD facility 33, code 99
```

The drawing is **not** damaged and no bytes are written. The file on disk is
simply still the old one — which is the dangerous part, because a later
verification against that path silently measures the pre-edit geometry.

## 3. Correct code, by intent

### 3.1 The ordinary case — MCP `drawing_save`

```python
# DXF: succeeds either directly, or via the built-in round trip.
result = drawing_save()                       # or drawing_save(path="out.dxf")
assert result["ok"] is True
if "save_fallback" in result:
    # The direct Save() was refused; MCP round-tripped:
    #   SaveAs(temp) -> Close(False) -> copy temp over target -> reopen -> rebind
    # The file IS written. The document now points at the same target path
    # again, i.e. it is "reopened from disk", so the NEXT plain Save() will
    # fail again and will also be round-tripped.
    print(result["save_fallback"]["reason"])   # dxf_in_place_save_unsupported
```

`drawing_save` is the right call for both formats. Do not hand-roll ActiveX
saves unless you are outside the MCP.

### 3.2 The ordinary case — MCP `drawing_save_as`

```python
# New path: no fallback needed, and the document is now saveable in place.
result = drawing_save_as(path="D:/work/report_r2.dxf")
assert result["ok"] is True
assert "save_fallback" not in result          # a new path never hits the defect

result = drawing_save()                       # plain Save() on the new path works
assert "save_fallback" not in result
```

**`drawing_save_as` is the cheapest way to make a DXF iterable**: re-point it
once to a fresh path and every later `drawing_save` is a direct write.

### 3.3 Delivering — MCP `drawing_finalize` / `drawing_deliver`

```python
finalize = drawing_finalize(save_path="out/flange.dxf", requirements=req)
assert finalize["artifact_integrity"]["reverified"] is True

deliver = drawing_deliver(output_dir="out/delivery", requirements=req)
assert deliver["status"] == "success"
```

`drawing_deliver` writes **copies**; it never re-points or saves the open
document. Deliver a DXF even when you have been working in DWG.

### 3.4 Raw COM — new drawing straight to DXF

```python
import pythoncom, win32com.client
app = win32com.client.Dispatch("AutoCAD.Application")

doc = app.Documents.Add()
doc.SaveAs(r"D:\work\plan.dxf", 61)     # 61 = ac2013_dxf; omit -> x.dxf.dwg!
doc.ModelSpace.AddCircle(point, 50.0)
doc.Save()                               # OK: this path came from SaveAs
doc.Close(False)                         # never Close(True); see 3.7
```

### 3.5 Raw COM — an existing DXF you must iterate on

```python
# WRONG: Save() / SaveAs(same path) both raise 0x80210063.
doc = app.Documents.Open(existing_dxf, False)
edit(doc)
doc.Save()                               # ❌ 0x80210063

# RIGHT: re-point to a new file first, then save freely in this session.
doc = app.Documents.Open(existing_dxf, False)
edit(doc)
doc.SaveAs(fresh_path, 61)               # ✅
edit_more(doc)
doc.Save()                               # ✅ same session, no fallback needed
doc.Close(False)

# RIGHT, if the user insists on the SAME filename: round trip like MCP does.
import shutil, os, tempfile
doc = app.Documents.Open(existing_dxf, False)
edit(doc)
tmp = existing_dxf + ".tmp.dxf"
doc.SaveAs(tmp, 61)                      # a path this doc did not Open
doc.Close(False)                         # releases the lock on existing_dxf
shutil.move(tmp, existing_dxf)           # now the target is writable
doc = app.Documents.Open(existing_dxf, False)   # rebind; Save() will fail again
```

### 3.6 Raw COM — DWG

```python
# Any path, any number of times. No fallback, no ritual.
doc = app.Documents.Open(dwg_path, False)     # False = ReadOnly off
edit(doc)
doc.Save()                                     # ✅
doc.Close(False)
```

`Documents.Open(path, True)` opens **read-only**, and a `Save()` on it fails
with `0x80210063` too — the same code as the DXF defect. When reproducing a
save bug, always pass `False` and assert `doc.ReadOnly is False` first, or you
will "discover" a bug that is only a flag you set yourself.

### 3.7 Never use these as a save

| Don't | Why (measured) |
|---|---|
| `doc.Close(True)` | Reports no error and **silently discards** the edits: 1078 entities in, 1078 out, the added circle gone. Worse than an error. |
| `doc.SendCommand("_.QSAVE\n")` | Stops at a prompt and holds the COM thread >120 s; ESC does not recover it — AutoCAD must restart. |
| `SaveAs("x.dxf")` with no type | Writes `x.dxf.dwg`. Pass the type (`61`). |

## 4. Proving the save landed

`ok: true` is the tool talking about itself. The file decides.

```text
drawing_info()                       -> saved: true        (the document agrees)
drawing_verify_requirements(
    requirements=req,
    artifact_path="out/flange.dxf")  -> rows pass ON THE FILE, not in memory
drawing_verify_requirements(
    manifest_path="out/delivery/manifest.json") -> nothing stale, nothing missing
```

For a format-independent check, compare bytes and entity counts before/after:

```python
import hashlib, ezdxf
def fingerprint(path):
    return (hashlib.sha256(open(path, "rb").read()).hexdigest()[:16],
            len(list(ezdxf.readfile(path).modelspace())))
```

Two traps this closes:

- **`saved: true` on the document is not proof of the file.** After the
  round-trip fallback the document is reopened from the copied target, so it
  reports clean — which is correct, but it is the *copy* that was verified, so
  quote the file, not the flag.
- **Verifying the wrong path.** After a failed save the old file is still
  there and still passes its old requirements. Re-open the artifact explicitly
  (`artifact_path=`) rather than reusing in-memory evidence.

## 5. Choosing the working format

| You are | Use | Why |
|---|---|---|
| Iterating on a drawing | **DWG** | `Save()` works on any path, any time, no fallback |
| Handing a file to someone | DXF (and PDF) | universally readable; one `drawing_save_as` makes it |
| Forced to iterate on a DXF | `drawing_save_as` once, then `drawing_save` | converts the session into the saveable case |
| Working headless (`ezdxf`) | DXF only | no DWG writer without the ODA File Converter |

## 6. Checklist before saying "saved"

1. `drawing_save` / `drawing_finalize` returned `ok: true`.
2. If `save_fallback` appeared, say so — the file is written, but by the round
   trip, and the same thing will happen on the next save.
3. `drawing_info().saved is True`.
4. The artifact was re-measured **on the file** (`artifact_path=`), not quoted
   from the in-memory pass.
5. No `Close(True)` and no `SendCommand("_.QSAVE")` anywhere in the path.
6. If the drawing is DXF and the person will keep editing it in AutoCAD by
   hand: tell them Ctrl+S will fail, and give them 3.5.

## 7. Every claim here is executable

The snippets above are not illustrative — they are run against a live AutoCAD
by `scripts/verify_skill_save_examples.py` in the MCP repo. Current status,
last run 2026-11-19:

| Assertion | Result |
|---|---|
| 3.4 `Add → SaveAs → Save → Close(False)` lands on disk | ✅ |
| 3.5 `Save()` on an opened DXF raises `0x80210063` (not something else) | ✅ |
| 3.5 `SaveAs(new path)` then two direct `Save()`s | ✅ 1078 → 1080 |
| 3.5 round trip keeps the original filename | ✅ 1078 → 1079 |
| 3.6 DWG `SaveAs` then `Save()` ×3, `ReadOnly is False` | ✅ |
| 3.7 `Close(True)` discards the edit | ✅ 1078 → 1078 |
| 3.7 `SaveAs("x.dxf")` without a type writes `x.dxf.dwg` | ✅ |

7/7. If a future AutoCAD version fixes the defect, the "is refused" row is the
one that fails first — that is the signal to retire this file.