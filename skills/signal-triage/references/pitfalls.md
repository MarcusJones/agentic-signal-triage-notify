# signal-triage — pitfalls & recovery

Read this when: recovering from an interrupted/timed-out run, handling very
large raw logs or batches of related documents, or when apparent duplicates
keep re-surfacing. Do not load this on a normal cron run.

## Source-id edge cases

- **Hashing rule**: `line:` ids hash the raw markdown entry exactly as it
  appears after trimming surrounding whitespace, INCLUDING the leading list
  marker (`- …`; legacy important entries may begin `! `). Hashing only the
  post-marker body creates a different id and re-surfaces duplicates.
- **Context-summary sensors** (a sensor that periodically re-emits the same
  summary body under a fresh poll id — weather digests, availability
  summaries): compare the content against today's triage log before
  surfacing. If unchanged, settle the new source id with a bookkeeping
  `none` row but do NOT append another FYI line. If such a sensor emits its
  own semantic id convention, use it consistently — never settle a
  `line:<hash>` id for a row whose established convention is semantic;
  mixing conventions creates apparent leftovers and hides the real
  idempotency key.
- **Same upstream item, two feeds** (e.g. a meeting visible as both an email
  invitation and a calendar entry): surface the calendar-native id as the
  EVENT and settle the email id as bookkeeping — one surfaced line, one
  calendar proposal.

## Performance + recovery

- **Logs grow mid-run**: after writing/proposing, run one final bulk
  `remaining_new` check against the current daily log + ledger. Only return
  `[SILENT]` or final counts after this check is zero.
- **Bulk prefilter, not per-line subprocess**: never spawn `$LEDGER seen`
  once per raw line — it can exceed tool time caps and leave a
  partially-written run. Read the SQLite ledger directly (`select source_id
  from actions` — the table is `actions`) for discovery, then do the
  required final `$LEDGER seen` guard only for the small batch you are about
  to emit. (The cron prefilter script does the bulk pass for you.)
- **Interrupted runs**: if a run dies after proposing but before writing all
  surfaced lines — query the ledger for rows created in the run window, diff
  against today's triage file, add the missing human-visible lines rather
  than re-proposing.
- **Document batches**: bulk imports arrive as many related entries plus
  follow-up "summary ready" rows. Cluster the batch into ONE surfaced
  ACTION/FYI; settle each underlying doc id plus the batch line as
  bookkeeping. A later summary-only row still deserves its own surfaced line
  if it adds materially new, action-relevant information (amounts, status,
  deadlines) — cite the document id in the text, keep `src:` = the summary
  row's own id.

## Cron execution

- Prefer plain file read/write and terminal tools over arbitrary
  code-execution tools — the latter may require interactive approval in cron
  contexts and stall the run.
- Never pipe ledger output directly into an interpreter
  (`$LEDGER recent | python3 …`) — approval guards may flag it. Run the CLI
  plainly and inspect JSON, or run one standalone script that reads the
  SQLite ledger.
- Routine context-only entries (ordinary daily weather, unchanged
  availability summaries) are bookkeeping/NOISE — settle with `kind none`,
  don't surface, unless they indicate disruptive or severe conditions.
