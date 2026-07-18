# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use one of these channels:

- **GitHub private disclosure:** Use the [Security tab](https://github.com/TadMSTR/matrix-dispatcher/security/advisories/new) to submit a private advisory.
- **Email:** Send a description to `security.i9v75@8alias.com` with the subject line `[matrix-dispatcher] Security Report`.

Include as much detail as possible: the affected component, steps to reproduce, and potential impact.

## Scope

**In scope:**

- Matrix credential exposure through environment variables, log output, or error messages
- Message injection allowing unauthorized messages to be sent to Matrix rooms
- Room identifier manipulation routing messages to unintended rooms
- Dependency vulnerabilities with a plausible exploitation path in matrix-dispatcher's usage

**Out of scope:**

- Vulnerabilities in the host system, the Matrix homeserver, or the `claude` CLI itself
- Issues that require attacker control of configuration or environment variables
  (operator-controlled trust boundaries, not input attack surfaces)
- Theoretical weaknesses without a realistic attack path against the dispatcher's
  message-handling or subprocess-spawning surface

## Response Expectations

| Stage | Timeline |
|-------|----------|
| Acknowledgement | Within 3 business days |
| Initial assessment | Within 7 business days |
| Fix or remediation plan | Within 30 days for critical/high; 60 days for medium/low |

This is a personal project maintained by one developer. Response times are best-effort.
If you haven't heard back within 3 business days, a follow-up email is welcome.

## Disclosure

Coordinated disclosure is preferred. Please allow time for a fix to be released before
public disclosure. The CHANGELOG documents remediated findings at an appropriate level
of detail after each release.
