# Security

Tabularium runs agents with access to a Telegram bot, model APIs, the local file system and a shell on the owner's
machine. Treat it as you would any automation with those rights.

- **Reporting.** Open a GitHub issue marked "security" for problems that do not expose a secret or a person, or
  contact the maintainer through the repository's profile for those that do.
- **Credentials.** All keys live in `config.bat`, which is never committed, and are redacted from logs by
  `agent_logging`. Do not put credentials in skills, memory or prompts.
- **Prompt injection.** Content read from the web, email or documents is treated as data; after reading outside
  content, sending email and destructive commands become parked actions that need the owner's yes (see the taint
  rail in the white paper). This reduces, and does not eliminate, the risk.
- **Isolation.** Command screening is a pattern list, not a sandbox. Run the agents under an account with no more
  rights than the work needs.
- **Owner check.** The agents answer only the Telegram user id in `OWNER_TELEGRAM_USER_ID` and refuse everyone
  else.
