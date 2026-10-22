# copyparty-share

A Hermes plugin with one tool, `copyparty_share`: the agent makes a temporary
read-only link to a file or folder in its own Copyparty folder, for the owner.
Files get there through the webdav MCP server (`webdav-mcp-server`), which the
agent uses with its own Copyparty account: read everywhere it is given `r`,
write only in its own folder.

The link stays with the owner. Every link made is remembered, and a
`pre_tool_call` hook blocks any tool call whose arguments carry one, except
`send_message` to a home channel (a bare platform name such as `telegram`) and
the tools in `link_tools` (default `mcp_calendar_*`). The agent's reply to the
owner is not a tool call and is not touched. The share key is matched with
separators and case ignored, so a link broken up with spaces is still caught;
calls through tool search's `tool_call` bridge are judged by the tool inside.

Copyparty enforces the rest on its side: with `shr-who: a` globally and
`shr_who: auth` on the agent's volume only, the agent (no admin permission
anywhere) can share only from its own folder; it gets `r` elsewhere and
`rwmd` only there.

Copyparty config (merge into the existing one):

```
[global]
  shr: /share       # turns shares on, served under /share
  shr-who: a        # elsewhere only accounts with admin permission (a) share

[accounts]
  agent: <long random password>

[/agent]
  /srv/copyparty/agent     # any empty directory
  accs:
    rwmd: agent
    rwmda: <owner>
  flags:
    shr_who: auth   # here the agent may share
    daw             # webdav edits overwrite instead of making copies

# every volume the agent may read gets  r: agent  in its accs
```

Settings (`plugins.entries.copyparty-share.settings`, Plugins hub):
`base_url` (Copyparty on the LAN, e.g. `http://10.0.0.125:3923`), `user`
(`agent`), `folder` (`/agent`), `days` (7, at most 30), `link_tools`; the
password is `COPYPARTY_AGENT_PASSWORD` in `~/.hermes/.env`.

Install: copy this directory to `~/.hermes/plugins/copyparty-share`, then
`hermes plugins enable copyparty-share` and restart the gateway.
