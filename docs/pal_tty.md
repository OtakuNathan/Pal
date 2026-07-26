# Pal TTY

`pal tty` is the interactive Unix-socket client for a running Pal runtime:

```bash
pal tty --runtime-root ~/.pal
```

It keeps one socket connection for multiple turns. Input is read asynchronously
with Prompt Toolkit; assistant text is accumulated and rendered as Rich
Markdown. Slash-command results preserve their original plain-text line
structure. Reasoning, tool calls, and errors use separate plain terminal styles
and are never interpreted as answer Markdown.

Local controls:

- `/exit` or `/quit`: close the TTY client.
- `Ctrl-D`: close the TTY client.
- `Ctrl-C` while editing: clear the current input and continue.

The client consists of three independent boundaries:

- `pal.tty.session.SocketSession` owns framing and request-id demultiplexing.
- `pal.tty.ui.TtyRepl` owns the interactive lifecycle.
- `pal.tty.render.TtyRenderer` owns Prompt Toolkit-safe Rich output.

`pal.socket_client` remains the public facade. Its non-interactive
`send_message` function deliberately keeps plain stdout/stderr streaming.
