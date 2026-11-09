# Callimachus (Cal)
Personal Librarian and Knowledge Steward for Alex.

Built from the current Cato runtime, with separate identity, memory, browser and sign-in.

## Start
1. Set LIBRARIAN_TELEGRAM_BOT_TOKEN in the home folder's .env to a NEW Telegram bot token.
2. `tabularium run librarian`, or let the supervisor start him. Do not run a duplicate copy of the bot.
3. Message Cal /openai connect and finish his own ChatGPT sign-in.
4. Give him a document and ask him to retain it, then retrieve it by description.

Automatic Cato research delegation is not enabled. The callable queue interface is built.
Build does not establish a working Telegram login, model connection or acceptance-test result.

See docs/IMPLEMENTATION.md for permissions, costs, interface fields and rollback.
See docs/design-brief.md for Alex's role specification.
