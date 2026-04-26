# NOT-TO-DO

Fehlerprotokoll fuer dieses Projekt. Eintraege werden bei Session-Start gelesen.
Format: `| Datum | Tag | Fehler | Vermeidung |`

| Datum | Tag | Fehler | Vermeidung |
|-------|-----|--------|------------|
| 2026-04-24 | python, klipper, stepcompress | Detach/Reattach via set_trapq(None) reicht nicht um stepcompress.last_step_clock zu re-anchorn fuer never-stepped Stepper — vier Iterationen P7-2..P7-5 als Sackgasse | Boot-Anchor-Step (P7-18) statt Detach-Architektur. set_position triggert NICHT stepcompress_set_last_position fuer detachten Stepper. Mainline-API gibt es nicht — Workaround mit echtem Step beim Boot. |
| 2026-04-25 | klipper, macros, jinja | Jinja2-Macros mit `{% if %}`-Branches haben kein try/finally bei Klipper-error/M112 — Sync-State kann haengen bleiben | Sync/Cleanup-Logik mit guaranteed-Cleanup gehoert in Python (cmd_*) mit Python-try/finally, nicht in Macro. P7-28 BUFFER_UNLOAD_FILAMENT als Beispiel. |
| 2026-04-26 | python, refactoring | "no behavior change" Commit-Body bei 134-Zeilen-Refactor mit nur 1 Smoke-Test als Coverage = Wunschdenken | Vor groesserem Refactor Charakterisierungs-Tests fuer die kritischen Pfade schreiben. Mindestens HALL1-Bypass-Branches und Recovery-Flag-Reset-Sites characterizen. |
| 2026-04-26 | python, codex | Codex schreibt Commits direkt aber `.git/index.lock` permission deny moeglich — uncommitted Aenderungen im Worktree | Wenn Codex commit failed: Worktree-Aenderungen pruefen + selbst committen. Codex-Output liest "Du wirst benachrichtigt" auch bei Hang — git status verifizieren statt blind warten. |
| 2026-04-26 | python, fakes | FakeConfig.getboolean(`bool("0")`) ist truthy in Python (`bool("0")=True`) — Test-Mock weicht von Klipper-Realitaet ab | Klipper-konformer FakeConfig.getboolean: explizit "0"/"false"/"no" → False, "1"/"true"/"yes" → True, ValueError fuer alles andere. |
| 2026-04-26 | jinja, klipper | Macro-Dispatcher (`{% if %} CMD_A {% else %} CMD_B {% endif %}`) leitet User-Args nicht automatisch weiter | `{rawparams}` an jeden Branch anhaengen damit `UNLOAD_FILAMENT TIP_CYCLES=2` durchgereicht wird. Sonst wirken Macro-Variables nach Flag-Switch nicht mehr. |
