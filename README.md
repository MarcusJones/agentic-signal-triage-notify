# signal-triage-notify

A three-layer situational-awareness system for [Hermes Agent](https://hermes-agent.nousresearch.com/): cheap no-LLM sensors collect raw signals (email, calendar, GitHub, weather, …), a **gated, batched** LLM triage pass classifies what's new, and a notify layer dispatches the handful of things that actually deserve a calendar entry or an interrupt.

Since v0.2 the LLM layers are behind deterministic **wake gates**: frequent polls cost zero tokens when there is nothing to judge, new items are batched by a content-blind age debounce, and pure bookkeeping is settled without waking a model at all. On the reference deployment this cut LLM runs by ~90% while *improving* worst-case alert latency.

Packaged as a Hermes **tap** — a `SKILL.md`-leaf-dir repo, installable in one command, following the [agentskills.io](https://agentskills.io) open standard.

```mermaid
flowchart LR
    subgraph L1["Layer 1 · Sensors — cheap, no LLM (cron)"]
        direction TB
        S1[gmail]
        S2[gcal]
        S3[ics]
        S4[github]
        S5[weather]
    end
    subgraph L15["Layer 1.5 · Prefilter — no LLM (gate + batch)"]
        PF["derive ids · diff vs ledger<br/>debounce · wakeAgent gate"]
    end
    subgraph L2["Layer 2 · Triage — gated LLM sweep"]
        T["classify & propose<br/>(reads policy.yaml)"]
    end
    subgraph L3["Layer 3 · Notify — dispatch to channels"]
        direction TB
        N1[calendar]
        N2[telegram]
    end

    S1 & S2 & S3 & S4 & S5 -->|append raw entry| DL[("daily log<br/>signals/daily/YYYY-MM-DD.md")]
    POL[/"policy.yaml"/] -.->|routing rules| T
    DL -->|entry lines| PF
    PF -->|NEW items only, or skip run| T
    T -->|write surfaced view| TV[("triage view<br/>signals/triage/YYYY-MM-DD.md")]
    T -->|propose actions| LG[("action ledger<br/>ledger.db · idempotent")]
    LG -->|pending| N1
    LG -->|pending| N2
    N1 & N2 -->|mark done/failed| LG
```

> Node labels are tool-agnostic. `signals/daily/…` and `signals/triage/…` are **relative to the resolved `signals_dir`** (XDG default `~/.local/share/signal-triage/signals/` on Linux, or an Obsidian vault if you configure one) — not a required vault path.

## Why

Most "AI assistant reads my inbox" setups either interrupt you constantly or need an LLM call per item (slow, expensive, and re-judges the same email every poll). This system separates cheap collection from judgment: Layer 1 sensors are plain scripts that run every few minutes for ~0 tokens; a Layer 1.5 prefilter deterministically finds what's *new*, batches it, and **skips the LLM entirely** when there's nothing to do; Layer 2 spends LLM reasoning only on genuinely new batches; Layer 3 is the only layer allowed to interrupt you or touch your calendar, and it never sends calendar invites to third parties.

### Poll fast, flush slow

Once empty polls are free, cadence stops being the cost driver — the wake-up is. So triage polls every 15 minutes but the LLM wakes only when the **oldest** unjudged item has waited `triage.debounce_minutes` (seeded default 30; 90 is the cost-optimal setting on the reference deployment; 0 flushes immediately). The debounce is deliberately **content-blind** — counts and timestamps only, no urgency keywords to plan ahead or rot. Notify wakes only when actionable rows are pending; bookkeeping is settled without a model. Measured effect (reference deployment, 7 days): 36 scheduled LLM runs/day → ~6–12 actual wake-ups, on cheaper per-run prompts, with urgent dispatch latency *improved* (a flush is picked up within ≤30 min instead of the old fixed hourly slots).

Escape hatches: set `prefilter: false` on a job in `registry.yaml` (declarative), or `hermes cron edit <job> --script ""` (immediate) to restore plain scheduled runs.

## Works with zero config

No Obsidian vault is required. This tool writes plain markdown to disk plus a small SQLite ledger; a "vault" is just a folder of `.md` files. On first run it resolves a sensible OS-correct location — `~/.local/share/signal-triage/` and friends on Linux (XDG), `~/Library/Application Support/signal-triage/` on macOS, `%LOCALAPPDATA%\signal-triage\` on Windows — creates the directories, and seeds a default `policy.yaml`. If you *do* use Obsidian, Logseq, or a Syncthing-synced folder, point it there with one config key: `signals_dir: ~/YourVault/signals`.

## Quickstart

```bash
hermes skills install <owner>/signal-triage-notify/signal-triage
hermes skills install <owner>/signal-triage-notify/signal-notify

# One-time bootstrap: builds the sensor venv, seeds policy.yaml, registers
# `hermes cron` jobs (sensors enabled; triage/notify created disabled so you
# can review output first).
${HERMES_SKILL_DIR}/scripts/setup.sh
```

> Verify the exact `hermes skills install` / tap-add subcommands against your installed Hermes CLI version — see `docs/writing-a-sensor.md#cli-verification`. The tap layout and `${HERMES_SKILL_DIR}` convention are confirmed by official Hermes docs; the precise install-verb spelling can drift between CLI releases.

After `setup.sh` finishes, it prints the OAuth (Gmail) and `gh` CLI credential steps for the sensors you want to enable. Enable `signal-triage` and `signal-notify` with `hermes cron resume <name>` once you've watched a few hours of raw signal output.

## What's in this repo

| Path | Purpose |
|---|---|
| `skills/signal-triage/SKILL.md` | Layer 2 — gated classify & propose (+ `references/` loaded on demand). |
| `skills/signal-notify/SKILL.md` | Layer 3 — dispatch pending actions. Bundles the whole sensor framework under `scripts/`. |
| `skills/signal-notify/scripts/` | `_sensorlib.py` (path resolution + log format + source-id derivation), `triage_prefilter.py`/`notify_prefilter.py` (Layer 1.5 gates, with `.sh`/`.ps1` launchers), `registry.yaml` + `setup.py`/`setup.sh` (bootstrap), `signal_ledger.py`, `gcal_write.py`, `bootstrap_oauth.py`, and the example sensors (`gmail`, `github`, `weather`, `ics`). |
| `docs/architecture.md` | Full three-layer design + signal-lifecycle sequence diagram. |
| `docs/writing-a-sensor.md` | The sensor contract — how to add your own. |
| `.well-known/skills/index.json` | Tap discovery manifest. |

## Included example sensors

- **gmail** — Gmail history-delta poll (read-only OAuth token).
- **github** — review requests + unread notifications via the `gh` CLI.
- **weather** — one daily context line from Open-Meteo (no auth).
- **ics** — any public/secret `.ics` calendar feed (Outlook, Google "secret address", Fastmail, …).

Writing your own sensor for a source not listed here is straightforward — see `docs/writing-a-sensor.md`.

## OAuth / credential setup

- Gmail: create a Desktop OAuth client in Google Cloud Console, enable the Gmail API, then run `bootstrap_oauth.py` on a machine with a browser (not your server) and copy the resulting `token.json` into the resolved state directory. `setup.sh` prints the exact path.
- GitHub: requires `gh auth login` already done.
- Calendar writes: `gcal_write.py` needs a full-scope Google token — see `docs/writing-a-sensor.md`.

## Versioning

Semver, tagged releases (`vX.Y.Z`), human-readable history in `CHANGELOG.md`.
The version string lives in three places (`.well-known/skills/index.json` and
both SKILL.md frontmatters); `scripts/bump_version.py` updates them together
and CI fails if they ever disagree.

## License

MIT — see `LICENSE`.
