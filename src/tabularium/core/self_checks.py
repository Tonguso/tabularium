"""What the agent checks about itself on a schedule: the daily self-test, the weekly memory checks, the benchmark
rerun after a route change, the lesson digest, and the nightly save.
"""
from tabularium.paths import child_env
import json
import subprocess
import sys
import time


class SelfChecks:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    SELFTEST_SECONDS = 24 * 3600

    SELFTEST_FIRST_DELAY = 300

    WEEKLY_SECONDS = 7 * 24 * 3600

    WEEKLY_CHECKS = (("memory_review", "run_memory_review"), ("memory_exam", "run_memory_exam"),
                     ("lesson_digest", "run_lesson_digest"))                                       # json name, method

    def _selftest_worker(self):
        """Daily self-test, plus the weekly memory review and memory exam (2026-10-11). A weekly check runs when its
        task_state/<name>.json is older than a week or missing, or when task_state/<name>_requested exists
        (tabularium review|exam)."""
        time.sleep(self.SELFTEST_FIRST_DELAY)
        last_selftest = 0
        while True:
            if time.time() - last_selftest >= self.SELFTEST_SECONDS:
                last_selftest = time.time()
                try:
                    self.run_selftest()
                except Exception as exc:
                    print(f"[selftest] worker error: {type(exc).__name__}: {exc}")
            for name, method in self.WEEKLY_CHECKS:
                flag = self.AGENT_DIR / "task_state" / f"{name}_requested"
                record = self.AGENT_DIR / "task_state" / f"{name}.json"
                due = flag.exists() or not record.exists() or time.time() - record.stat().st_mtime >= self.WEEKLY_SECONDS
                if not due:
                    continue
                flag.unlink(missing_ok=True)
                try:
                    getattr(self, method)()
                except Exception as exc:
                    print(f"[{name}] worker error: {type(exc).__name__}: {exc}")
            try:
                self.run_correction_eval()                                       # daily, when the owner has been quiet
            except Exception as exc:
                print(f"[correction_eval] worker error: {type(exc).__name__}: {exc}")
            try:
                self.run_benchmark_rerun()                                       # a model or route change is rescored (2026-11-02)
            except Exception as exc:
                print(f"[benchmark_rerun] worker error: {type(exc).__name__}: {exc}")
            try:
                if self._cli_changed():                                          # a Claude update: prove the turn still works
                    last_selftest = time.time()
                    self.run_selftest()
            except Exception as exc:
                print(f"[selftest] update check error: {type(exc).__name__}: {exc}")
            try:
                from tabularium.learning import corrections   # a quiet thread of corrections: one diagnosis
                corrections.flush(self)
            except Exception as exc:
                print(f"[correction] flush error: {type(exc).__name__}: {exc}")
            try:
                from tabularium.learning import dockets       # membership proposals and due method reviews (2026-11-10)
                dockets.flush(self)
            except Exception as exc:
                print(f"[dockets] flush error: {type(exc).__name__}: {exc}")
            flag = self.AGENT_DIR / "task_state" / "memory_trial_requested"     # consolidation trial (2026-10-20)
            if flag.exists():
                flag.unlink(missing_ok=True)
                try:
                    self.run_memory_trial()
                except Exception as exc:
                    print(f"[memory_trial] worker error: {type(exc).__name__}: {exc}")
            time.sleep(60)

    def run_benchmark_rerun(self):
        """A model or route change reruns the benchmark, once per route (2026-11-02, the review article: rescore
        whenever the model changes; until now a digest line only said a rerun was due). The run is the Sunday
        profile in its own process, logging to logs/eval-<agent>.log; the dashboard's Regression eval row then shows
        the new score against the previous route's."""
        from tabularium.learning import regression
        from tabularium.ops.operations import read_json
        from tabularium.core.agent_runtime import atomic_json
        current = regression.primary_route(self.AGENT_DIR)
        if not current or not regression.route_gap(self.AGENT_DIR):
            return False
        record = self.AGENT_DIR / "task_state" / "benchmark_rerun.json"
        if read_json(record).get("route") == current:
            return False                                                         # launched for this route already
        root = self.AGENT_DIR.parent
        with open(root / "logs" / f"eval-{self.AGENT_DIR.name}.log", "ab") as log:   # the child keeps its own handle
            subprocess.Popen([sys.executable, "-m", "tabularium", "eval", self.AGENT_DIR.name, "--k", "3", "--sample", "8"],
                             cwd=str(root), env=child_env(), stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        atomic_json(record, {"route": current, "time": time.time()})
        print(f"[benchmark_rerun] primary route is now {current}: benchmark rerun launched")
        return True

    def run_lesson_digest(self):
        """Weekly: the skill edits awaiting Alex's go, sent by the runtime, so a proposal never depends on the agent
        remembering to show it (2026-11-01: 40 proposals had sat unseen; Alex: "Cato never proposed any change to
        me"). The dashboard lists the same edits at any time."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.core.agent_runtime import atomic_json
        from tabularium.learning import learning
        text = learning.awaiting_go(self.AGENT_DIR, OWNER_CHAT_ID) if self.config.role.learns else ""
        sent = bool(text) and self.send_message(OWNER_CHAT_ID, text)
        atomic_json(self.AGENT_DIR / "task_state" / "lesson_digest.json", {"time": time.time(), "sent": bool(sent), "chars": len(text)})
        return text

    def _cli_changed(self):
        """True once per change of the subscription CLI paths (Claude Code or Antigravity CLI).
        2026-10-20: an update moved the CLI and the agents ran on the fallback for two days; the daily
        self-test passed because the fallback answered. Now paths are checked every minute and a change
        runs the self-test at once, with the route it used in the record."""
        from tabularium.models.subscription_runtime import find_cli, find_gemini_cli
        from tabularium.core.agent_runtime import atomic_json
        path = self.AGENT_DIR / "task_state" / "claude_cli.json"
        current = find_cli() or ""
        current_gemini = find_gemini_cli() or ""
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            previous = saved.get("path")
            previous_gemini = saved.get("gemini_path")
        except (OSError, ValueError):
            previous = None
            previous_gemini = None
        if previous == current and previous_gemini == current_gemini:
            return False
        atomic_json(path, {"path": current, "gemini_path": current_gemini, "changed": time.time()})
        claude_changed = previous is not None and previous != current
        gemini_changed = previous_gemini is not None and previous_gemini != current_gemini
        if not claude_changed and not gemini_changed:
            return False                                 # first record on this installation: nothing changed
        if claude_changed:
            print(f"[selftest] Claude CLI changed: {previous or '(none)'} -> {current or '(none)'}; running the self-test now")
        if gemini_changed:
            print(f"[selftest] Gemini CLI changed: {previous_gemini or '(none)'} -> {current_gemini or '(none)'}; running the self-test now")
        return True

    def run_selftest(self):
        """One real turn, composed the way the message handler composes it (compose_turn on a synthetic Telegram
        message) and run through run_turn: the model must call read_file on the identity file and answer OK.
        Result in task_state/selftest.json; the supervisor tells Alex on failure, the dashboard shows it.
        Runs under the chat lock so it never interleaves with a real conversation turn (2026-10-11). Nothing is sent
        to Telegram. Before 2026-10-11 18:00 this bypassed the handler and passed while every real message failed."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.core.agent_runtime import atomic_json
        from types import SimpleNamespace
        path = self.AGENT_DIR / "task_state" / "selftest.json"
        target = self.AGENT_DIR / "SKILL.md"
        text = ("[Runtime self-test, not a message from Alex; send nothing to Telegram and change nothing.] "
                f"Call read_file on {target} (a few lines are enough), then reply with exactly the word OK.")
        incoming = SimpleNamespace(chat=SimpleNamespace(id=OWNER_CHAT_ID), text=text, date=int(time.time()), reply_to_message=None)
        messages = []
        started = time.time(); reply = ""; error = ""; ok = False
        self.ensure_initialised(OWNER_CHAT_ID)
        with self.get_chat_lock(OWNER_CHAT_ID):
            try:
                messages.append({"role": "user", "content": self.compose_turn(incoming, nudge=False)})
                reply = self.run_turn(messages, OWNER_CHAT_ID) or ""
                used_tool = any(isinstance(m.get("content"), list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"])
                               for m in messages)
                ok = "OK" in reply.upper() and used_tool
                if not ok:
                    error = "no tool call" if not used_tool else "reply was not OK"
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        record = {"time": time.time(), "ok": ok, "seconds": round(time.time() - started, 1), "reply": reply[:200], "error": error}
        # Which route answered, and whether it was the first choice: for two days (2026-10-20 to 10-22) the daily
        # self-test passed on the ChatGPT fallback while the Claude CLI was missing, and nothing said so.
        try:
            state = json.loads((self.AGENT_DIR / "task_state" / "routing_state.json").read_text(encoding="utf-8"))
            routes = self._routing.config(self.AGENT_DIR).get("routes") or []
            route = state.get("route") or {}
            record["route"] = route.get("kind", "")
            record["fallback"] = bool(routes and route and route.get("kind") != routes[0].get("kind"))
            if record["fallback"]:
                print(f"[selftest] passed on the fallback route {route.get('kind')}; first choice {routes[0].get('kind')} did not answer")
        except (OSError, ValueError, AttributeError, KeyError):
            pass
        atomic_json(path, record)
        print(f"[selftest] {'passed' if ok else 'FAILED'} in {record['seconds']}s" + (f": {error}" if error else ""))
        return record

    def _nightly_save_worker(self) -> None:
        """Background thread: watches for save_requested.flag and runs the memory save.

        Uses a blocking lock.acquire() so it waits for any in-progress turn to
        finish before starting — no race conditions, no timing games.
        """
        print("Nightly save worker started.")
        while True:
            try:
                time.sleep(10)
                if not self.SAVE_REQUEST_FILE.exists():
                    continue

                print("Nightly save requested — waiting for chat lock...")

                try:
                    chat_id = int(self.CHAT_ID_FILE.read_text(encoding="utf-8").strip())
                except Exception:
                    # No active chat — confirm immediately so the restart can proceed
                    self.SAVE_REQUEST_FILE.unlink(missing_ok=True)
                    self.SAVE_CONFIRM_FILE.write_text("done", encoding="utf-8")
                    print("Nightly save: no active chat — confirmed without saving.")
                    continue

                print(f"Nightly save: acquiring lock for chat {chat_id}...")
                self.ensure_initialised(chat_id)
                lock = self.get_chat_lock(chat_id)
                lock.acquire()  # blocking — waits for any in-progress turn to finish
                print("Nightly save: lock acquired, running save turn...")
                try:
                    self.SAVE_REQUEST_FILE.unlink(missing_ok=True)
                    messages = self.conversations[chat_id]
                    messages.append({"role": "user", "content": f"{self._ts()} {self.NIGHTLY_SAVE_PROMPT}"})
                    self.bot.send_chat_action(chat_id, "typing")
                    reply = self._run_turn_with_guard(messages, chat_id)
                    if reply:
                        self.save_conversation(chat_id)
                        self.send_message(chat_id, reply)
                        if getattr(reply, "complete", False):      # the turn ran to its own end, not to a limit
                            self.SAVE_CONFIRM_FILE.write_text("done", encoding="utf-8")
                            self._conversation_path(chat_id).unlink(missing_ok=True)
                            self.send_message(chat_id, "Memory saved. Restarting shortly.")
                            print("Nightly save: complete.")
                        else:
                            print(f"Nightly save: the save turn did not complete: {reply[:100]}")
                    else:
                        print("Nightly save: _run_turn_with_guard returned None.")
                finally:
                    lock.release()

            except Exception as e:
                print(f"Nightly save worker error: {e}")
