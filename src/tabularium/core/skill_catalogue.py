"""The agent's skills: finding them, the catalogue in the prompt, loading one, and carrying loaded skills from one
worker episode to the next.
"""
import re
from pathlib import Path
from tabularium.core.outcome import AlreadyShown


class SkillCatalogue:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    ALWAYS_SKILLS: tuple = ()

    def _dedup_episode_skill(self, chat_id, inputs, result):
        """A skill already loaded in this episode, unchanged, is not loaded into the request again (2026-11-19: task
        232 loaded engineering-workflow 15 times in 10 episodes). Skills are never compacted, so the text stays above."""
        import hashlib
        name = str((inputs or {}).get("name") or "")
        if not name or not isinstance(result, str) or not result.startswith("# Skill: "):
            return result
        body = (self.discover_skills().get(name) or {}).get("body")
        if body is None:
            return result
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        skills = self._episode_state(chat_id).setdefault("skills", {})
        if skills.get(name) == digest:
            return AlreadyShown(f"[Skill {name} is already loaded above in this session, unchanged; follow that text.]")
        skills[name] = digest
        return result

    def loaded_skills(self, chat_id) -> list:
        """The skills this episode holds, in load order."""
        return list((self._episode_reads.get(chat_id) or {}).get("skills", {}))

    def carry_skills(self, chat_id, messages, names) -> None:
        """A new episode opens with the skills the last one held, in full, so the worker does not spend calls loading
        them again; they count as loaded in the new episode."""
        import hashlib
        installed, blocks = self.discover_skills(), []
        skills = self._episode_state(chat_id).setdefault("skills", {})
        for name in names:
            found = installed.get(name)
            if not found:
                continue
            blocks.append(f"# Skill: {name}\n(loaded from {found['path']})\n\n{found['body']}")
            skills[name] = hashlib.sha256(found["body"].encode("utf-8")).hexdigest()
        if blocks and messages and isinstance(messages[-1].get("content"), str):
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] + "\n\nSkills this task had loaded, in "
                            "full; they are loaded in this session too:\n\n" + "\n\n".join(blocks)}

    def _parse_frontmatter(self, text: str):
        """Return (meta_dict, body). Minimal YAML: flat `key: value`, values may wrap."""
        if not text.startswith("---"):
            return {}, text
        end = text.find("\n---", 3)
        if end == -1:
            return {}, text
        raw, body = text[3:end], text[end + 4:]
        meta, key = {}, None
        for line in raw.splitlines():
            if not line.strip():
                continue
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                # YAML block scalars (`key: |` / `key: >`) carry their text on following lines.
                meta[key] = "" if val in ("|", ">", "|-", ">-") else val
            elif key and line.startswith((" ", "\t")):
                meta[key] = (meta[key] + " " + line.strip()).strip()   # wrapped / block value
        return meta, body.lstrip("\n")

    def discover_skills(self) -> dict:
        """Scan skills/ and return {name: {description, path, body}}."""
        found = {}
        if not self.SKILLS_DIR.exists():
            return found
        for d in sorted(self.SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if not (d.is_dir() and f.exists()):
                continue
            try:
                from tabularium.learning.trials import overridden
                source = Path(overridden(f) or f)                   # a trial arm reads the proposed edit (tabularium.learning.trials)
                meta, body = self._parse_frontmatter(source.read_text(encoding="utf-8-sig"))   # editors add a BOM; it hid two descriptions (2026-10-10)
            except Exception as e:
                print(f"[skills] cannot read {f}: {e}")
                continue
            name = meta.get("name") or d.name
            found[name] = {"description": meta.get("description", ""), "path": f, "body": body}
        return found

    def skills_prompt_block(self) -> str:
        """Layer 1 — what goes into the system prompt: the full body of every ALWAYS_SKILLS skill (a method the agent must
        follow in every conversation is standing instruction, not something to remember to load; Tacitus, 2026-10-09),
        then the catalogue of the rest."""
        sk = self.discover_skills()
        if not sk:
            return ""
        lines = []
        standing = [n for n in self.ALWAYS_SKILLS if n in sk]
        for n in standing:
            lines.append(f"\n\n## Standing method: {n} (always loaded, follow it in every conversation)\n"
                         f"Its references/ folder ({sk[n]['path'].parent / 'references'}) is read with read_file when the method points to it.\n\n"
                         + sk[n]["body"])
        sk = {n: s for n, s in sk.items() if n not in standing}
        if not sk:
            return "\n".join(lines)
        lines += ["\n\n## Available Skills\n",
                  "Installed methods: " + ", ".join(sk) + ".",
                  'Use skill(name="") to discover their descriptions when choosing a method; '
                  'use skill(name="exact-name") to load it before acting. '
                  'Do not guess a method from its name or treat an unloaded method as instructions.']
        return "\n".join(lines)

    def tool_skill(self, name: str, chat_id: int = None) -> str:
        """Layer 2 — load one skill's full instructions, with the agent's own earlier work with it (2026-10-21)."""
        sk = self.discover_skills()
        if not name:
            return "\n".join(f"- {n}: {s['description']}" for n, s in sk.items()) or "No skills installed."
        if name not in sk:
            avail = ", ".join(sk) or "(none installed)"
            return f"No skill named '{name}'. Installed skills: {avail}"
        s = sk[name]
        text = (f"# Skill: {name}\n(loaded from {s['path']})\n"
                f"Supporting files in that folder — references/, scripts/ — are read with read_file "
                f"or run with execute_command, only if the instructions below point to them.\n\n{s['body']}")
        import sqlite3, zipfile
        from tabularium.records import skill_record
        scope, ref = (("task", int(self.BACKGROUND_TASK_ID)) if self.BACKGROUND_TASK_ID
                      else ("turn", getattr(getattr(self._inbox, "local", None), "job", None)))
        try:
            history = skill_record.record(self.AGENT_DIR, name, chat_id, scope, ref,
                                          getattr(self, "_background", None), self._inbox)
            skill_record.note(self.AGENT_DIR, name, scope, ref, chat_id)
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:     # the skill still loads
            print(f"[skill record] {name}: {type(exc).__name__}: {exc}")
            history = ""
        return text + history
