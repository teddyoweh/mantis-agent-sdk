# HTTP MCP in print mode

A parent application can supply ephemeral tools to `mantis -p` through the child
process environment:

```json
{"universe":{"type":"http","url":"http://127.0.0.1:12345/mcp","headers":{"Authorization":"Bearer token"}}}
```

Set that JSON string as `MANTIS_MCP_SERVERS` (a direct name-to-config object, not
an `mcpServers` wrapper). Do not put credentials in command arguments or project
files. This hook only accepts HTTP/HTTPS servers; it does not enable project
stdio commands. An absent variable means no injected servers; malformed values
are refused without echoing their contents.

The dict-query path used by print mode connects before emitting its init frame,
registers tools as `mcp__<server>__<tool>`, and stops the MCP manager on completion,
error, or explicit stream closure. Init frames contain only server names/status,
not URLs, headers or connection error details. Connection failures appear as a
`failed` server status rather than pretending tools were discovered.

The existing stream-json message envelope and session resume behavior are
unchanged. Supply the environment again for every resumed process: credentials
are not saved with the session.
