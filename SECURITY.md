# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly by emailing **8546900+1-true-prod@users.noreply.github.com** instead of opening a public issue.

You should receive a response within 48 hours. Please include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact

## Scope

claudio runs locally on macOS. By default it talks only to a local Ollama server and makes no
external network requests. The one exception is opt-in: with `"decider": {"backend": "jev"}` it
sends redacted phrase and command summaries to OpenRouter, using a key you put in
`~/.claude/claudio.env` (see the README's Jev section for exactly what is sent). The primary
security surface is:

- Hook scripts executed by Claude Code on every tool call, permission prompt, stop and prompt
- The installer's edit of `~/.claude/settings.json` (hooks only; backed up first, other entries
  untouched, no permissions granted)
- Ollama API calls to localhost, and OpenRouter calls when Jev is enabled
- Audio file caching and session state in `~/.claude/`
