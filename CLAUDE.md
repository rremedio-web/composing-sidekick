# Composing Sidekick

An LLM composing bandmate that learns a composer's musical identity from their Ableton
catalogue (symbolic, not audio) and works with them inside Live via Producer Pal MCP.

Read [`AGENTS.md`](AGENTS.md) first — it applies to every assistant. Then, for a
composing session, [`docs/CURSOR_CHAT_PLAYBOOK.md`](docs/CURSOR_CHAT_PLAYBOOK.md) and
[`docs/KNOWLEDGE_GUIDE.md`](docs/KNOWLEDGE_GUIDE.md); for engineering,
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Principles

- **Durable KEEP over features** — the score is session survival of judged
  suggestions, not KEEP clicks or new tools.
- **Close the night** — every JAM write is stamped; `close_session()` (Live still up)
  assigns terminal states. An unswept night cannot be scored later.
- **The owner's material first** — retrieve and develop real phrases; the generator is
  a critic. A reference steers *which of their* phrases; it never re-keys a loop.
- **JAM/BOOTH name-lock** — auto-writes only on `SIDEKICK JAM` and `SIDEKICK BOOTH`;
  everything else is preview or confirm.
- **Deterministic + selftested** — seeded RNG wherever machines make notes.
- **Ears opt-in** — pass `use_ears=True` explicitly until `store/ears_default.json`
  flips it; audio under `moment.unheard` is named, not heard — ask, never infer from a
  clip name.

## Verify

```bash
.venv/bin/python -m unittest discover -s tests -t .   # stdlib unittest; pytest is not installed
.venv/bin/python mcp_server/server.py --selftest
```

Skills under `.claude/skills/` (`sombra-brain`, `challenge-deck`) are shared musical
knowledge for every client, shipped as templates.
