# Erkenntnisse aus anderen Wahoo-Integrationen

Stand 16.10.2026. Gesammelt aus Forks von armonge/wahoo-mcp und aus Projekten, die Pläne und Workouts über die Wahoo Cloud API schreiben. Gelesen, nicht kopiert: gradientpeak, AiWattCoach und quantified-self haben keine (klare) Lizenz. wahoolib, FitSync und wahoo_cloud_client sind MIT, ClemensHar/wahoo-mcp ist GPL-3.0.

## Forks von armonge/wahoo-mcp

| Repo | Inhalt |
|---|---|
| joaodrp/wahoo-mcp | Nur der Fix „Schemas relativ zu `__file__` laden“ (offener Upstream-PR #10). Bei uns übernommen. |
| iflow-mcp/armonge-wahoo-mcp | Spiegel ohne eigene Commits. |
| beide | Enthalten den nie gemergten Upstream-Branch `feat/artifacts` (PR #3): FIT-Dateien laden, mit `fitparse` auswerten, Karten und Charts mit `folium`/`plotly`. Für den Planer nicht nötig, aber eine Idee für Auswertung nach der Einheit. |
| ClemensHar/wahoo-mcp | Kein GitHub-Fork, aber eine Kopie. Bringt `create_workout` und den Scope `workouts_write`, sonst nichts. Kein PUT/DELETE. |

## Regeln der Produktions-API (strenger als die Doku)

Status: ✅ umgesetzt, ⏳ offen (Arbeitspaket in Klammern).

1. ✅ **`header.description` muss gesetzt sein**, obwohl das Plan-Schema sie optional nennt. Fallback: Name, max. 5000 Zeichen. *(quantified-self, gradientpeak)*
2. ✅ **Jedes Intervall braucht ein nicht leeres `targets`-Array.** Freie Abschnitte bekommen `{"type": "rpe", "low": 1, "high": 10}`. *(quantified-self, gradientpeak)*
3. ✅ **`header.ftp` muss eine Ganzzahl sein**, sobald relative `ftp`-Ziele vorkommen. Wir setzen es immer. *(quantified-self, AiWattCoach)*
4. **Nur das erste Ziel eines Intervalls** wird auf dem ELEMNT angezeigt. Leistung also zuerst. Relative HF- und Tempoziele wirken nur bei Laufband-Workouts in der Wahoo-App. *(alle)*
5. **Plan-Create und -Delete antworten in Produktion mit 200**, nicht 201. `raise_for_status()` deckt das ab. `provider_updated_at` wird beim Zurücklesen auf ganze Sekunden gekürzt. *(quantified-self)*
6. **403 heißt fehlender Scope oder fehlende Plans-Freigabe der App**, kein Grund für neuen Login. 422 ist ein Validierungsfehler und darf nicht wiederholt werden. *(quantified-self, wahoolib)*

## Pläne ersetzen statt duplizieren (AP 5) ✅

- Ablauf: `GET /v1/plans?external_id=…` → gefunden: `PUT /v1/plans/:id` mit `plan[file]`, `plan[filename]`, `plan[provider_updated_at]` und **ohne** `plan[external_id]`. Nicht gefunden: `POST`. *(quantified-self, AiWattCoach, wahoolib)*
- Mehr als ein Treffer ist ein Fehler. Treffer nur akzeptieren, wenn `external_id` passt und `deleted == false`. *(AiWattCoach, quantified-self)*
- **Gelöschte Pläne bleiben als `deleted=true` sichtbar.** Ein Neuanlegen unter derselben `external_id` ist unsicher. Lösung: Generationszähler anhängen, z. B. `pbp-2026-10-18-angels-g2`. *(quantified-self)*
- Ein Plan pro terminiertem Workout, nicht teilen. *(quantified-self)*
- Kein Projekt verlässt sich auf POST-Idempotenz. Geht eine Antwort verloren, erst per `external_id` bzw. `workout_token` suchen, dann PUT. *(quantified-self)*

## Geräte holen geänderte Pläne nur bei geändertem Workout ✅

Live gefunden am 17.10.2026, in keinem der anderen Projekte dokumentiert:

- Der ELEMNT ROAM lädt einen Plan nur neu, wenn sich das **Workout** ändert (dessen `updated_at`). Ein per PUT ersetzter Plan allein bleibt auf dem Gerät auf der alten Fassung, auch nach mehrfachem Sync.
- Wahoo setzt `updated_at` eines Workouts bei einem PUT ohne inhaltliche Änderung **nicht** neu.
- Lösung: Wird ein Plan ersetzt und das Workout bliebe gleich, verschiebt der Server den Start um eine Sekunde (wechselnd :00 ↔ :01). `push_session` macht das automatisch. `create_plan` fasst beim Ersetzen alle kommenden, nicht gefahrenen Workouts mit diesem Plan so an (1 GET + 1 PUT pro Workout).
- Der zuerst vermutete Grund (Repeat-Block, RPE-Ziel) war es nicht: v4 mit Repeat-Block erschien sofort, nachdem das Workout geändert wurde. v5 mit Repeat-Block und freiem RPE-Ausfahren kam mit dem 1-Sekunden-Trick ohne Zutun an (17.10.2026, 17:57).
- Der ROAM rechnet ein RPE-Ziel offenbar mit 0 W in IF und TSS ein (v5: IF 0,55, TSS 15). Wie sich der KICKR im ERG-Modus während eines RPE-Abschnitts verhält, ist ungetestet.

## Workouts ändern und löschen (AP 5) ✅

- Umgesetzt ohne gespeicherte IDs: Die neuesten 50 Workouts (nach Start absteigend, geplante zuerst) werden nach `workout_token` durchsucht. Live bestätigt am 17.10.2026: erneuter Push mit gleichem Token ändert das Workout, gleiche `external_id` ersetzt den Plan (PUT ohne `external_id` funktioniert).
- Variante der anderen Projekte: Gespeicherte ID per `GET /v1/workouts/:id` prüfen. Sonst `GET /v1/workouts?per_page=100` seitenweise nach `workout_token` durchsuchen (genau ein Treffer). Dann `PUT`, bei 404 auf `POST` zurückfallen. *(AiWattCoach, quantified-self)*
- `workout_token` deterministisch aus der Planer-ID bilden, z. B. `pbp-2026-10-18-angels`, damit das Wiederfinden klappt.
- Löschen: erst Workout, dann Plan. 404 beim DELETE gilt als „schon weg“. *(AiWattCoach)*
- **Workouts mit `workout_summary` nicht überschreiben oder löschen**: Die Einheit wurde schon gefahren. *(quantified-self)*
- Verknüpfung nach dem Schreiben prüfen: `GET /v1/workouts/:id/plans`. *(quantified-self)*
- Beobachtet: Die Workout-Antwort hat `plan_id` gesetzt, aber `plan_ids: []`. `GET /v1/workouts/:id/plans` liefert den Plan trotzdem, und der ELEMNT ROAM zeigt ihn an.
- Beobachtet: Die Wahoo-SYSTM-App zeigt per Cloud API geplante Workouts nicht im Kalender. Der ELEMNT ROAM zeigt sie, aber als Einheit „für heute“, obwohl sie für den Folgetag angelegt war.

## Zeit und 6-Tage-Fenster

- ✅ Fenster heute bis heute + 6 in der Zeitzone des Nutzers (bei uns `WAHOO_TIMEZONE`, Standard Europe/Berlin). *(alle)*
- ✅ `starts` als echte Ortszeit nach UTC umrechnen. Andere Projekte schicken UTC-Mitternacht, was für Europa auf den falschen Tag rutschen kann. quantified-self nimmt 12:00 Ortszeit, wenn keine Uhrzeit bekannt ist. *(quantified-self; Gegenbeispiel AiWattCoach, FitSync)*
- `day_code` weglassen: Doku und Beispiele widersprechen sich beim Bezugsdatum. *(quantified-self)*
- ⏳ Einheiten, die weiter als 6 Tage entfernt sind, später pushen statt ablehnen. gradientpeak legt dafür eine Queue mit `runAt = start − 6 Tage` an. Für uns reicht, dass der Planer bzw. der Morgenlauf täglich die nächsten 7 Tage pusht. *(gradientpeak, quantified-self)*

## Plan-JSON (AP 6) ✅

- **Wiederholungen als `repeat`-Intervall** mit Kind-`intervals` und `exit_trigger_value = Anzahl − 1` (Wahoo wiederholt *nach* dem ersten Durchlauf). Nicht ausrollen. Gesamtdauer muss alle Durchläufe zählen. *(gradientpeak, AiWattCoach, quantified-self; Gegenbeispiel FitSync)* Umgesetzt und live von Wahoo angenommen am 17.10.2026.
- Absolute Watt (unser Standard) vs. relative `ftp`-Ziele: Relative Ziele passen sich an, wenn sich die FTP am Gerät ändert. Absolute Watt sind eindeutig und entsprechen dem Planer. Wir bleiben bei absoluten Watt und rechnen `percent_ftp` serverseitig um.
- gradientpeak gibt Einzelwerten ±5 % Spielraum (`low = 0,95 × Wert`, `high = 1,05 × Wert`). Für ERG am KICKR ist ein fester Wert besser. Bei uns bleibt `low == high`, wenn nur `target_value` kommt.

## Tokens und Rate-Limits (429-Retry ✅, Rest ⏳)

- Höchstens 10 nicht widerrufene Tokens pro Nutzer und App (seit 01/2026). Nur direkt vor einem API-Aufruf refreshen und den neuen Refresh-Token immer speichern. *(wahoolib, quantified-self)*
- Ein generisches 400 beim Refresh kann vorübergehend sein. quantified-self wartet 5, dann 15 Minuten, bevor es neuen Login verlangt.
- Limits in Produktion: 200 pro 5 min, 1000 pro Stunde, 5000 pro Tag (Sandbox: 25/100/250). Bei 429 das Maximum aus `Retry-After` und `X-RateLimit-Reset` abwarten. Die Header können mehrere kommagetrennte Werte enthalten. *(wahoolib, quantified-self)*
- `GET /v1/workouts` dokumentiert nur `page` und `per_page`. Die Parameter `created_after`/`created_before` in `list_workouts` stammen aus armonge und sind undokumentiert.
