# Vitruvius (Vit)
Dedicated engineer and web designer. Identity is SKILL.md; requirements are docs/design-brief.md.

## Connect
1. Create a separate bot with Telegram @BotFather and enter its token in the home folder's .env under VITRUVIUS_TELEGRAM_BOT_TOKEN. Never reuse another bot token.
2. Open Start Vit.vbs through Explorer. This starts only Vit, hidden, with logs in D:\Agents\logs. Do not launch persistent agents as children of a coding app.
3. In Vit's Telegram chat send /start then /openai_connect and complete his own browser sign-in. Existing agents' sign-ins are not copied.
The configured model is gpt-6.1-sol through the existing subscription routing, with its existing quota/API handling. Availability is determined by service responses, not promised by this file. API-only mode requires explicit VITRUVIUS_ALLOW_API=1; no new service was purchased.

## Delegation
Cato's vit-engineering skill documents work_client.py. submit queues an isolated task; status/result retrieve evidence; steer adds clarification; resume is explicit after a blocked/terminal task. cancel is cooperative. Stable request IDs prevent duplicate submission. Foreground chat remains separate.

## Reuse and boundaries
Runtime is a scoped snapshot of Cal's current Cato-derived engine, using shared agent_runtime, logging and owner Telegram control. Browser automation reuses the installed executable but uses Vit's own profile and CDP 9226. Port 8216 enforces a single instance. Source hashes are recorded in docs/reuse-manifest.json.
This avoids refactoring running agents but upstream fixes must be ported deliberately. Tool processes run with the Windows user's rights; allowed_paths is an instruction contract, not a filesystem sandbox. Secret scanning catches common patterns, not all secrets.

## Layout
scripts: reusable utilities; scratch: disposable work; reports: deliverables; skills: methodology; memory.md: separate durable knowledge; task_state: queue, checkpoints and evidence.
