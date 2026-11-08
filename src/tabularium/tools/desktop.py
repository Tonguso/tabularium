"""The desktop as a tool: the screen, mouse and keyboard of the owner's logged-in session (2026-11-07, Alex: [quote removed]).

The agents run inside the owner's interactive Windows session, so a process of theirs can capture the screen and send
input. What makes this unlike the headless browser is that there is one mouse and one keyboard, shared with the owner
and with anything else driving the desktop, and that everything on screen is reachable, a bank page as much as a
settings dialog. So the tool is gated in code, not by prompt:

- The agent takes the desktop per task. `take` is parked as an exact action and runs only on the owner's yes, like a
  destructive command; his yes records a grant for that chat and starts the turn that uses it.
- The grant ends on `release`, on the owner's next message, after GRANT_SECONDS, and is refused while another agent
  holds one.
- Before every click, keystroke and scroll the foreground window's title is read; a title naming a credential or
  payment page refuses the action. The agent never enters credentials; the title check is the part of that rule that
  code can keep.
- Moving the mouse into a screen corner aborts the action (pyautogui's fail-safe) and releases the grant: the owner
  takes the desktop back by reaching for the mouse.

Coordinates are in the frame of the last screenshot: one monitor, scaled to the model's image budget. The tool maps
them back to the screen.
"""
import base64
import io
import json
import time
from pathlib import Path

from tabularium.core.outcome import Refused, Failed
from tabularium.core.wire_budget import IMAGE_ENCODED_CAP, encoded_size

GRANT_SECONDS = 30 * 60
GRANT_FILE = Path("task_state") / "desktop_grant.json"
DENIED_TITLES = ("password", "passcode", "sign in", "sign-in", "log in", "login", "verification code",
                 "bank", "payment", "checkout", "wallet", "card details")
MAX_EDGE = 1568

TOOL = {
    "name": "desktop",
    "description": (
        "Alex's desktop: screen, mouse and keyboard of his logged-in session. For what has no other way in (a GUI-only "
        "app or dialog); files, settings and programs are usually quicker through execute_command. "
        "take (purpose) asks Alex for the desktop; it runs only on his yes, and his yes starts the turn that uses it. "
        "Then screenshot (screen: 0 is the primary monitor) to see, click (x, y in the last screenshot's frame; "
        "button, double), type (text), key (keys such as 'enter', 'ctrl+s', 'alt+tab'), scroll (x, y, direction, "
        "amount), and release when done. Screenshot after acting to see the result; never act on a stale picture. "
        "The grant ends on release, on Alex's next message, after 30 minutes, or when he moves the mouse into a screen "
        "corner: then stop and talk to him. Never enter passwords, codes, card or account numbers; a window whose title "
        "names a credential or payment page is refused in code. Tell Alex what you did, with the screenshot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["take", "screenshot", "click", "type", "key", "scroll", "release"]},
            "purpose": {"type": "string", "description": "take: what the desktop is for, in one line Alex can say yes to."},
            "screen": {"type": "integer", "description": "screenshot: monitor index, 0 = primary, then left to right."},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right"]},
            "double": {"type": "boolean"},
            "text": {"type": "string"},
            "keys": {"type": "string", "description": "key: one key or a chord joined with '+'."},
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "integer", "description": "scroll: wheel ticks, default 3."},
        },
        "required": ["action"],
    },
}


# ── The machine (replaced in tests) ───────────────────────────────────────────────

def monitors():
    """Monitor rectangles (left, top, right, bottom): the primary first, the rest left to right."""
    import win32api
    rects = [tuple(m[2]) for m in win32api.EnumDisplayMonitors()]
    return sorted(rects, key=lambda r: (not (r[0] <= 0 < r[2] and r[1] <= 0 < r[3]), r[0]))


def grab(rect):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=rect, all_screens=True)


def foreground_title():
    import win32gui
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


def driver():
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.2
    return pyautogui


# ── Grants ────────────────────────────────────────────────────────────────────────

def grant(agent_dir):
    try:
        return json.loads((Path(agent_dir) / GRANT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(agent_dir, value):
    from tabularium.core.agent_runtime import atomic_json
    atomic_json(Path(agent_dir) / GRANT_FILE, value)


def _drop(agent_dir):
    try:
        (Path(agent_dir) / GRANT_FILE).unlink()
    except OSError:
        pass


def holder(root, exclude):
    """The name of another agent whose grant is still live, or ''."""
    now = time.time()
    for path in sorted(Path(root).glob(f"*/{GRANT_FILE.as_posix()}")):
        name = path.parents[1].name
        if name != exclude:
            g = grant(path.parents[1])
            if g and g.get("since", 0) + GRANT_SECONDS > now:
                return name
    return ""


def _lapsed(app, chat, g):
    """Why the grant no longer holds, as a Refused, or None. A lapsed grant is removed."""
    if not g or g.get("chat") != chat:
        return Refused("No desktop grant for this chat. Call desktop take with a purpose; it runs on Alex's yes.")
    if time.time() > g.get("since", 0) + GRANT_SECONDS:
        _drop(app.AGENT_DIR)
        return Refused("The desktop grant expired after 30 minutes. Tell Alex where you got to; take it again if he wants.")
    with app._inbox.db() as db:
        newer = db.execute("SELECT 1 FROM jobs WHERE chat=? AND handler='handle_message' AND id>? LIMIT 1",
                           (chat, g.get("job", 0))).fetchone()
    if newer:
        _drop(app.AGENT_DIR)
        return Refused("Alex sent a message after the grant, so the desktop is released. Answer him; take it again "
                       "if he still wants the work done.")
    return None


# ── Actions ───────────────────────────────────────────────────────────────────────

def execute(app, chat, inputs):
    action = inputs.get("action", "")
    if action == "take":
        return _take(app, chat, inputs)
    g = grant(app.AGENT_DIR)
    lapsed = _lapsed(app, chat, g)
    if lapsed:
        return lapsed
    if action == "release":
        _drop(app.AGENT_DIR)
        return "Desktop released."
    if action == "screenshot":
        return _screenshot(app, g, inputs)
    if action not in ("click", "type", "key", "scroll"):
        return Refused(f"Unknown desktop action '{action}'.")
    title = foreground_title()
    if any(word in title.lower() for word in DENIED_TITLES):
        return Refused(f"Refused: the foreground window is '{title}', which looks like a credential or payment page. "
                       "Alex does those himself; tell him what is on screen and stop.")
    try:
        if action == "click":
            return _click(g, inputs)
        if action == "type":
            return _type(inputs)
        if action == "key":
            return _key(inputs)
        return _scroll(g, inputs)
    except Exception as exc:                                    # pyautogui.FailSafeException among them
        if type(exc).__name__ == "FailSafeException":
            _drop(app.AGENT_DIR)
            return Refused("The mouse is in a screen corner: Alex took the desktop back. The grant is released; "
                           "tell him where you got to.")
        return Failed(f"Desktop {action} error: {exc}")


def _take(app, chat, inputs):
    if app.BACKGROUND_TASK_ID:
        return Refused("The desktop is taken in the foreground only, on Alex's yes; a background task has no one to ask. "
                       "Report what needs doing on the screen.")
    purpose = " ".join((inputs.get("purpose") or "").split())
    if not purpose:
        return Refused("Say what the desktop is for: purpose.")
    other = holder(app.AGENT_DIR.parent, app.AGENT_DIR.name)
    if other:
        return Refused(f"{other} holds the desktop; one driver at a time. Try later or ask Alex.")
    if not app._approved("desktop", action="take", purpose=purpose):
        return app._park(chat, "desktop", {"action": "take", "purpose": purpose},
                         f"Take the desktop (screen, mouse and keyboard) for: {purpose}\n"
                         "I stop on your next message; move the mouse into a screen corner to take it back at once.")
    with app._inbox.db() as db:
        job = db.execute("SELECT COALESCE(MAX(id),0) FROM jobs WHERE chat=?", (chat,)).fetchone()[0]
    _save(app.AGENT_DIR, {"chat": chat, "job": int(job), "purpose": purpose, "since": time.time()})
    return f"Desktop taken for: {purpose}. Take a screenshot first; coordinates are in its frame."


def _screenshot(app, g, inputs):
    rects = monitors()
    index = int(inputs.get("screen") or 0)
    if not 0 <= index < len(rects):
        return Refused(f"screen must be 0..{len(rects) - 1} ({len(rects)} monitor(s)).")
    rect = rects[index]
    image = grab(rect).convert("RGB")
    width, height = image.size
    scale = min(1.0, MAX_EDGE / max(width, height))
    while True:                                                  # the wire budget, like view_image
        shown = image.resize((max(1, round(width * scale)), max(1, round(height * scale))))
        buffer = io.BytesIO()
        shown.save(buffer, format="JPEG", quality=85)
        payload = buffer.getvalue()
        if encoded_size(len(payload)) <= IMAGE_ENCODED_CAP or scale < 0.2:
            break
        scale *= 0.8
    g["frame"] = {"rect": list(rect), "scale": scale}
    _save(app.AGENT_DIR, g)
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(payload).decode()}},
        {"type": "text", "text": f"[Screen {index} of {len(rects)}, {width}x{height}, shown at {shown.size[0]}x{shown.size[1]}. "
                                 f"Give coordinates in this image's frame. Foreground window: '{foreground_title()}'.]"},
    ]


def _point(g, inputs):
    frame = g.get("frame")
    if not frame:
        raise ValueError("take a screenshot first; coordinates are in its frame")
    x, y = inputs.get("x"), inputs.get("y")
    if x is None or y is None:
        raise ValueError("x and y are required")
    left, top = frame["rect"][0], frame["rect"][1]
    return round(left + int(x) / frame["scale"]), round(top + int(y) / frame["scale"])


def _click(g, inputs):
    try:
        sx, sy = _point(g, inputs)
    except ValueError as exc:
        return Refused(f"click: {exc}.")
    driver().click(sx, sy, button=inputs.get("button") or "left", clicks=2 if inputs.get("double") else 1)
    return f"Clicked ({inputs['x']}, {inputs['y']}). Foreground window: '{foreground_title()}'. Screenshot to see the result."


def _type(inputs):
    text = inputs.get("text") or ""
    if not text:
        return Refused("type: text is required.")
    d = driver()
    if text.isascii():
        d.write(text)
    else:                                                        # pyautogui types ASCII only; paste the rest
        import pyperclip
        pyperclip.copy(text)
        d.hotkey("ctrl", "v")
    return f"Typed {len(text)} characters. Screenshot to see the result."


def _key(inputs):
    keys = [k.strip().lower() for k in (inputs.get("keys") or "").split("+") if k.strip()]
    if not keys:
        return Refused("key: keys is required, e.g. 'enter' or 'ctrl+s'.")
    driver().hotkey(*keys)
    return f"Pressed {'+'.join(keys)}. Screenshot to see the result."


def _scroll(g, inputs):
    try:
        sx, sy = _point(g, inputs)
    except ValueError as exc:
        return Refused(f"scroll: {exc}.")
    ticks = int(inputs.get("amount") or 3)
    driver().scroll(ticks if (inputs.get("direction") or "down") == "up" else -ticks, sx, sy)
    return f"Scrolled {inputs.get('direction') or 'down'} {ticks} at ({inputs['x']}, {inputs['y']}). Screenshot to see the result."
