# WoW Forever news editor

This is the source of a Python service that collects publicly available World of Warcraft: Forever news, prepares Russian-language Telegram drafts using an AI model, and lets a human editor review and publish them. Its configured sources are Blizzard, Wowhead, Icy Veins, and Noob Club. It uses SQLite for local state and can call GPT-6 Luna through OpenCode. Channel publication is manual by default; enabling limited official-news autoposting requires separate configuration.

**Reddit is not connected.** We intend to request approved, read-only access to public submissions and selected comments in `r/wowforever` to identify substantive discussions. Selected text would be sent to an external AI model for *summarization, not training*, then presented to a human editor as a draft. A published summary would link to the original thread and distinguish player opinions from verified facts. The service would not post, comment, vote, or message on Reddit. We will not collect Reddit data automatically unless Reddit explicitly approves the proposed use and access method. Text-only drafts for interesting image-free discussions are planned, **not implemented here**; ordinary news currently requires a verified image.

## Code and operation

- `wownews/`: discovery, extraction, editorial validation, OpenCode bridge, Telegram review and publication.
- `config.json`, `glossary.json`, `EDITORIAL_RU.txt`: non-secret source settings and editorial rules.
- `test_bot.py`: unit tests using synthetic data.

The service targets Linux with Python 3.11+ (the standard library, including `fcntl` and `zoneinfo`). To run it, provide a private `.env` with `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, `PUBLISH_CHANNEL_ID`, `MODEL_PROVIDER=opencode`, `OPENCODE_MODEL=openai/gpt-6-luna`, and the path to the installed OpenCode CLI as `OPENCODE_BIN` if needed. Do not commit that file. Run `python3 -m unittest -q test_bot` for tests, `python3 -m wownews --check-sources` to check sources, or `python3 -m wownews` to start the service. OpenCode must be installed and authorized separately. No live credentials or database are included.

This public source snapshot is for transparency about the server application that would request Reddit access; it is not a claim that Reddit has approved the application.
