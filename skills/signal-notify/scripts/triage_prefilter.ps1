# triage_prefilter.ps1 — Windows wrapper (PowerShell 7+). Same contract as the .sh twin.
Set-Location -Path $PSScriptRoot
uv run python "triage_prefilter.py"
exit 0
