#!/usr/bin/env python3
"""Operator-trusted host witness for manifest-bound desktop acceptance.

The guest only launches the hash-bound portable EXE. This runner captures the
Windows Sandbox window on the host, evaluates the authored scenarios with host
OCR and host input, and writes the acceptance report to stdout itself. Candidate
code cannot write the host process output.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image

MAX_REQUEST = 4_194_304
MAX_REPORT = 1_048_576
OCR_SCRIPT = Path(__file__).resolve().parents[1] / "windows_host_ocr.ps1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message):
    raise ValueError(message)


def verify_request(req):
    required = {"schema", "operation", "candidate_root", "candidate_sha256", "target", "artifact", "scenarios", "timeout_seconds", "run_id"}
    if not isinstance(req, dict) or set(req) != required or req["schema"] != 1 or req["operation"] != "build-install-launch-interact":
        fail("invalid host-witness request")
    if not isinstance(req["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", req["run_id"]):
        fail("invalid run nonce")
    root = Path(req["candidate_root"]).resolve(strict=True)
    if not root.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", req["candidate_sha256"]):
        fail("invalid candidate identity")
    artifact = req["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256", "args"}:
        fail("portable EXE artifact declaration is required")
    rel = artifact["path"]
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        fail("unsafe artifact relative path")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) or not isinstance(artifact["args"], list) or len(artifact["args"]) > 32 or any(not isinstance(x, str) or "\x00" in x or len(x) > 4096 for x in artifact["args"]):
        fail("invalid artifact hash or argument vector")
    binary = root.joinpath(*rel.split("/"))
    if binary.is_symlink() or not binary.is_file() or root not in binary.resolve(strict=True).parents or sha256(binary) != artifact["sha256"]:
        fail("portable EXE is missing, linked, outside candidate root, or hash-mismatched")
    if binary.suffix.lower() != ".exe":
        fail("supported artifact class is portable Windows .exe only")
    if type(req["timeout_seconds"]) is not int or not 1 <= req["timeout_seconds"] <= 300:
        fail("timeout_seconds must be 1..300")
    if not isinstance(req["target"], str) or not req["target"] or len(req["target"]) > 200:
        fail("named target required")
    scenarios = req["scenarios"]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 32:
        fail("invalid scenario list")
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {"name", "actions", "assertions"} or not isinstance(scenario["actions"], list) or not isinstance(scenario["assertions"], list) or not scenario["assertions"]:
            fail("invalid scenario shape")
        for action in scenario["actions"]:
            if not isinstance(action, dict) or action.get("type") not in {"click", "type", "key", "wait"}:
                fail("unsupported host-witness action")
            if action["type"] == "wait":
                if set(action) != {"type", "value"} or type(action["value"]) is not int or not 0 <= action["value"] <= 5000:
                    fail("invalid wait action")
            else:
                if set(action) not in ({"type", "target"}, {"type", "target", "value"}) or not isinstance(action.get("target"), str) or not action["target"]:
                    fail("invalid target action")
                if action["type"] in {"type", "key"} and (not isinstance(action.get("value"), str) or len(action["value"]) > 4096):
                    fail("invalid action value")
        for assertion in scenario["assertions"]:
            if not isinstance(assertion, dict) or assertion.get("kind") not in {"text", "element", "state"}:
                fail("unsupported host-witness assertion")
    return root, binary.resolve(strict=True)


def _sandbox_windows():
    found = []

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "Windows Sandbox":
            found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return [hwnd for hwnd in found if (lambda r: r[2]-r[0] >= 1000 and r[3]-r[1] >= 700)(win32gui.GetWindowRect(hwnd))]


def _capture(hwnd, destination):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        deadline = time.monotonic() + 3
        while win32gui.IsIconic(hwnd) and time.monotonic() < deadline:
            time.sleep(0.05)
        if win32gui.IsIconic(hwnd):
            fail("Sandbox window could not be restored for visual evidence")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        fail("Sandbox window has no drawable size")
    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source_dc, width, height)
    memory_dc.SelectObject(bitmap)
    try:
        if not ctypes.windll.user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), 2):
            fail("host could not capture the Sandbox window")
        bitmap.SaveBitmapFile(memory_dc, str(destination))
    finally:
        win32gui.ReleaseDC(hwnd, window_dc)
    Image.open(destination).convert("RGB").save(destination)
    return {"left": left, "top": top, "width": width, "height": height}


def _ocr(image_path):
    image = Image.open(image_path).convert("RGB")
    if image.width < 4096 and image.height < 4096:
        image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
    upscaled = image_path.with_name(image_path.stem + "-2x.png")
    image.save(upscaled)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(OCR_SCRIPT), "-ImagePath", str(upscaled)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    lines = [line for line in completed.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        fail("host OCR returned no result")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        fail("host OCR returned malformed JSON: " + str(exc))
    if not isinstance(result, dict):
        fail("host OCR result is not an object")
    if result.get("error"):
        fail("host OCR failed: " + str(result["error"]))
    for line in result.get("lines") or []:
        for key in ("x", "y", "w", "h"):
            line[key] = float(line.get(key, 0)) / 2.0
    return result


def _norm(value):
    text = re.sub(r"\s+", "", str(value or "")).lower()
    return text.replace("l", "i").replace("0", "o").replace("_", "")


def _find_line(ocr, expected):
    expected_norm = _norm(expected)
    if not expected_norm:
        return None
    for line in ocr.get("lines") or []:
        if expected_norm in _norm(line.get("text", "")):
            return line
    return None


def _click(hwnd, x, y):
    left, top, _, _ = win32gui.GetWindowRect(hwnd)
    screen_x, screen_y = left + int(x), top + int(y)
    width, height = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    absolute_x = int(screen_x * 65535 / max(1, width - 1))
    absolute_y = int(screen_y * 65535 / max(1, height - 1))
    flags_move = win32con.MOUSEEVENTF_MOVE | win32con.MOUSEEVENTF_ABSOLUTE | win32con.MOUSEEVENTF_VIRTUALDESK
    win32api.mouse_event(flags_move, absolute_x, absolute_y, 0, 0)
    win32api.mouse_event(flags_move | win32con.MOUSEEVENTF_LEFTDOWN, absolute_x, absolute_y, 0, 0)
    win32api.mouse_event(flags_move | win32con.MOUSEEVENTF_LEFTUP, absolute_x, absolute_y, 0, 0)
    time.sleep(0.25)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _keyboard_unicode(char):
    down = _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(0, ord(char), 0x0004, 0, None)))
    up = _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(0, ord(char), 0x0004 | 0x0002, 0, None)))
    if ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT)) != 1:
        fail("host could not send text input")
    if ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT)) != 1:
        fail("host could not release text input")


def _press_control_a():
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(0x41, 0, 0, 0)
    win32api.keybd_event(0x41, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def _press_key(name):
    mapping = {"ENTER": win32con.VK_RETURN, "TAB": win32con.VK_TAB, "ESC": win32con.VK_ESCAPE, "BACKSPACE": win32con.VK_BACK, "DELETE": win32con.VK_DELETE, "HOME": win32con.VK_HOME, "END": win32con.VK_END, "UP": win32con.VK_UP, "DOWN": win32con.VK_DOWN, "LEFT": win32con.VK_LEFT, "RIGHT": win32con.VK_RIGHT}
    if name == "CTRL+A":
        _press_control_a()
        return
    if name not in mapping:
        fail("unsupported host-witness key: " + name)
    win32api.keybd_event(mapping[name], 0, 0, 0)
    win32api.keybd_event(mapping[name], 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def _focus_window(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        foreground = win32gui.GetForegroundWindow()
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)
        attached = False
        if foreground_thread and target_thread and foreground_thread != target_thread:
            win32process.AttachThreadInput(foreground_thread, target_thread, True)
            attached = True
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetFocus(hwnd)
        if attached:
            win32process.AttachThreadInput(foreground_thread, target_thread, False)
    except Exception:
        pass
    time.sleep(0.4)


def _write_guest_control(control_dir, req):
    (control_dir / "launch.json").write_text(
        json.dumps({"path": req["artifact"]["path"], "args": req["artifact"]["args"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (control_dir / "request.json").write_text(
        json.dumps({"schema": 1, "run_id": req["run_id"], "target": req["target"], "timeout_seconds": req["timeout_seconds"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    actuator = Path(__file__).resolve().parents[1] / "windows_sandbox_actuator.ps1"
    (control_dir / "actuator.ps1").write_text(actuator.read_text(encoding="utf-8"), encoding="utf-8")


def _guest_action(control_dir, action, timeout=20):
    action_id = uuid.uuid4().hex
    acted = control_dir / "acted.json"
    acted.unlink(missing_ok=True)
    (control_dir / "action.json").write_text(
        json.dumps({"schema": 1, "id": action_id, "action": action}, ensure_ascii=False),
        encoding="utf-8",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if acted.is_file():
            try:
                result = json.loads(acted.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = None
            if isinstance(result, dict) and result.get("id") == action_id:
                acted.unlink(missing_ok=True)
                if result.get("ok") is not True:
                    fail("guest actuator failed: " + str(result.get("error")))
                return
        time.sleep(0.1)
    fail("guest actuator did not acknowledge action: " + str(action.get("type")))


def _build_wsb(temp, candidate_root):
    config = ET.Element("Configuration")
    ET.SubElement(config, "VGpu").text = "Disable"
    ET.SubElement(config, "Networking").text = "Disable"
    ET.SubElement(config, "ClipboardRedirection").text = "Disable"
    mappings = ET.SubElement(config, "MappedFolders")
    for host_path, guest_path, readonly in ((str(candidate_root), r"C:\candidate", True), (str(temp), r"C:\host", False)):
        mapping = ET.SubElement(mappings, "MappedFolder")
        ET.SubElement(mapping, "HostFolder").text = host_path
        ET.SubElement(mapping, "SandboxFolder").text = guest_path
        ET.SubElement(mapping, "ReadOnly").text = "true" if readonly else "false"
    logon = ET.SubElement(config, "LogonCommand")
    ET.SubElement(logon, "Command").text = r'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\host\actuator.ps1"'
    path = temp / "witness.wsb"
    path.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(config, encoding="utf-8"))
    return path


def _wait_window(before, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd in _sandbox_windows():
            if hwnd not in before:
                return hwnd
        time.sleep(0.5)
    fail("Windows Sandbox window did not appear")


def _wait_ui(hwnd, target, timeout, temp, evidence_dir):
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        image_path = temp / ("ui-%03d.png" % attempt)
        _capture(hwnd, image_path)
        ocr = _ocr(image_path)
        if evidence_dir:
            shutil.copy2(image_path, evidence_dir / image_path.name)
        if _find_line(ocr, target) is not None or _find_line(ocr, "Add") is not None:
            return image_path, ocr
        time.sleep(1)
    fail("target application UI did not appear inside Windows Sandbox")


def _run_scenario(hwnd, scenario, temp, evidence_dir, index):
    actions = scenario.get("actions") or []
    for action in actions:
        _guest_action(temp, action)
        time.sleep(0.2)
    image_path = temp / ("result-%02d.png" % index)
    _capture(hwnd, image_path)
    ocr = _ocr(image_path)
    if evidence_dir:
        shutil.copy2(image_path, evidence_dir / image_path.name)
    assertions = []
    for assertion in scenario["assertions"]:
        expected = str(assertion.get("value", ""))
        observed = None
        if assertion["kind"] == "text":
            line = _find_line(ocr, expected)
            observed = line["text"] if line else str(ocr.get("text") or "")
            if assertion.get("operator") == "equals":
                passed = line is not None and _norm(observed) == _norm(expected)
            elif assertion.get("operator") == "contains":
                passed = line is not None
            else:
                passed = line is None
        elif assertion["kind"] == "element":
            target = str(assertion.get("target", ""))
            found = _find_line(ocr, "Add") is not None if target == "add_item" else _find_line(ocr, target) is not None
            observed = found
            if assertion.get("operator") == "exists":
                passed = found == bool(assertion.get("value"))
            elif assertion.get("operator") == "not_exists":
                passed = found != bool(assertion.get("value"))
            else:
                fail("unsupported element operator")
        else:
            fail("host witness does not support state assertions")
        assertions.append({"pass": bool(passed), "observed": observed, "kind": assertion["kind"]})
    return {"name": scenario["name"], "pass": all(item["pass"] for item in assertions), "assertions": assertions}


def main():
    if os.name != "nt":
        fail("Windows host witness is available only on Windows")
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        fail("request exceeds size limit")
    req = json.loads(raw.decode("utf-8"))
    candidate_root, _binary = verify_request(req)
    evidence_dir = None
    if os.environ.get("AUTOFORGE_HOST_WITNESS_DIR"):
        evidence_dir = Path(os.environ["AUTOFORGE_HOST_WITNESS_DIR"]).resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="autoforge-host-witness-"))
    hwnd = None
    try:
        _write_guest_control(temp, req)
        wsb = _build_wsb(temp, candidate_root)
        before = set(_sandbox_windows())
        if before:
            fail("an existing Windows Sandbox window is active; close it before running the host witness")
        sandbox = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
        if not sandbox.is_file():
            fail("Windows Sandbox client is absent")
        subprocess.Popen([str(sandbox), str(wsb)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        hwnd = _wait_window(before, min(90, req["timeout_seconds"] + 45))
        _wait_ui(hwnd, req["target"], min(90, req["timeout_seconds"] + 45), temp, evidence_dir)
        scenarios = []
        for index, scenario in enumerate(req["scenarios"]):
            scenarios.append(_run_scenario(hwnd, scenario, temp, evidence_dir, index))
        cleanup = False
        (temp / "stop").write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not win32gui.IsWindow(hwnd):
                cleanup = True
                break
            time.sleep(0.3)
        if not cleanup:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
            time.sleep(2)
            cleanup = not win32gui.IsWindow(hwnd)
        if not cleanup:
            fail("Sandbox window did not close after guest shutdown")
        report = {
            "schema": 1,
            "run_id": req["run_id"],
            "target": req["target"],
            "candidate_sha256": req["candidate_sha256"],
            "artifact_sha256": req["artifact"]["sha256"],
            "build": "NOT_PERFORMED",
            "install": True,
            "launch": True,
            "sandbox": True,
            "cleanup": True,
            "scenarios": scenarios,
            "limitations": [
                "The guest only launches the portable EXE; assertions and the report are produced by the host screenshot/OCR witness.",
                "Host OCR and input are a witness for the visible UI, not proof against a malicious UI deliberately drawing fake states.",
                "Build from source was not performed; only the manifest-bound portable EXE was launched inside Windows Sandbox.",
            ],
            "evidence_trust": "host_screenshot_ocr_witness",
        }
        sys.stdout.buffer.write(canonical(report) + b"\n")
    finally:
        if hwnd is not None:
            try:
                (temp / "stop").write_text("stop\n", encoding="utf-8")
            except Exception:
                pass
            for _ in range(20):
                if not win32gui.IsWindow(hwnd):
                    break
                time.sleep(0.2)
            if win32gui.IsWindow(hwnd):
                try:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass
            time.sleep(1)
            if win32gui.IsWindow(hwnd):
                try:
                    _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                    win32api.TerminateProcess(win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid), 0)
                except Exception:
                    pass
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write(f"BLOCKED: {exc}\n")
        raise SystemExit(2)
