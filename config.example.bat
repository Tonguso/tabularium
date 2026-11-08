@echo off
rem Tabularium configuration. Copy this file to config.bat and replace every <placeholder>.
rem config.bat is listed in .gitignore and must never be committed. Load it in a shell with: call config.bat

rem ===== Required for one agent =====================================================================

rem Your Telegram user id (message @userinfobot to see it). The agents answer this user and refuse everyone else.
set OWNER_TELEGRAM_USER_ID=<your-telegram-user-id>

rem The coordinator's bot token, from @BotFather. One token per agent; never reuse a token between agents.
set TELEGRAM_BOT_TOKEN=<coordinator-bot-token>

rem Model access: at least one. Both may be set; routing prefers a configured subscription route and falls back
rem to the API. Leave a key blank to disable that provider.
set ANTHROPIC_API_KEY=<your-anthropic-api-key>
set OPENAI_API_KEY=<your-openai-api-key>

rem ===== The rest of the cast (one token each; leave blank for slots you do not run) ================

set CALLIMACHUS_TELEGRAM_BOT_TOKEN=<librarian-bot-token>
set VITRUVIUS_TELEGRAM_BOT_TOKEN=<engineer-bot-token>
set TACITUS_TELEGRAM_BOT_TOKEN=<private-agent-bot-token>

rem ===== Optional ===================================================================================

rem Claude subscription route through the Claude Code CLI: run `claude setup-token` and paste the token, or leave
rem blank to use the CLI's own sign-in.
set CLAUDE_CODE_OAUTH_TOKEN=<optional-claude-code-oauth-token>

rem Web search providers, tried in the order set in search_order.json. Without them, search goes through the
rem model routes that offer it.
set TAVILY_API_KEY=<optional-tavily-api-key>
set ANYSEARCH_API_KEY=<optional-anysearch-api-key>

rem Spoken replies to voice notes.
set ELEVENLABS_API_KEY=<optional-elevenlabs-api-key>

rem SEC EDGAR asks every client to identify itself with a name and an e-mail address.
set EDGAR_IDENTITY=<Your Name> <you@example.com>

rem The librarian's writable knowledge-base folder.
set KNOWLEDGE_BASE_DIR=<path-to-your-knowledge-base>\_wiki
