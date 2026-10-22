# Übergabe: wahoo-mcp weiterentwickeln

Stand 16.10.2026. Für Claude Code. Diese Datei als `docs/UEBERGABE.md` in den Fork legen und Claude Code zuerst lesen lassen.

## Ziel

Der PBP-Planer (Claude-Artifact) soll strukturierte Einheiten mit Christophers Wattzielen direkt zu Wahoo schicken. Sie sollen dann am Tag der Einheit auf KICKR BIKE SHIFT und ELEMNT ROAM bereitstehen. Die geplanten Morgenläufe von Claude laufen in der Cloud. Der Server muss deshalb per HTTPS erreichbar sein, betrieben auf dem Synology-NAS in Docker.

## Ausgangslage

- Upstream: https://github.com/armonge/wahoo-mcp (GPL-3.0, Python ≥ 3.13, uv). Letzte echte Entwicklung im Juli 2025, danach nur Dependabot.
- Tools: `list_workouts`, `get_workout`, `list_routes`, `get_route`, `list_plans`, `get_plan`, `create_plan`, `list_power_zones`, `get_power_zone`.
- Transport nur stdio. OAuth mit PKCE, Tokens in einer JSON-Datei, automatischer Refresh.
- Vergleich lohnt sich: https://glama.ai/mcp/servers/ClemensHar/wahoo-mcp hat ein Tool `create_workout` (name, starts, minutes, workout_token, workout_type_id, plan_id, route_id). Laut Beschreibung erscheint ein Plan in der ELEMNT-App nur, wenn das Workout zwischen heute und 6 Tagen in der Zukunft liegt. Vor dem Bauen prüfen, ob sich der Code übernehmen lässt.

## Befunde aus dem Code-Review (16.10.2026)

1. **Kein Terminieren.** `create_plan` legt nur einen Plan in der Bibliothek an. `POST /v1/workouts` mit `plan_id` und `starts` fehlt.
2. **Scope fehlt.** `src/auth.py` fragt `user_read workouts_read routes_read plans_read plans_write power_zones_read` an. Zum Terminieren wird vermutlich `workouts_write` gebraucht.
3. **FTP fest verdrahtet.** `WorkoutPlan.to_wahoo_format()` setzt im Header `"ftp": 250`. Relative Ziele (`ftp`, `map` …) würden damit falsch umgerechnet. Christophers FTP: 229 W (Stand 10/2026).
4. **Stille Fallbacks.** `_map_target_type` macht aus unbekannten Typen `watts`, `_map_intensity_type` macht `active`. Ein Ziel `percent_ftp: 0.95` würde zu 0,95 W.
5. **Header immer Rad indoor.** `workout_type_family` und `workout_type_location` sind fest 0, auch bei `workout_type: run`.
6. **Kein Ändern oder Löschen** von Plänen oder Workouts.
7. **Auth-Kleinigkeiten:** kein `state`-Parameter beim OAuth-Login, Token-Datei wird erst nach dem Schreiben auf 0600 gesetzt, die Erfolgsseite zeigt Tokens und Code-Verifier im Klartext.
8. Sonst sauber: nur Verbindungen zu `api.wahooligan.com`, keine Telemetrie, keine Shell-Aufrufe.

## Arbeitspakete

1. **Fork und Basis:** GitHub-Fork anlegen, `uv sync`, Tests grün (`uv run pytest`). Upstream als Remote behalten.
2. **Terminieren:** Tool `schedule_workout` (oder `create_workout`): `POST /v1/workouts` mit `name`, `starts`, `minutes`, `workout_token`, `workout_type_id` (12 = Indoor-Rad), `plan_id`. Das 6-Tage-Fenster validieren. Scope `workouts_write` ergänzen. Feldnamen gegen die offizielle API-Doku prüfen.
3. **FTP richtig:** FTP aus `list_power_zones` lesen oder per Umgebungsvariable `WAHOO_FTP` setzen. Standard für den Planer: absolute Watt.
4. **Strikte Validierung:** unbekannte Ziel- und Intervalltypen als Fehler zurückgeben statt umdeuten. Header-Felder aus `workout_type` ableiten.
5. **Ändern und Löschen:** Pläne und terminierte Workouts aktualisieren bzw. löschen, sofern die API das hergibt. `external_id` aus dem Planer nutzen (z. B. `pbp-2026-10-18-angels`), damit ein erneuter Push ersetzt statt dupliziert.
6. **Komfort-Tool für den Planer:** `push_session(date, time, name, intervals)` mit Intervallen in % FTP, das in einem Schritt Plan anlegt, in Watt umrechnet und terminiert.
7. **HTTP-Betrieb:** Streamable-HTTP-Transport zusätzlich zu stdio, Zugriff nur mit Token (Bearer oder OAuth je nach Anforderung des Claude-Konnektors). Dockerfile und `docker-compose.yml` für Synology, Token-Datei im Volume, Erreichbarkeit über Cloudflare Tunnel oder Tailscale Funnel.
8. **Auth härten:** `state` prüfen, Token-Datei mit `os.open(..., 0o600)` anlegen, Tokens nicht im Browser anzeigen.

## Vorab manuell (Christopher)

- Im Wahoo Developer Portal eine App anlegen: Client ID und Secret, Redirect-URI passend zur lokalen Auth (Standard `http://localhost:8080/callback`). Prüfen, welche Scopes freigegeben werden und ob die App sofort mit dem eigenen Konto funktioniert.
- Einmal lokal `make auth` ausführen, damit die Token-Datei entsteht.

## Abnahmetest

1. Plan „Test Z2 30 min“ mit absoluten Watt (Einfahren 110 W, 20 min 135 W, Ausfahren 100 W) anlegen.
2. Für morgen 18:00 terminieren.
3. Prüfen: Taucht er in der Wahoo-App, auf dem ELEMNT und am KICKR BIKE SHIFT auf? Stimmen die Watt?
4. Ändern und erneut pushen: Wird ersetzt statt dupliziert?

## Erster Prompt für Claude Code

> Lies docs/UEBERGABE.md. Prüf zuerst, ob ClemensHar/wahoo-mcp den Terminierungs-Teil schon sauber löst, und schlag vor, ob wir übernehmen oder selbst bauen. Danach Arbeitspakete 1–4, jeweils mit Tests. HTTP-Betrieb erst, wenn der Abnahmetest lokal per stdio klappt.
