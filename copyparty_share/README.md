# copyparty-share

A Hermes plugin with one tool, `copyparty_share`: the agent makes a temporary
read-only link to a Copyparty file or folder, for the owner. It works on the
agent's own Copyparty account, the one the webdav MCP server
(`webdav-mcp-server`) uses: anything that account can read can be shared,
unless the `folder` setting narrows it to one folder.

The link stays with the owner. Every link made is remembered, and a
`pre_tool_call` hook blocks any tool call whose arguments carry one, except
`send_message` to a home channel (a bare platform name such as `telegram`) and
the tools in `link_tools` (default `mcp_calendar_*`). The agent's reply to the
owner is not a tool call and is not touched. The share key is matched with
separators and case ignored, so a link broken up with spaces is still caught;
calls through tool search's `tool_call` bridge are judged by the tool inside.

On Copyparty's side, shares must be on (`shr: /share` in `[global]`), and the
volume must let the agent share: `shr-who` is `auth` (any logged-in account
that can read it) unless set to `a` (admin permission only) or `no`, globally
or per volume with the `shr_who` volflag. Volumes the agent cannot read
cannot be shared at all.

```
[global]
  shr: /share

[accounts]
  agent: <long random password>

# every volume the agent may read (and so share) gets  r: agent  in its accs
```

Settings (`plugins.entries.copyparty-share.settings`, Plugins hub):
`base_url` (Copyparty on the LAN, e.g. `http://10.0.0.125:3923`), `user`
(`agent`), `folder` (`/`, everything the agent can read), `days` (7, at most
30), `link_tools`; the password is `COPYPARTY_AGENT_PASSWORD` in `~/.hermes/.env`.

Install: copy this directory to `~/.hermes/plugins/copyparty-share`, then
`hermes plugins enable copyparty-share` and restart the gateway.
