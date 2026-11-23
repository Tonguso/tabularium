"""The tools that make or deliver a file: photos with marks, generated and edited images, documents, and the
independent review of a finished output.
"""
from tabularium.paths import tool_env
import base64
import io
import subprocess
import sys
from pathlib import Path
import requests
from tabularium.core.outcome import Failed


def mark_image(data, marks, crop=None):
    """The picture with the question drawn on it: red boxes with labels in the original file's pixels, then an
    optional crop (2026-11-08: a shape question goes out as a marked picture, not as a numbered list of geometry)."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.open(io.BytesIO(data)).convert("RGB")
    draw = ImageDraw.Draw(image)
    stroke = max(2, min(image.size) // 300)
    try:
        font = ImageFont.load_default(size=max(14, min(image.size) // 40))
    except (TypeError, OSError):
        font = ImageFont.load_default()
    for m in marks:
        x, y, w, h = int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"])
        draw.rectangle([x, y, x + w, y + h], outline=(230, 30, 30), width=stroke)
        label = str(m.get("label") or "").strip()
        if label:
            left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
            tw, th = right - left, bottom - top
            ly = y - th - 2 * stroke - 4 if y - th - 2 * stroke - 4 >= 0 else y + h + stroke
            draw.rectangle([x, ly, x + tw + 2 * stroke + 4, ly + th + 2 * stroke + 4], fill=(230, 30, 30))
            draw.text((x + stroke + 2 - left, ly + stroke + 2 - top), label, fill="white", font=font)
    if crop:
        x, y, w, h = (int(v) for v in crop)
        image = image.crop((max(0, x), max(0, y), min(image.width, x + w), min(image.height, y + h)))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class MediaTools:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    IMAGE_MODEL = "gpt-image-2"

    def tool_send_photo(self, chat_id: int, url: str = "", path: str = "", caption: str = "", marks=None, crop=None) -> str:
        try:
            if path:
                p = Path(path)
                if not p.is_file():
                    return f"No file at {path}"
                data, name = p.read_bytes(), p.name
            elif url:
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                data, name = response.content, "image.jpg"
            else:
                return "Give path (a local image) or url."
            if marks or crop:
                data, name = mark_image(data, marks or [], crop), Path(name).stem + "_marked.png"
            image_data = io.BytesIO(data)
            image_data.name = name
            self.bot.send_photo(chat_id, image_data, caption=caption if caption else None)
            return "Photo sent." + (f" {len(marks)} mark(s) drawn." if marks else "")
        except requests.RequestException as e:
            return f"Failed to download image: {e}"
        except Exception as e:
            return f"Failed to send photo: {e}"

    def tool_edit_image(self, prompt: str, chat_id: int, quality: str = "medium") -> str:
        """Edit the last photo Alex sent, per his instruction, and send back the result."""
        if not self.OPENAI_API_KEY:
            return "Image editing needs OPENAI_API_KEY in .env."
        src = self._last_photo.get(chat_id)
        if not src:
            saved = self._images.list(chat_id, limit=1)
            if saved and Path(saved[0]["path"]).is_file():
                src = Path(saved[0]["path"]).read_bytes()
        if not src:
            return "No photo to edit — ask Alex to send the photo first, then say what to change."

        q = (quality or "medium").lower()
        if q not in ("low", "medium", "high"):
            q = "medium"
        try:
            from openai import OpenAI as OAClient
            oa = OAClient(api_key=self.OPENAI_API_KEY, timeout=300.0)
            img = io.BytesIO(src)
            img.name = "input.png"
            r = oa.images.edit(model=self.IMAGE_MODEL, image=img, prompt=prompt, quality=q, n=1)
            b64 = r.data[0].b64_json
            if not b64:
                return "Image API returned no image data."
            data = base64.b64decode(b64)
        except Exception as e:
            return f"Image edit failed: {e}"

        try:
            buf = io.BytesIO(data)
            buf.name = "edited.png"
            self.bot.send_photo(chat_id, buf, caption=prompt[:1000])
            return ("Edited image sent. Do not repeat the prompt back to Alex; he can see it.")
        except Exception as e:
            return f"Edited but failed to send: {e}"

    def tool_generate_image(self, prompt: str, chat_id: int, orientation: str = "square",
                            quality: str = "medium") -> str:
        """Generate an image with OpenAI's image model and send it to Alex on Telegram."""
        if not self.OPENAI_API_KEY:
            return "Image generation needs OPENAI_API_KEY in .env."

        size = {"landscape": "1536x1024", "portrait": "1024x1536"}.get(
            (orientation or "square").lower(), "1024x1024")
        q = (quality or "medium").lower()
        if q not in ("low", "medium", "high"):
            q = "medium"

        try:
            from openai import OpenAI as OAClient
            oa = OAClient(api_key=self.OPENAI_API_KEY, timeout=300.0)
            r = oa.images.generate(model=self.IMAGE_MODEL, prompt=prompt, size=size, quality=q, n=1)
            b64 = r.data[0].b64_json
            if not b64:
                return "Image API returned no image data."
            data = base64.b64decode(b64)
        except Exception as e:
            return f"Image generation failed: {e}"

        try:
            buf = io.BytesIO(data)
            buf.name = "image.png"
            self.bot.send_photo(chat_id, buf, caption=prompt[:1000])
            return (f"Image generated and sent ({size}, {q} quality). "
                    "Do not repeat the prompt back to Alex; he can see it.")
        except Exception as e:
            return f"Generated but failed to send: {e}"

    def tool_review_output(self, path: str = "", checks=None, claims=None, recalculate: bool = True,
                           render: bool = True, date_from: str = "", date_to: str = "") -> str:
        import uuid
        request = self.AGENT_DIR / "task_state" / "quality_requests" / (uuid.uuid4().hex + ".json")
        self.atomic_json(request, dict(path=path, checks=checks, claims=claims, recalculate=recalculate,
                                  render=render, date_from=date_from, date_to=date_to))
        try:
            proc = subprocess.Popen([sys.executable, "-m", "tabularium.records.review_output", str(request)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=tool_env(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                out, err = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc.pid)
                return Failed("Quality review timed out. The original file was not modified; quality is UNVERIFIED. Saved request: " + str(request))
            output = (out or b"").decode("utf-8", errors="replace").strip()
            if not output:
                return "Quality review failed; no completion claim is justified. " + (err or b"").decode("utf-8",errors="replace")[-1500:]
            return self._cap_result(output, limit=18000)
        except Exception as exc:
            return "Quality review unavailable: " + str(exc)

    def tool_send_document(self, path: str, chat_id: int, caption: str = "") -> str:
        """Send a local file (spreadsheet, PDF, doc, etc.) to Alex on Telegram."""
        try:
            p = Path(path)
            if not p.exists():
                return f"File not found: {path}"
            if p.stat().st_size > 45 * 1024 * 1024:
                return f"File too large to send via Telegram ({p.stat().st_size // (1024*1024)} MB; limit ~50 MB)."
            with open(p, "rb") as f:
                self.bot.send_document(chat_id, f, visible_file_name=p.name, caption=caption or None)
            return f"Sent {p.name}."
        except Exception as e:
            return f"Failed to send document: {e}"
