# Security

## Reporting a vulnerability

Use **Report a vulnerability** under the repository's Security tab. It opens a
private report that only the maintainer sees. Please do not open a public issue
for something that can be exploited.

Fixes go into the next release. Only the latest release is supported: earlier
packages stay on the Releases page as they were built and do not get fixes.

## What to know before running it

- **The web interface has no login.** It listens on 127.0.0.1 by default. With
  `--host 0.0.0.0` anyone who can reach the port can upload logs and read every
  report in the report directory. Do that only on a network you trust.
- The server refuses requests whose `Host` header is not a name it was started
  with, and cross-site requests by their `Origin`. `--allow-host` adds a name.
- A log can come from anyone. A file that is not a DataFlash log is refused,
  and a damaged one is read as far as it goes. Text from a log is escaped in
  the report, and reports are served with a Content-Security-Policy.
- The LLM diagnosis is off by default. `--ai-provider anthropic` or `openai`
  sends each analyzer's status and summary to that provider under your API
  key, with the vehicle type, firmware version, board name, motor and IMU
  counts, flight duration and health score. With `--ai-base-url` all of that
  goes to the address you give instead, and so does the key from
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
- A flight log holds the GPS track of the flight and the serial number of the
  flight controller, and so does a report made from it. Think before posting
  either in a public issue.
