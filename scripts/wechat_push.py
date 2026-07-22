#!/usr/bin/env python3
"""Guarded Windows desktop delivery for soccer-predict lineup checks.

The script deliberately refuses to send unless a previously confirmed WeChat
conversation matches two local visual signatures. It never falls back to a
partial contact-name match and records an event key before clicking Send so a
failed run cannot be retried blindly.
"""

from __future__ import annotations

import argparse
import atexit
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat


WINDOW_TITLE = "Weixin"
WINDOW_CLASS = "Qt51514QWindowIcon"
# Capture only the stable name text plus a group-toolbar marker. Do not include
# a member count or mosaic avatar because both may change when members are added.
HEADER_CROP = (318, 35, 355, 75)
IDENTITY_CROP = (675, 30, 716, 80)
SEARCH_POINT = (160, 55)
INPUT_X = 430
INPUT_BOTTOM_OFFSET = 126
SEND_RIGHT_OFFSET = 52
SEND_BOTTOM_OFFSET = 42
DEFAULT_MAX_RMS = 8.0
EVENT_RE = re.compile(r"^(?:initial:\d+|lineup-check:\d+|test:[A-Za-z0-9._-]+)$")


if os.name == "nt":
    USER32 = ctypes.windll.user32
    KERNEL32 = ctypes.windll.kernel32
    OLE32 = ctypes.OleDLL("ole32")
    KERNEL32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    KERNEL32.GlobalAlloc.restype = ctypes.c_void_p
    KERNEL32.GlobalLock.argtypes = (ctypes.c_void_p,)
    KERNEL32.GlobalLock.restype = ctypes.c_void_p
    KERNEL32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    KERNEL32.GlobalFree.argtypes = (ctypes.c_void_p,)
    USER32.OpenClipboard.argtypes = (wintypes.HWND,)
    USER32.EmptyClipboard.restype = wintypes.BOOL
    USER32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
    USER32.SetClipboardData.restype = ctypes.c_void_p
    USER32.CloseClipboard.restype = wintypes.BOOL
    OLE32.OleInitialize.argtypes = (ctypes.c_void_p,)
    OLE32.OleInitialize.restype = ctypes.c_long
    OLE32.OleGetClipboard.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
    OLE32.OleGetClipboard.restype = ctypes.c_long
    OLE32.OleSetClipboard.argtypes = (ctypes.c_void_p,)
    OLE32.OleSetClipboard.restype = ctypes.c_long
    OLE32.OleFlushClipboard.restype = ctypes.c_long
    try:
        USER32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            USER32.SetProcessDPIAware()
        except Exception:
            pass
else:
    USER32 = None
    KERNEL32 = None
    OLE32 = None


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


ULONG_PTR = wintypes.WPARAM


class KeybdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeybdInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]


def normalize_message(message: str) -> str:
    """Create a compact plain-text WeChat summary with deliberate line breaks."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in message.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    normalized = "\n".join(lines)
    if not normalized:
        raise ValueError("message is empty")
    if len(normalized) > 1200:
        raise ValueError("message exceeds the 1200-character safety limit")
    if len(lines) > 16:
        raise ValueError("message exceeds the 16-line safety limit")
    if re.search(r"(?:^|\n)\s*(?:#{1,6}\s|[-*+]\s|```|<\/?(?:html|table|div|p)\b)", normalized, re.I):
        raise ValueError("message must use plain WeChat text, not Markdown or HTML")
    return normalized


def validate_event_message(event_key: str, message: str) -> str:
    if not EVENT_RE.fullmatch(event_key):
        raise ValueError("event key must be initial:<match_id>, lineup-check:<match_id>, or test:<token>")
    normalized = normalize_message(message)
    if event_key.startswith("initial:"):
        match_id = event_key.split(":", 1)[1]
        if not normalized.startswith(f"【初盘分析｜{match_id}】\n"):
            raise ValueError(f"initial message must start with 【初盘分析｜{match_id}】")
        required = ("比赛：", "开赛：", "主推：", "比分参考：")
        if any(field not in normalized for field in required):
            raise ValueError("initial message is missing a required plain-text field")
    elif event_key.startswith("lineup-check:"):
        match_id = event_key.split(":", 1)[1]
        if not normalized.startswith(f"【临场分析｜{match_id}】\n"):
            raise ValueError(f"lineup message must start with 【临场分析｜{match_id}】")
        required = ("比赛：", "检查时间：", "比赛状态：", "主推：", "比分参考：")
        if any(field not in normalized for field in required):
            raise ValueError("lineup message is missing a required plain-text field")
        if "主推维持：" not in normalized and "主推变更：" not in normalized:
            raise ValueError("lineup message must state 主推维持 or 主推变更")
    elif "测试" not in normalized:
        raise ValueError("test messages must contain 测试")
    return normalized


def image_rms(left: Image.Image, right: Image.Image) -> float:
    if left.size != right.size:
        return math.inf
    diff = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    values = ImageStat.Stat(diff).rms
    return math.sqrt(sum(value * value for value in values) / len(values))


def green_fraction(image: Image.Image, point: tuple[int, int]) -> float:
    x, y = point
    sample = image.convert("RGB").crop((x - 20, y - 10, x + 20, y + 10))
    pixels = list(sample.get_flattened_data())
    if not pixels:
        return 0.0
    green = sum(1 for r, g, b in pixels if g > r + 30 and g > b + 20 and g > 100)
    return green / len(pixels)


def _require_windows() -> None:
    if USER32 is None:
        raise RuntimeError("WeChat desktop delivery is supported only on Windows")


def _window_text(hwnd: int) -> str:
    length = USER32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    USER32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def find_wechat_window(title: str, class_name: str) -> int:
    _require_windows()
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if USER32.IsWindowVisible(hwnd):
            if _window_text(hwnd) == title and _class_name(hwnd) == class_name:
                matches.append(int(hwnd))
        return True

    if not USER32.EnumWindows(callback, 0):
        raise RuntimeError("could not enumerate desktop windows")
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one visible WeChat window, found {len(matches)}")
    return matches[0]


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = Rect()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("could not read WeChat window bounds")
    if rect.right - rect.left < 650 or rect.bottom - rect.top < 650:
        raise RuntimeError("WeChat window is too small for guarded delivery")
    return rect.left, rect.top, rect.right, rect.bottom


def focus_window(hwnd: int) -> None:
    USER32.ShowWindow(hwnd, 9)
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.45)
    if int(USER32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("WeChat could not be brought to the foreground")


def capture_window(hwnd: int) -> Image.Image:
    return ImageGrab.grab(bbox=window_rect(hwnd), all_screens=True)


def restore_topmost(hwnd: int, was_topmost: bool) -> None:
    if USER32 is not None and not was_topmost:
        USER32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def make_temporarily_topmost(hwnd: int) -> bool:
    ex_style = USER32.GetWindowLongW(hwnd, -20)
    was_topmost = bool(ex_style & 0x00000008)
    if not was_topmost:
        if not USER32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010):
            raise RuntimeError("could not protect the WeChat window from visual occlusion")
    atexit.register(restore_topmost, hwnd, was_topmost)
    return was_topmost


def _click(x: int, y: int) -> None:
    if not USER32.SetCursorPos(x, y):
        raise RuntimeError("could not position the pointer")
    USER32.mouse_event(0x0002, 0, 0, 0, 0)
    USER32.mouse_event(0x0004, 0, 0, 0, 0)


def _tap_vk(vk: int) -> None:
    USER32.keybd_event(vk, 0, 0, 0)
    USER32.keybd_event(vk, 0, 0x0002, 0)


def _ctrl_a() -> None:
    USER32.keybd_event(0x11, 0, 0, 0)
    _tap_vk(0x41)
    USER32.keybd_event(0x11, 0, 0x0002, 0)


def _ctrl_v() -> None:
    USER32.keybd_event(0x11, 0, 0, 0)
    _tap_vk(0x56)
    USER32.keybd_event(0x11, 0, 0x0002, 0)


def _shift_enter() -> None:
    USER32.keybd_event(0x10, 0, 0, 0)
    _tap_vk(0x0D)
    USER32.keybd_event(0x10, 0, 0x0002, 0)


def _unicode_text(value: str, expected_hwnd: int | None = None) -> None:
    encoded = value.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        if expected_hwnd is not None and int(USER32.GetForegroundWindow()) != int(expected_hwnd):
            raise RuntimeError("WeChat lost foreground focus during message input")
        unit = int.from_bytes(encoded[index : index + 2], "little")
        down = Input(type=1, ki=KeybdInput(0, unit, 0x0004, 0, 0))
        up = Input(type=1, ki=KeybdInput(0, unit, 0x0004 | 0x0002, 0, 0))
        if USER32.SendInput(1, ctypes.byref(down), ctypes.sizeof(Input)) != 1:
            raise RuntimeError("unicode keyboard input failed")
        if USER32.SendInput(1, ctypes.byref(up), ctypes.sizeof(Input)) != 1:
            raise RuntimeError("unicode keyboard input failed")
        time.sleep(0.001)


def _type_message(message: str, hwnd: int) -> None:
    lines = message.split("\n")
    for index, line in enumerate(lines):
        _unicode_text(line, expected_hwnd=hwnd)
        if index + 1 < len(lines):
            if int(USER32.GetForegroundWindow()) != int(hwnd):
                raise RuntimeError("WeChat lost foreground focus before a line break")
            _shift_enter()
            time.sleep(0.02)


def _open_clipboard() -> None:
    for _attempt in range(20):
        if USER32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise RuntimeError("Windows clipboard is busy")


def _set_clipboard_text(value: str) -> None:
    encoded = value.encode("utf-16-le") + b"\x00\x00"
    handle = KERNEL32.GlobalAlloc(0x0002, len(encoded))
    if not handle:
        raise RuntimeError("could not allocate clipboard memory")
    pointer = KERNEL32.GlobalLock(handle)
    if not pointer:
        KERNEL32.GlobalFree(handle)
        raise RuntimeError("could not lock clipboard memory")
    ctypes.memmove(pointer, encoded, len(encoded))
    KERNEL32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        if not USER32.EmptyClipboard():
            raise RuntimeError("could not clear temporary clipboard state")
        if not USER32.SetClipboardData(13, handle):
            raise RuntimeError("could not set temporary Unicode clipboard text")
        handle = None  # Clipboard now owns the allocation.
    finally:
        USER32.CloseClipboard()
        if handle:
            KERNEL32.GlobalFree(handle)


def _ole_get_clipboard_with_retry() -> ctypes.c_void_p:
    last_error: Exception | None = None
    for _attempt in range(40):
        pointer = ctypes.c_void_p()
        try:
            result = OLE32.OleGetClipboard(ctypes.byref(pointer))
            if result >= 0:
                return pointer
        except OSError as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"could not snapshot the existing clipboard after retries: {last_error}")


def _ole_restore_clipboard_with_retry(original: ctypes.c_void_p) -> None:
    last_error: Exception | None = None
    for _attempt in range(40):
        try:
            restore_result = OLE32.OleSetClipboard(original if original.value else None)
            if restore_result >= 0:
                if original.value:
                    flush_result = OLE32.OleFlushClipboard()
                    if flush_result < 0:
                        raise RuntimeError(f"could not flush restored clipboard (HRESULT={flush_result})")
                return
        except (OSError, RuntimeError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"could not restore the clipboard after retries: {last_error}")


@contextmanager
def _preserved_clipboard():
    OLE32.OleInitialize(None)
    original = _ole_get_clipboard_with_retry()
    try:
        yield
    finally:
        try:
            _ole_restore_clipboard_with_retry(original)
        finally:
            # Deliberately do not invoke the IDataObject vtable directly. COM
            # and process teardown own this short-lived reference.
            OLE32.OleUninitialize()


def _paste_message(message: str, hwnd: int) -> None:
    _set_clipboard_text(message)
    if int(USER32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("WeChat lost foreground focus before paste")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('^v')",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        creationflags=0x08000000,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Windows SendKeys paste helper failed")
    # WeChat 4.x reads the clipboard asynchronously through its custom editor.
    # Keep the temporary text available long enough before restoring the user's
    # original clipboard object.
    time.sleep(2.5)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_crop(image: Image.Image, box: list[int] | tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = (int(value) for value in box)
    if left < 0 or top < 0 or right > image.width or bottom > image.height:
        raise RuntimeError("configured visual-signature crop is outside the WeChat window")
    return image.crop((left, top, right, bottom))


def _chat_name(args: argparse.Namespace, confirmed: bool = False) -> str:
    prefix = "confirmed_" if confirmed else ""
    plain = getattr(args, f"{prefix}chat_name", None)
    encoded = getattr(args, f"{prefix}chat_name_b64", None)
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except Exception as exc:
            raise ValueError("chat-name Base64 is not valid UTF-8") from exc
    if plain:
        return str(plain)
    raise ValueError("an exact chat name is required")


def _message(args: argparse.Namespace) -> str:
    if getattr(args, "message_b64", None):
        try:
            return base64.b64decode(args.message_b64, validate=True).decode("utf-8")
        except Exception as exc:
            raise ValueError("message Base64 is not valid UTF-8") from exc
    if getattr(args, "message", None):
        return str(args.message)
    raise ValueError("a message is required")


def configure(args: argparse.Namespace) -> dict[str, Any]:
    chat_name = _chat_name(args)
    confirmed_chat_name = _chat_name(args, confirmed=True)
    if chat_name != confirmed_chat_name:
        raise ValueError("the confirmed chat name must exactly match the chat name")
    config_path = Path(args.config).resolve()
    hwnd = find_wechat_window(WINDOW_TITLE, WINDOW_CLASS)
    make_temporarily_topmost(hwnd)
    focus_window(hwnd)
    screenshot = capture_window(hwnd)
    header_path = config_path.with_name("wechat_target_header.png")
    identity_path = config_path.with_name("wechat_target_identity.png")
    proof_path = config_path.with_name("wechat_target_confirmed.png")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _relative_crop(screenshot, HEADER_CROP).save(header_path)
    _relative_crop(screenshot, IDENTITY_CROP).save(identity_path)
    screenshot.save(proof_path)
    config = {
        "version": 1,
        "enabled": True,
        "chat_name": chat_name,
        "window_title": WINDOW_TITLE,
        "window_class": WINDOW_CLASS,
        "header_crop": list(HEADER_CROP),
        "identity_crop": list(IDENTITY_CROP),
        "header_template": header_path.name,
        "identity_template": identity_path.name,
        "max_rms": float(args.max_rms),
        "configured_proof": proof_path.name,
        "configured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "events": ["initial", "lineup-check"],
        "format": "wechat-plain-text-v1",
    }
    _write_json(config_path, config)
    return {
        "configured": True,
        "enabled": True,
        "chat_name": chat_name,
        "config": str(config_path),
        "proof": str(proof_path),
    }


def _search_chat(hwnd: int, chat_name: str) -> Image.Image:
    focus_window(hwnd)
    left, top, _right, _bottom = window_rect(hwnd)
    _click(left + SEARCH_POINT[0], top + SEARCH_POINT[1])
    time.sleep(0.15)
    if int(USER32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("WeChat lost foreground focus before chat search")
    _ctrl_a()
    _unicode_text(chat_name)
    time.sleep(0.8)
    if int(USER32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("WeChat lost foreground focus during chat search")
    _tap_vk(0x0D)
    time.sleep(0.8)
    # New WeChat opens a transient WebView search window. Escape closes it after
    # Enter has selected the result; then return focus to the verified main UI.
    _tap_vk(0x1B)
    time.sleep(0.35)
    focus_window(hwnd)
    return capture_window(hwnd)


def inspect_target(args: argparse.Namespace) -> dict[str, Any]:
    hwnd = find_wechat_window(WINDOW_TITLE, WINDOW_CLASS)
    make_temporarily_topmost(hwnd)
    chat_name = _chat_name(args)
    screenshot = _search_chat(hwnd, chat_name)
    proof_path = Path(args.proof).resolve()
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot.save(proof_path)
    return {
        "sent": False,
        "inspection_only": True,
        "searched_name": chat_name,
        "proof": str(proof_path),
    }


def clear_configured_draft(args: argparse.Namespace) -> dict[str, Any]:
    if not EVENT_RE.fullmatch(args.confirmed_event_key):
        raise ValueError("a valid confirmed event key is required")
    config_path = Path(args.config).resolve()
    config = _read_json(config_path)
    hwnd = find_wechat_window(str(config["window_title"]), str(config["window_class"]))
    make_temporarily_topmost(hwnd)
    verified, scores = _select_and_verify(hwnd, config, config_path)
    width, height = verified.size
    send_point = (width - SEND_RIGHT_OFFSET, height - SEND_BOTTOM_OFFSET)
    if green_fraction(verified, send_point) < 0.10:
        return {"draft_cleared": False, "already_empty": True, "scores": scores}
    left, _top, _right, bottom = window_rect(hwnd)
    focus_window(hwnd)
    _click(left + INPUT_X, bottom - INPUT_BOTTOM_OFFSET)
    time.sleep(0.2)
    _ctrl_a()
    _tap_vk(0x08)
    time.sleep(0.6)
    proof = capture_window(hwnd)
    proof_path = config_path.with_name("wechat_draft_cleared_operator.png")
    proof.save(proof_path)
    if green_fraction(proof, send_point) >= 0.10:
        raise RuntimeError("configured draft could not be cleared")
    return {
        "draft_cleared": True,
        "confirmed_event_key": args.confirmed_event_key,
        "scores": scores,
        "proof": str(proof_path),
    }


def _select_and_verify(hwnd: int, config: dict[str, Any], config_path: Path) -> tuple[Image.Image, dict[str, float]]:
    screenshot = _search_chat(hwnd, str(config["chat_name"]))
    header_template = Image.open(config_path.with_name(config["header_template"]))
    identity_template = Image.open(config_path.with_name(config["identity_template"]))
    scores = {
        "header_rms": image_rms(_relative_crop(screenshot, config["header_crop"]), header_template),
        "identity_rms": image_rms(_relative_crop(screenshot, config["identity_crop"]), identity_template),
    }
    threshold = float(config.get("max_rms", DEFAULT_MAX_RMS))
    if scores["header_rms"] > threshold or scores["identity_rms"] > threshold:
        raise RuntimeError(
            "exact WeChat target verification failed "
            f"(header_rms={scores['header_rms']:.2f}, identity_rms={scores['identity_rms']:.2f}, max={threshold:.2f})"
        )
    return screenshot, scores


def _editor_text(value: str) -> str:
    """Normalize the line endings exposed by the WeChat UIA editor."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def prepare_rpa_editor(editor: Any, message: str, adopt_sha256: str | None = None) -> bool:
    """Stage an exact message in a UIA editor without clicking Send.

    Returns True only when an operator-confirmed existing draft was adopted.
    This helper is deliberately independent from pywinauto so its safety rules
    can be unit tested with a small fake editor.
    """
    current = _editor_text(str(editor.get_value()))
    expected_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if current:
        if adopt_sha256 != expected_hash or current != message:
            raise RuntimeError("target conversation contains an unsent draft; refusing to alter or append to it")
        return True
    editor.set_edit_text(message)
    staged = _editor_text(str(editor.get_value()))
    if staged != message:
        raise RuntimeError("WeChat UIA editor did not confirm the exact message draft; nothing was sent")
    return False


def clear_rpa_editor(editor: Any, expected_message: str) -> None:
    current = _editor_text(str(editor.get_value()))
    if current != expected_message:
        raise RuntimeError("WeChat draft changed during verification; refusing to clear unknown content")
    editor.set_edit_text("")
    if str(editor.get_value()):
        raise RuntimeError("WeChat UIA editor could not confirm draft cleanup")


def _open_rpa_editor(hwnd: int) -> tuple[Any, Any]:
    """Return the current WeChat editor and pyautogui after accessibility checks."""
    try:
        import pyautogui
        from pywinauto import Desktop
        from pyweixin.Uielements import Edits
    except ImportError as exc:
        raise RuntimeError(
            "pyweixin RPA is not installed; run: python -m pip install pywechat127"
        ) from exc

    main_window = Desktop(backend="uia").window(handle=hwnd)
    try:
        detected_class = main_window.class_name()
    except Exception as exc:
        raise RuntimeError("WeChat accessibility interface is unavailable") from exc
    if detected_class != "mmui::MainWindow":
        raise RuntimeError(
            "WeChat accessibility UI tree is hidden for this account/version; current WeChat 4.1+ "
            "does not restore it through Windows Narrator. Do not restart or retry unattended delivery"
        )
    edit_spec = main_window.child_window(**Edits.CurrentChatEdit)
    if not edit_spec.exists(timeout=3):
        raise RuntimeError("verified WeChat chat input was not exposed through accessibility UIA")
    return edit_spec.wrapper_object(), pyautogui


def _send_with_rpa(
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    state_path: Path,
    state: dict[str, Any],
    hwnd: int,
    verified: Image.Image,
    scores: dict[str, float],
    message: str,
) -> dict[str, Any]:
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", args.event_key)
    editor, pyautogui = _open_rpa_editor(hwnd)
    adopted = prepare_rpa_editor(editor, message, args.adopt_existing_draft_sha256)

    draft_path = config_path.with_name(f"wechat_rpa_draft_{safe_key}.png")
    capture_window(hwnd).save(draft_path)
    if args.verify_draft_only:
        clear_rpa_editor(editor, message)
        cleared_path = config_path.with_name(f"wechat_rpa_draft_cleared_{safe_key}.png")
        capture_window(hwnd).save(cleared_path)
        return {
            "sent": False,
            "draft_verified": True,
            "draft_cleared": True,
            "backend": "pyweixin-uia",
            "chat_name": config["chat_name"],
            "event_key": args.event_key,
            "proof": str(cleared_path),
        }

    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    attempted_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["deliveries"][args.event_key] = {
        "status": "attempting",
        "chat_name": config["chat_name"],
        "message_sha256": message_hash,
        "adopted_verified_draft": adopted,
        "backend": "pyweixin-uia",
        "updated_at": attempted_at,
    }
    _write_json(state_path, state)

    focus_window(hwnd)
    editor.click_input()
    pyautogui.hotkey("alt", "s", _pause=False)
    time.sleep(1.0)
    if str(editor.get_value()):
        raise RuntimeError("WeChat send was invoked but the editor did not clear; retry is blocked")

    proof = capture_window(hwnd)
    proof_path = config_path.with_name(f"wechat_sent_{safe_key}.png")
    proof.save(proof_path)
    sent_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["deliveries"][args.event_key].update(
        {"status": "sent", "updated_at": sent_at, "proof": proof_path.name}
    )
    _write_json(state_path, state)
    return {
        "sent": True,
        "target_verified": True,
        "backend": "pyweixin-uia",
        "chat_name": config["chat_name"],
        "event_key": args.event_key,
        "sent_at": sent_at,
        "adopted_verified_draft": adopted,
        "scores": scores,
        "proof": str(proof_path),
    }


def send(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    config = _read_json(config_path)
    if not config.get("enabled"):
        raise RuntimeError("WeChat delivery is disabled in the workspace configuration")
    message = validate_event_message(args.event_key, _message(args))
    state_path = config_path.with_name("wechat_push_state.json")
    state = _read_json(state_path) if state_path.exists() else {"version": 1, "deliveries": {}}
    previous = state.setdefault("deliveries", {}).get(args.event_key)
    if previous:
        return {
            "sent": False,
            "duplicate_blocked": True,
            "event_key": args.event_key,
            "previous_status": previous.get("status"),
            "previous_time": previous.get("updated_at"),
        }

    hwnd = find_wechat_window(str(config["window_title"]), str(config["window_class"]))
    make_temporarily_topmost(hwnd)
    verified, scores = _select_and_verify(hwnd, config, config_path)
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", args.event_key)
    verified_path = config_path.with_name(f"wechat_verified_{safe_key}.png")
    verified.save(verified_path)

    if not args.send and not args.verify_draft_only:
        return {
            "sent": False,
            "dry_run": True,
            "target_verified": True,
            "chat_name": config["chat_name"],
            "event_key": args.event_key,
            "scores": scores,
            "proof": str(verified_path),
        }

    if args.backend == "rpa":
        return _send_with_rpa(
            args,
            config,
            config_path,
            state_path,
            state,
            hwnd,
            verified,
            scores,
            message,
        )

    width, height = verified.size
    send_point = (width - SEND_RIGHT_OFFSET, height - SEND_BOTTOM_OFFSET)
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    existing_draft = green_fraction(verified, send_point) >= 0.10
    adopt_existing = False
    if existing_draft:
        if args.verify_draft_only:
            raise RuntimeError("draft verification requires an empty target input box")
        if args.adopt_existing_draft_sha256 != message_hash:
            raise RuntimeError("target conversation contains an unsent draft; refusing to alter or append to it")
        adopt_existing = True

    left, top, _right, bottom = window_rect(hwnd)
    if not adopt_existing:
        try:
            # Snapshot the clipboard before the WeChat editor receives focus;
            # this avoids the editor and IME competing for the clipboard lock.
            with _preserved_clipboard():
                focus_window(hwnd)
                _click(left + INPUT_X, bottom - INPUT_BOTTOM_OFFSET)
                time.sleep(0.2)
                if int(USER32.GetForegroundWindow()) != int(hwnd):
                    raise RuntimeError("WeChat input box did not retain foreground focus")
                _paste_message(message, hwnd)
        except Exception:
            focus_window(hwnd)
            _click(left + INPUT_X, bottom - INPUT_BOTTOM_OFFSET)
            _ctrl_a()
            _tap_vk(0x08)
            raise
        time.sleep(1.0)
        drafted = capture_window(hwnd)
        draft_path = config_path.with_name(f"wechat_draft_{safe_key}.png")
        drafted.save(draft_path)
        draft_green = green_fraction(drafted, send_point)
        if draft_green < 0.50:
            time.sleep(0.8)
            drafted = capture_window(hwnd)
            drafted.save(draft_path)
            draft_green = green_fraction(drafted, send_point)
        if draft_green < 0.50:
            focus_window(hwnd)
            _click(left + INPUT_X, bottom - INPUT_BOTTOM_OFFSET)
            _ctrl_a()
            _tap_vk(0x08)
            raise RuntimeError(
                f"message draft was not confirmed (green_fraction={draft_green:.3f}); nothing was sent; proof={draft_path}"
            )

    if args.verify_draft_only:
        focus_window(hwnd)
        _click(left + INPUT_X, bottom - INPUT_BOTTOM_OFFSET)
        _ctrl_a()
        _tap_vk(0x08)
        time.sleep(0.5)
        cleared = capture_window(hwnd)
        cleared_path = config_path.with_name(f"wechat_draft_cleared_{safe_key}.png")
        cleared.save(cleared_path)
        if green_fraction(cleared, send_point) >= 0.10:
            raise RuntimeError("draft-only verification could not confirm cleanup")
        return {
            "sent": False,
            "draft_verified": True,
            "draft_cleared": True,
            "chat_name": config["chat_name"],
            "event_key": args.event_key,
            "proof": str(cleared_path),
        }

    attempted_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["deliveries"][args.event_key] = {
        "status": "attempting",
        "chat_name": config["chat_name"],
        "message_sha256": message_hash,
        "adopted_verified_draft": adopt_existing,
        "updated_at": attempted_at,
    }
    _write_json(state_path, state)

    _click(left + send_point[0], top + send_point[1])
    time.sleep(0.9)
    proof = capture_window(hwnd)
    proof_path = config_path.with_name(f"wechat_sent_{safe_key}.png")
    proof.save(proof_path)
    if green_fraction(proof, send_point) >= 0.10:
        raise RuntimeError("Send was clicked but delivery could not be confirmed; retry is blocked")

    sent_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["deliveries"][args.event_key].update(
        {"status": "sent", "updated_at": sent_at, "proof": proof_path.name}
    )
    _write_json(state_path, state)
    return {
        "sent": True,
        "target_verified": True,
        "chat_name": config["chat_name"],
        "event_key": args.event_key,
        "sent_at": sent_at,
        "adopted_verified_draft": adopt_existing,
        "scores": scores,
        "proof": str(proof_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("configure", help="capture a visually confirmed exact WeChat target")
    setup.add_argument("--config", required=True)
    setup_name = setup.add_mutually_exclusive_group(required=True)
    setup_name.add_argument("--chat-name")
    setup_name.add_argument("--chat-name-b64")
    setup_confirmed = setup.add_mutually_exclusive_group(required=True)
    setup_confirmed.add_argument("--confirmed-chat-name")
    setup_confirmed.add_argument("--confirmed-chat-name-b64")
    setup.add_argument("--max-rms", type=float, default=DEFAULT_MAX_RMS)

    inspect = sub.add_parser("inspect", help="select a chat by name and save a proof without configuring or sending")
    inspect_name = inspect.add_mutually_exclusive_group(required=True)
    inspect_name.add_argument("--chat-name")
    inspect_name.add_argument("--chat-name-b64")
    inspect.add_argument("--proof", required=True)

    clear = sub.add_parser("clear-draft", help="clear an operator-confirmed draft from the configured target")
    clear.add_argument("--config", required=True)
    clear.add_argument("--confirmed-event-key", required=True)

    delivery = sub.add_parser("send", help="verify the configured target and optionally send once")
    delivery.add_argument("--config", required=True)
    delivery.add_argument("--event-key", required=True)
    delivery_message = delivery.add_mutually_exclusive_group(required=True)
    delivery_message.add_argument("--message")
    delivery_message.add_argument("--message-b64")
    delivery.add_argument("--send", action="store_true", help="commit the external send after verification")
    delivery.add_argument(
        "--backend",
        choices=("rpa", "legacy"),
        default="rpa",
        help="use guarded pyweixin UIA by default; legacy is retained only for operator diagnostics",
    )
    delivery.add_argument(
        "--verify-draft-only",
        action="store_true",
        help="exercise paste and cleanup without clicking Send or recording a delivery",
    )
    delivery.add_argument(
        "--adopt-existing-draft-sha256",
        help="commit an operator-verified existing draft only when this equals the expected message SHA-256",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            result = configure(args)
        elif args.command == "inspect":
            result = inspect_target(args)
        elif args.command == "clear-draft":
            result = clear_configured_draft(args)
        else:
            result = send(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
