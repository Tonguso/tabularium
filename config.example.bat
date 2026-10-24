@echo off
rem Tabularium configuration. Copy this file to config.bat and replace every <placeholder>.
rem config.bat is listed in .gitignore and must never be committed.

rem Your Telegram user id: the agents answer this user and refuse everyone else.
set OWNER_TELEGRAM_USER_ID=<your-telegram-user-id>

rem One Telegram bot per agent (create them with @BotFather). Never reuse a token between agents.
set TELEGRAM_BOT_TOKEN=<coordinator-bot-token>
set CALLIMACHUS_TELEGRAM_BOT_TOKEN=<librarian-bot-token>
set VITRUVIUS_TELEGRAM_BOT_TOKEN=<engineer-bot-token>
set TACITUS_TELEGRAM_BOT_TOKEN=<private-agent-bot-token>

rem Model APIs. The paid APIs are fallbacks when a subscription route is unavailable; leave blank to disable one.
set ANTHROPIC_API_KEY=<your-anthropic-api-key>
set OPENAI_API_KEY=<your-openai-api-key>

rem Optional: Claude subscription route. Run `claude setup-token` and paste the token here, or leave blank to use
rem the Claude Code CLI's own sign-in.
set CLAUDE_CODE_OAUTH_TOKEN=<optional-claude-code-oauth-token>

rem Optional: web search providers, tried in the order set in search_order.json.
set TAVILY_API_KEY=<optional-tavily-api-key>
set ANYSEARCH_API_KEY=<optional-anysearch-api-key>

rem SEC EDGAR asks every client to identify itself with a name and an e-mail address.
set EDGAR_IDENTITY=<Your Name> <you@example.com>

rem Optional: the librarian's writable knowledge-base folder.
set KNOWLEDGE_BASE_DIR=<path-to-your-knowledge-base>\_wiki
