# graph-me

graph-me indexes your own files, emails, chats and contacts on your computer, so you or your AI
assistant can ask questions about them:

- "Where is the lease PDF my landlord emailed me?"
- "When is my sister's birthday?"
- "What did the plumber quote last spring?"

The answer gives you a file path or a message, with its date and source, so you can check it.
graph-me only reads your data. It never sends, edits or deletes anything, and nothing leaves your
computer unless you turn on an AI provider yourself.

You can use it from [Claude Code](https://claude.com/claude-code) or any app that supports MCP
("use graph-me: where is my lease?"), from the terminal, or in a small web page on your machine.

!!! note "Alpha software"
    graph-me is at version 0.1.0. It runs on Linux and macOS. See the [changelog](changelog.md)
    for what changed.

## What it reads

| Source | What graph-me indexes |
| --- | --- |
| Folders on your disk | The text of PDF, Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`), Markdown, plain text, HTML, `.eml` emails, CSV, JSON and code files |
| Email and chats | Gmail, IMAP, Outlook/Microsoft 365, MBOX, WhatsApp, iMessage, Slack, Discord and more, through [msgvault](https://github.com/kenn-io/msgvault) |
| Contacts | Address books exported as `.vcf` files, or contacts synced by msgvault |

From all that, graph-me builds a small graph: the people you deal with (merged across email,
phone and chats), your folders and projects, and facts such as birthdays. Every fact keeps a link
to the files or messages it came from.

graph-me doesn't read images, audio, or scanned PDFs without a text layer.

## Where to go next

<div class="grid cards" markdown>

- **[Install](install.md)**: one command with uv, no system Python needed.
- **[Quick start](quickstart.md)**: configure your sources, build the index, ask questions.
- **[Use it from your AI assistant](assistants.md)**: the Claude Code skill and the MCP server.
- **[Privacy and safety](privacy.md)**: what stays local, what is hidden, what is never done.

</div>
