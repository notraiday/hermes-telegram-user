# ask-claude

A separate Hermes plugin with one tool, `ask_claude`: the local agent consults cloud
Claude on a question it cannot handle well itself.

What leaves the machine is the agent's question and nothing else — no history, no
files, no tool results — after these steps:

1. **The agent writes it impersonal.** The tool description tells it to.
2. **Fixed rules mask what they recognise** (`consult.scrub`): phones, e-mail,
   cards, passport/SNILS/INN and other numbers of 7+ digits, Telegram handles and
   links, home-network addresses, keys and tokens, names from `names.txt` in
   their Russian forms. Query strings are cut from links.
3. **The owner sees the exact text** that will be sent and approves every call
   (per-call approval key, so "always" covers one question only). In cron there
   is nobody to approve, and Hermes refuses the call.
4. **Logged with its cost** in `log.jsonl`; a monthly cap blocks the tool.

Settings (Hermes `.env`):

| variable | default | |
|---|---|---|
| `ASK_CLAUDE_API_KEY` | — | required; deliberately not `ANTHROPIC_API_KEY`, which Hermes would take as a provider key |
| `ASK_CLAUDE_MODEL` | `claude-opus-5-5` | `claude-fable-5-1` is stronger and 2.5× the price |
| `ASK_CLAUDE_EFFORT` | `high` | `low` … `max` |
| `ASK_CLAUDE_MONTHLY_USD` | `20` | counted from the plugin's own log |
| `ASK_CLAUDE_NAMES_FILE` | `<plugin data>/names.txt` | one name per line |

Data: `~/.hermes/plugin-data/ask-claude/` (`log.jsonl`, `names.txt`), private to
the Hermes user.

Install: copy this directory to `~/.hermes/plugins/ask-claude`, then
`hermes plugins enable ask-claude` and `hermes pm repair` (installs `anthropic`).
