# CLI credential ID completion

Interactive `/make_token`, `/spawnas`, `/runas`, `/lateral`, and `/creds del`
complete the value after `-i`. Type an ID prefix and select a suggestion with
Tab. The menu displays the credential label; insertion contains only the ID.

`CREDENTIAL_ID_COMPLETION_COMMANDS` registers command paths and their credential
ID flags. Attach the provider to another command by adding its tuple path and
supported flag tuple to this mapping. Parsing still needs to support that flag.

`credential_completion_prefix(line)` recognizes a registered ID argument at the
cursor, preserving quoted arguments and rejecting unfinished quotes. It avoids
completion inside the arbitrary command following `/runas` credentials.

`CredentialIdCompletionProvider.__init__(client)` creates a lazy provider.
`candidates(prefix)` requests the authenticated workspace's redacted credential
listing and retains only IDs and labels for five seconds. An unavailable store
produces no suggestions and discards expired cached values. No password request
is made. Recently added/deleted IDs can take up to five seconds to appear.

Both `RmmCliCompleter` and `RmmCliPTKCompleter` accept an optional API client and
share this provider implementation. `run_interactive` supplies its workspace
client to the live prompt completer. Existing calls without a client retain
static completion and make no credential requests.

The interactive prompt runs completion in a worker thread so an unavailable
workspace API does not block typing.
