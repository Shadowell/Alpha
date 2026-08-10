# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `main` / latest release | Yes |
| Older tagged releases | Best effort |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports (credential leaks, remote code paths, auth bypasses, unsafe deserialization, etc.).

Prefer one of:

1. GitHub Security Advisory: [Report a vulnerability](https://github.com/Shadowell/Alpha/security/advisories/new)
2. Email the maintainer via the address on the [GitHub profile](https://github.com/Shadowell)

Include:

- Affected component / file path if known
- Steps to reproduce
- Impact assessment
- Whether a patch / PoC is available

We aim to acknowledge reports within 7 days and coordinate a fix + disclosure timeline.

## Safe contribution notes

- Never commit `.env`, API tokens, cookies, or private market-data credentials
- Prefer mocked / fixture-based tests over live network calls in CI
- Treat LLM / MCP tool surfaces as untrusted input boundaries
