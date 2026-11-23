"""The research-report reader: listing and reading the reports of a subscribed group through the agent's own script.
"""
from tabularium.paths import tool_env
import json
import subprocess
import sys
from datetime import datetime


class ResearchReports:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    ZSXQ_DEFAULT_URL  = "https://wx.zsxq.com/group/YOUR_GROUP_ID"

    def _resolve_zsxq_group(self, group_url: str) -> tuple[str | None, str | None]:
        value = (group_url or "").strip()
        if not value or value in {"默认研报圈", "默认研报", "默认研报"}:
            return self.ZSXQ_DEFAULT_URL, None
        if not value.startswith(("https://", "http://")):
            return None, "That is a group name, not a URL. Omit group_url to use 默认研报圈."
        return value, None

    def tool_list_research_reports(self, date_from: str = "", date_to: str = "", group_url: str = "") -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        date_from = date_from.strip() or today
        date_to   = date_to.strip() or today
        group_url, group_error = self._resolve_zsxq_group(group_url)
        if group_error:
            return group_error

        scan, err = self._run_zsxq("summaries", date_from, date_to, group_url, timeout=300)
        if err:
            return err
        if not isinstance(scan, dict) or not isinstance(scan.get("items"), list):
            return "Summary reader returned an unexpected format; summary coverage is unverified."
        posts = scan["items"]
        coverage = scan.get("coverage", {})
        lines = [f"{len(posts)} post summaries between {date_from} and {date_to}; no PDFs downloaded.",
                 "Coverage: " + json.dumps(coverage, ensure_ascii=False),
                 "If the date boundary was not reached or posts are undated/collapsed, describe this as a partial scan."]
        for i, post in enumerate(posts, 1):
            names = "; ".join(post.get("filenames", [])) or "Post without a new PDF filename"
            lines.append(f"\n{i}. [{post.get('date', '?')}] {names}\n"
                         f"Visible post summary{' (COLLAPSED / incomplete)' if post.get('summary_collapsed') else ''}:\n"
                         f"{post.get('summary', '')}")
        lines.append("\nDiscuss candidate themes and supporting summaries with Alex first. "
                     "Wait for his report selection before a PDF deep dive. If this scan is archived, "
                     "read its remaining sections before claiming to have reviewed every summary.")
        return self._cap_result("\n".join(lines), limit=40000)

    def tool_read_research_reports(self, date_from: str = "", date_to: str = "", group_url: str = "",
                                   filter: str = "", max_reports: int = 5,
                                   full: bool = False, preview_pages: int = 3) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        date_from = date_from.strip() or today
        date_to   = date_to.strip() or today
        group_url, group_error = self._resolve_zsxq_group(group_url)
        if group_error:
            return group_error
        if not filter.strip():
            return ("Summary-first workflow: no selected report filename filter was provided. "
                    "No PDFs will be downloaded. Scan summaries, discuss with Alex, then deep-read his choices.\n\n"
                    + self.tool_list_research_reports(date_from, date_to, group_url))
        max_reports = max_reports or 5

        # Budget ~2 min per report plus scroll/scan overhead, capped at 25 min.
        timeout = min(1500, 180 + max_reports * 120)
        reports, err = self._run_zsxq("read", date_from, date_to, group_url,
                                 name_filter=filter.strip(), max_reports=max_reports, timeout=timeout)
        if err:
            return err
        if not reports:
            hint = f" matching '{filter}'" if filter.strip() else ""
            return f"No PDFs retrieved{hint} between {date_from} and {date_to}. Scan coverage is unverified; this is not proof that no matching reports exist."

        # Persist full extraction to disk so nothing is lost to history trimming.
        try:
            self.ZSXQ_REPORTS_DIR.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = self.ZSXQ_REPORTS_DIR / f"reports_{stamp}.json"
            out_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
            saved_note = f"\n\n(Full text saved to {out_path} — read_file it to revisit any report.)"
        except Exception:
            saved_note = ""

        # Cost control: by default only the first few pages go into context; the full text is on
        # disk as a .txt per report (read it in slices with execute_command if more is needed).
        import re as _re
        preview_pages = max(1, int(preview_pages or 3))
        blocks = [f"Read {len(reports)} report(s) between {date_from} and {date_to}:\n"]
        for i, r in enumerate(reports, 1):
            text = r.get("text", "") or ""
            npages = r.get("total_pages", r.get("pages_read", 0))
            coverage = (f"\nExtraction coverage: {r.get('coverage', 'Legacy extraction; verify page coverage')}. "
                        f"Pages needing vision: {r.get('vision_pages', [])}. "
                        f"Pages containing visuals: {r.get('visual_pages', [])}. "
                        f"Original PDF: {r.get('pdf_path', 'not retained')}; "
                        f"Full extracted text: {r.get('text_path', 'see saved JSON')}. "
                        f"Render an exact page for view_image: {r.get('render_command', 'unavailable')}\n")
            note = ""
            if not full:
                cut = None
                m = _re.search(rf"\*\*Page {preview_pages + 1}\*\*", text)
                if m:
                    cut = m.start()
                if cut is None or cut > 9000:
                    cut = min(len(text), 9000)
                if cut < len(text):
                    try:
                        txt_path = self.ZSXQ_REPORTS_DIR / f"reports_{stamp}_{i}.txt"
                        txt_path.write_text(text, encoding="utf-8")
                        where = str(txt_path)
                    except Exception:
                        where = str(out_path)
                    note = (f"\n\n[PREVIEW: first {cut} of {len(text)} chars shown (about the first "
                            f"{preview_pages} of {npages} pages). Full text: {where} . Read further slices "
                            f"only if needed, or re-call with full=true if Alex asks for the whole report.]")
                    text = text[:cut]
            blocks.append(
                f"=== {r.get('filename', '?')} ({r.get('date', '?')}, {npages} pages) ===\n"
                f"{coverage}{text}{note}"
            )
        return "\n\n".join(blocks) + saved_note

    def _run_zsxq(self, mode: str, date_from: str, date_to: str, group_url: str,
                  name_filter: str = "", max_reports: int = 0, timeout: int = 600):
        """Run zsxq_reader.py and return (parsed_json, error_string). One of them is None."""
        args = [sys.executable, str(self.ZSXQ_SCRIPT), "--mode", mode,
                "--group-url", group_url, "--date-from", date_from, "--date-to", date_to]
        if name_filter:
            args += ["--filter", name_filter]
        if max_reports:
            args += ["--max-reports", str(max_reports)]
        import hashlib
        scope = dict(mode=mode, group_url=group_url, date_from=date_from, date_to=date_to,
                     filter=name_filter, max_reports=max_reports)
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
        checkpoint = self.AGENT_DIR / "reports" / "research_checkpoints" / (key + ".json")
        args += ["--checkpoint", str(checkpoint)]
        # The reader attaches over CDP to the bot's own browser (port 9222), so make sure it is up.
        try:
            with self._browser_lock:
                self._get_page(authenticated=True)
        except Exception as e:
            return None, f"Could not start the browser for the report reader: {e}"
        try:
            # errors="replace": a single malformed byte must not blank the whole result.
            # Strict decoding fails on the reader thread and silently yields stdout=None.
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=tool_env(),
                                  encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            if checkpoint.exists():
                partial = json.loads(checkpoint.read_text(encoding="utf-8"))
                partial["coverage"] = dict(partial.get("coverage", {}), complete=False, interrupted=True,
                                          warning="Timeout; this is only the saved partial scan.")
                self.atomic_json(checkpoint, partial)
                if mode == "summaries":
                    return partial, None
            return None, f"Report reader timed out; partial work saved at {checkpoint}. Resume with the SAME scope; successful PDFs are reused, not downloaded again."
        except Exception as e:
            return None, f"Failed to launch report reader: {e}"
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            if len(detail) > 1500:
                detail = detail[:250] + "\n...[traceback middle omitted]...\n" + detail[-1200:]
            return None, f"Report reader failed (exit {proc.returncode}); saved partial work: {checkpoint}. Resume the same scope.\n{detail}"
        try:
            return json.loads(proc.stdout), None
        except Exception:
            return None, f"Report reader returned unparseable output:\n{(proc.stdout or '').strip()[:1000]}"
