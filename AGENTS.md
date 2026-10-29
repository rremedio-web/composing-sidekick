# Working in this repo (any assistant)

composing-sidekick is an LLM composing bandmate for Ableton Live: an MCP server that
serves a composer's own catalogue back to them, writes verified MIDI into Live through
Producer Pal, and closes every suggestion with a judgment. Read the
[README](README.md) for setup and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
the module map.

## Composing sessions

1. [`docs/CURSOR_CHAT_PLAYBOOK.md`](docs/CURSOR_CHAT_PLAYBOOK.md) — the session loop:
   hear the room, answer on `SIDEKICK JAM`, the owner judges, close the night.
2. [`docs/KNOWLEDGE_GUIDE.md`](docs/KNOWLEDGE_GUIDE.md) — what has authority (the
   owner's words > a fresh Set read > measurements > model listening > your
   interpretation) and where each kind of knowledge lives.
3. [`config/claude_mcp.md`](config/claude_mcp.md) — every tool, the judgment ledger
   contract, audio references.
4. The musical-knowledge skills under `.claude/skills/` are shared by every client
   (the path is historical). They ship as templates; fill them from the catalogue and
   the owner's words before leaning on them.

Non-negotiables: the owner's material first; automatic writes only on `SIDEKICK JAM` /
`SIDEKICK BOOTH`; only the owner assigns KEEP or DROP; audio clips are named, not
heard, until a listening pass says otherwise; speak producer (bars, sections, energy),
not code.

## Engineering

- Verify with `.venv/bin/python -m unittest discover -s tests -t .` and
  `.venv/bin/python mcp_server/server.py --selftest`. Tests never contact Live
  (`tests/_guard.py`) and never write the real store. Six modules also carry their
  own `--selftest` (session_ears, mix_trials, mix_contracts, mix_witness, mix_dsp,
  mix_check); `tests/test_module_selftests.py` runs them all, e.g.
  `.venv/bin/python -m unittest tests.test_module_selftests`.
- Producer Pal behaviour comes from captures (`tests/fixtures/pp/`), never from a
  migration guide. Mutation captures run only in a disposable scratch Set.
- Several tests assert on `config/claude_mcp.md`, `docs/CURSOR_CHAT_PLAYBOOK.md` and
  `docs/PRODUCER_PAL_VERSIONS.md`; update docs and code together.
- Say no to a feature unless it improves candidate selection, closes the judgment loop,
  measures durable adoption, or removes friction from that loop.
