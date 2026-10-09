# ask-claude

A separate Hermes plugin with one tool, `ask_claude`: the local agent consults cloud
Claude on a question it cannot handle well itself, on the owner's Claude
subscription.

**Why through Claude Code.** A Claude subscription may be used only through
Anthropic's own apps; signing in to the unmodified Claude Code binary with your own
subscription is the documented case. So the question goes to `claude -p`, not to the
API: safe mode (no CLAUDE.md, skills, plugins, hooks, MCP servers, memory), no tools,
an empty working directory, a config directory of its own
(`<plugin data>/claude`), and an environment with nothing but `PATH`, proxy settings
and the token.

**The token** comes from `claude setup-token` (one year, model requests only) and is
kept as `ASK_CLAUDE_TOKEN` — never as `CLAUDE_CODE_OAUTH_TOKEN` in the Hermes `.env`,
and never through `claude login` as the Hermes user: Hermes reads both as Anthropic
credentials of its own and could route its own traffic (whole conversations) to
Claude. Setting `auth.adopt_external_logins: false` in `config.yaml` is a second lock.

**What leaves** is the agent's question only, after fixed masking rules
(`consult.scrub`): phones, e-mail, cards, passport/SNILS/INN and other numbers of 7+
digits, Telegram handles and links, home-network addresses, keys and tokens, names
from `names.txt` in their Russian forms. Every call is logged to `log.jsonl`.

Settings — `plugins.entries.ask-claude.settings` in `config.yaml`, also in the
Plugins hub of Hermes Desktop:

| key | default | |
|---|---|---|
| `model` | `opus` | `sonnet`, `fable`, or a full id; `fable` may bill to usage credits instead of plan limits |
| `effort` | `high` | `low` … `max` |
| `claude_path` | found on `PATH` or in `~/.local/bin` | |
| `timeout_minutes` | `15` | |
| `token` (secret) | — | stored in `.env` as `ASK_CLAUDE_TOKEN` |

Data: `~/.hermes/plugin-data/ask-claude/` (`log.jsonl`, `names.txt`, `claude/`),
private to the Hermes user.
