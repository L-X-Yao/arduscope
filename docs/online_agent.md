# Online analysis agent

ArduScope's offline analyzers remain the authority. With a model configured,
an optional agent reads their results through read-only tools, checks each
warning and critical against the log, and writes an explanation whose claims
cite the tool calls behind them. It cannot change a status, the health score,
an `action_level` or a `.param` suggestion, and it never opens the `.BIN`
itself.

Without a model, analysis is exactly as before.

## Command line

```bash
pip install .[ai]

# Anthropic Messages API
ANTHROPIC_API_KEY=... python3 flight_report.py \
  --ai-agent --ai-provider anthropic --ai-model MODEL log.BIN

# Any service that speaks the OpenAI chat completions API
OPENAI_API_KEY=... python3 flight_report.py \
  --ai-agent --ai-provider openai \
  --ai-base-url https://api.deepseek.com --ai-model deepseek-chat log.BIN
```

`--ai-agent` requires a provider and a model; `--ai-provider rule` is refused.
The key is read from the environment, never from argv; a key saved on the
settings page (`~/.arduscope/agent.json`) is reused, and the environment still
wins.

## Web interface

1. Start the server on a loopback address (the default `127.0.0.1`).
2. Open `/settings` and fill in the API type, address, model and key.
   "Test connection" sends one tiny request.
3. Tick "Analyze with a model" before uploading a log. The choice is
   remembered in the browser; the first tick lists what will be sent.

The settings page is served only when `--host` is a loopback address. On a LAN
listener it returns 404; put the key in `~/.arduscope/agent.json` or an
environment variable before starting. The Windows zip carries both SDKs, so
the web switch works there without `pip install`.

## Tools the model may call

| Tool | Returns | Limit |
|---|---|---|
| `get_vehicle` | Type, board, firmware, motor/IMU counts, duration | 1 KB |
| `list_results` | Every analyzer card: key, status, summary | 16 KB |
| `get_result` | One card: details, evidence, suggestions, source | 8 KB |
| `get_messages` | Downsampled message series | 500 points / 8 KB |
| `get_params` | Parameter values by name or prefix | 200 rows / 8 KB |
| `get_events` | Mode changes, ERR, EV, STATUSTEXT | 200 rows / 8 KB |
| `get_spectrum` | FFT peaks for one field | 20 peaks / 4 KB |
| `get_threshold` | Threshold value and its source note | 1 KB |

Total tool output per run is capped at 120 KB. The run is capped at 20 tool
calls, 5 minutes and 8192 output tokens per reply by default. The report card
shows the tool calls, token usage and every unverified observation.

## What leaves the machine

| Data | Default | Opt-in |
|---|---|---|
| Analyzer titles, statuses, summaries, evidence | sent | — |
| Parameter names and values (on request) | sent | — |
| Message series, downsampled (on request) | sent | — |
| GPS latitude/longitude | stripped | yes |
| Board serial (STM32 UID) | stripped | yes |
| STATUSTEXT text | stripped | yes |
| Raw `.BIN` | never | — |
| Log file name | not sent; a neutral label is used | — |
| API key | only to the configured API address | — |

The first use lists these categories. The report's footnote names the
categories sent, never their contents. The key is stored in plain text at
`~/.arduscope/agent.json` (mode 0600) only if entered on the settings page;
environment variables take precedence.

## Fallbacks

| Condition | Result |
|---|---|
| No key, model or SDK | rule engine, card says why |
| Service does not support tool calls | single-shot LLM summary, card says so |
| Network error or timeout | single-shot summary; then rule engine |
| Tool budget exhausted | finish with what exists; no data → single-shot |
| Malformed submission | one retry, then single-shot |
| Untraced numbers | the model is asked to rewrite once; whatever remains is marked unverified |
| Tool error | error returned to the model; the loop continues |

The card's status is always the worst offline status, and its verdict is
never the model's.

## Acceptance check

```bash
OPENAI_API_KEY=... python3 tools/agent_eval.py \
  --provider openai --model deepseek-flash \
  --base-url https://api.deepseek.com --repeats 3
```

It runs the eight scenarios in `tests/sitl/logs`, three times each, and fails
on a run that errors, an unverified observation, or a fault scenario whose
fault is not named. Results go to `reports/agent-eval/`.

## Limitations

- No follow-up questions in v1. The web server deletes the upload when the
  analysis ends, so a conversation would need the log kept.
- Batch, multi-log and fleet reports use the offline engine only.
- Citations catch numbers that do not appear in the cited tool result, or
  follow from simple arithmetic on two cited numbers. The summary is marked
  too when it carries a number that is in none of the tool results. A
  qualitative claim can still be wrong; the card marks only what it can check.
- One real service has been tested: DeepSeek's OpenAI-compatible endpoint, on
  the eight SITL scenarios, three runs each, with no unverified number.
  Anthropic's and OpenAI's own services, and other compatible services, need a
  run with their own key.
- Log text can contain instructions. The system prompt treats it as data, and
  STATUSTEXT is not sent unless opted in, but prompt injection is not fully
  preventable.
