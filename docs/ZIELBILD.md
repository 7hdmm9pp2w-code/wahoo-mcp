# Zielbild: Trainingssteuerung

Stand 23.10.2026. Rahmenbedingungen abgestimmt; Übergangsbetrieb bis Februar läuft, das Zielbild ohne Claude ist abgestimmt (Abschnitt unten).

## Ziel

Jeden Morgen eine begründete Empfehlung: **Was trainiere ich heute, und passt der Rest der Woche noch?** Grundlage sind alle Daten: Rad (Wahoo, SYSTM), Lauf und Erholung (Garmin Fenix 7). Fahren und ausführen bleibt wie gewohnt: SYSTM-Einheiten mit Video in der SYSTM-App, eigene Einheiten auf dem ROAM, Läufe auf der Fenix. Claude schlägt vor und begründet. Du entscheidest, solange wir die Regeln nicht kalibriert haben.

## Quellen

| Quelle | Liefert | Zugang | Stand |
|---|---|---|---|
| Garmin Fenix 7 | Ruhe-HF, HRV, Schlaf, Body Battery, Stress, Aktivitäten (FIT) | offizielle Garmin Connect Personal API, eigener `garmin-mcp` (nur lesend, Schlüssel im Mac-Schlüsselbund) | ✅ läuft seit 23.10.2026; ohne Training Readiness, Training Status, VO2max-Verlauf |
| Wahoo Cloud | Radeinheiten von KICKR/ROAM inkl. Leistung, FTP | eigener `wahoo-mcp` | ✅ läuft (PR #1) |
| Wahoo SYSTM | Bibliothek (über 1000 Einheiten), 4DP-Profil (FTP, MAP, AC, NM), Kalender | `wahoo-systm-mcp` (inoffizielle API) | ✅ installiert; der SYSTM-Kalender ist der eine Ort für alles Geplante (Rad, Lauf, Kraft als Platzhalter) |
| Strava | alle Aktivitäten an einer Stelle | Konnektor in Claude | vorhanden, Rückfallebene |
| Familien- und Kanzleikalender | belegte Zeiten aus Familie, Christopher, Stephanie, Ferien NRW, Geburtstage und Kanzlei (nur frei/belegt, auf dem TRMNL verborgen) | vorhandene TRMNL-Kalenderbrücke `calbridge` auf dem NAS, `familie.json` (`week` und `week_next` = 14 Tage mit Start und Ende) | vorhanden; der Hub muss ins Docker-Netz von Terminus |
| Hevy (über Strava) | Hanteltraining | Strava-API (eigene App für den Hub) | Strava-Konnektor in Claude vorhanden |
| Du | Befinden, Beine, Zeit heute | kurzer Check-in im Chat | – |

## Entscheidungslogik

Bewusst einfach und nachvollziehbar, jede Empfehlung mit Begründung:

1. **Belastung**
   - Jede Einheit bekommt einen TSS: Rad aus Leistung, Lauf aus Herzfrequenz bzw. Tempo.
   - Daraus folgen die Fitness (CTL, 42 Tage), die Ermüdung (ATL, 7 Tage) und die Form (TSB = CTL − ATL).
   - Rad und Lauf zählen gemeinsam.
2. **Erholung heute**
   - HRV gegen die eigene 7-Tage-Basis, Schlafdauer, Ruhe-HF, Body Battery und dein Check-in.
   - Ergebnis ist eine Ampel: grün, gelb oder rot.
3. **Zeitfenster**
   - Training muss ins Leben passen, nicht umgekehrt.
   - Aus den Kalendern ergeben sich freie Blöcke pro Tag, nach deinen Regeln: wann Training überhaupt geht, Puffer vor und nach Terminen, Familienzeit.
   - Die Dauer einer Einheit richtet sich nach dem Fenster. Eine Schlüsseleinheit wandert auf den Tag mit Platz.
   - Ferien und Ganztagstermine verändern die ganze Woche.
4. **Plan**
   - Die Wochenstruktur mit Schlüsseleinheiten kommt aus dem PBP-Planer.
   - Periodisierung auf das Ziel-Event.
5. **Regeln**
   - Rot: frei oder locker (Z1).
   - Gelb: Schlüsseleinheit gegen Z2 tauschen oder verschieben.
   - Grün: wie geplant.
   - TSB unter −30 oder CTL-Anstieg über 7 pro Woche: Entlastung.
   - Nie zwei harte Tage in Folge, egal ob Rad oder Lauf.
6. **Auswahl**
   - Eine passende SYSTM-Einheit nach 4DP-Fokus, Dauer und TSS, eingetragen in den SYSTM-Kalender.
   - Oder eine eigene Einheit per `push_session` auf den ROAM.
   - Später auch Laufeinheiten auf die Fenix.

Die Schwellen sind Startwerte. Sie werden nach 2–4 Wochen an deinen Daten kalibriert.

## Bausteine

```
Kalender ─▶ calbridge (TRMNL) ┐
Fenix 7 ─▶ garmin-mcp (neu) ─┤
Wahoo   ─▶ wahoo-mcp ✅ ─────┼─▶ Trainings-Hub (neu) ─▶ Claude: Morgen-Check ─▶ SYSTM-Kalender / ROAM / Fenix
SYSTM   ─▶ wahoo-systm-mcp ──┘   Datenbasis + Kennzahlen      PBP-Planer
Strava  ─▶ Konnektor (Rückfall)
```

- **Ein MCP-Server pro Quelle.** Jeder liest Daten und führt aus, rechnet aber nicht.
- **Trainings-Hub**, der einzige neue Kern:
  - eine kleine Datenbank, SQLite auf dem NAS,
  - täglicher Abgleich aller Quellen,
  - einmaliger Rückimport der Historie, weil CTL mindestens 42 Tage braucht,
  - berechnet Belastung und Ampel und gibt sie als Tools aus, z. B. `get_status` oder `get_week`,
  - liest die Kalender aus `calbridge` und gibt nur **freie und belegte Blöcke** weiter, ohne Titel und Orte. Wie beim Depotschirm verlassen keine Details die Brücke, Stephanies Termine schon gar nicht.
  - Grund: Claude soll nicht jeden Morgen Monate an Daten neu abrufen. Wahoo erlaubt in der Sandbox nur 250 Anfragen pro Tag.
- **Logik in Claude**, also Morgen-Check und Wochenplanung, gestützt auf den Hub und den PBP-Planer.

## Reihenfolge

| Phase | Was | Nutzen |
|---|---|---|
| 0 | Aufräumen: PR #1 mergen, HTTP-Arbeit (AP 7) sichern | nichts geht verloren |
| 1 | `wahoo-systm-mcp` installieren, Einheiten auswählen und einplanen | sofort im Alltag nutzbar |
| 2 | Garmin-Personal-Key holen, `garmin-mcp` nur lesend: Läufe, HRV, Schlaf | klären, was die API wirklich liefert |
| 3 | Trainings-Hub: Datenbank, Abgleich, Rückimport, CTL/ATL/TSB, Ampel | gemeinsame Sicht auf Rad und Lauf |
| 4 | Morgen-Check in Claude, manuell ausgelöst, 2–4 Wochen beobachten und kalibrieren | Regeln passen zu dir |
| 5 | Automatisieren: Hub und Server auf dem NAS (AP 7), Claude-Routine am Morgen; optional Garmin Training API (Stufe Startup) für Laufeinheiten | läuft ohne dich anzustoßen |

## Rahmenbedingungen (abgefragt am 22.10.2026)

| Thema | Festlegung |
|---|---|
| Ziel | regelmäßige Brevets, einmal im Jahr ein großes (nächstes: **PBP 2027**, August, mit Brevet-Serie zur Qualifikation), dazu sehr langfristig fit bleiben |
| Umfang | **6–8 Stunden pro Woche**, alles zusammen |
| Laufen | Ausgleich, das Rad hat Vorrang |
| Kraft | **Hanteltraining 2× pro Woche, etwa 30 Minuten**; erfasst in Hevy, von dort zu Strava |
| Werktags | früh vor der Arbeit, **10–12 Uhr** (wenn die erste Arbeit erledigt ist) oder abends; höchstens **90 Minuten** pro Einheit |
| Wochenende | **Samstag nachmittags** und **Sonntag vormittags** |
| Sonntag | Laufen, außer es steht eine lange Ausfahrt an; dann wandert der Lauf auf Samstag |
| Indoor/Outdoor | werktags meist KICKR mit SYSTM, gelegentlich draußen; am Wochenende draußen oder KICKR; Brevets draußen |
| Puffer | **30 Minuten** vor und nach Terminen |
| Tabu | **13–15 Uhr** (Kochen und Mittagessen mit der Familie); kein hartes Training am **Vorabend eines Gerichtstags** |
| Autonomie | **Vorschlag + Klick**: Claude schlägt vor und begründet, du bestätigst, dann wird eingeplant (SYSTM-Kalender, ROAM) |
| Morgen-Check | **automatisch um 6:00 Uhr** |
| Planung | **rollend täglich**, immer die nächsten 7 Tage |
| Gesundheitsdaten | auf dem NAS; Claude darf alles sehen, bis das später eine lokale KI übernimmt |
| Gesundheit | keine bekannten Einschränkungen |

### Folgen für den Aufbau

- **Strava wird wichtiger:** Über Strava kommen die Hevy-Krafteinheiten herein. Für den Hub auf dem NAS reicht der Strava-Konnektor in Claude nicht. Der Hub braucht eine eigene Strava-API-App, kostenlos und nur für dich.
- **Frühe Einheiten und der 6-Uhr-Check:** Eine Einheit vor der Arbeit beginnt womöglich vor 6 Uhr. Deshalb kommt der Vorschlag für den nächsten Tag schon mit der rollenden Planung am Vortag. Der 6-Uhr-Check passt ihn nur noch nach Schlaf und HRV an.
- **Kraft als eigene Einheit:** Hanteltraining wird nicht über TSS gezählt, aber als harter Reiz behandelt: nicht direkt vor einer Schlüsseleinheit, gern an lockeren Tagen.
- **Brevets im normalen Kalender:** Brevets kommen nach und nach dazu, sobald du dich anmeldest; der erste ist im Dezember 2026. Sie stehen in deinem normalen Kalender, mit **„Brevet“ und der Distanz im Titel**, z. B. „Brevet 200 km Köln“. Der PBP-Planer findet sie heute schon über die Kalendersuche nach „BRM“ bzw. „… km“ und bietet sie zur Übernahme an; ein Titel wie „BRM 200 Köln“ ist daher am sichersten. Ohne Claude übernimmt das eine kleine Sicht `ziele` in `calbridge`: 12 Monate voraus nur dieses Stichwort, Datum und Distanz, sonst nichts. Brevets sind die Fixpunkte der Periodisierung: Vorbelastung davor, Erholung danach.

## Übergang bis Februar (mit Claude)

- **Leitstand ist der PBP-Planer** (Claude-Artefakt): Makroplan, Regelwerk, Zeitfenster aus Fastmail (Kanzlei nur als belegt), Abgleich mit Strava, CTL/TSB, Coach-Notiz.
- **Morgens** schlägt der Training-Chat in Claude Desktop vor, du bestätigst, er plant in SYSTM ein bzw. schickt die Einheit auf den ROAM.
- **Erholung** kommt aus `garmin-mcp` (HRV, Schlaf, Ruhepuls, Body Battery).
- **Geparkt:** NAS-Betrieb mit HTTP und Login (PR #2), Trainings-Hub.

## Zielbild ohne Claude (ab Mac Studio, abgestimmt am 23.10.2026)

**Faustregel: Das NAS sammelt und rechnet, der Mac Studio denkt mit.**

```
Wahoo · SYSTM · Garmin · Strava · Kalender
            │  MCP-Server, unverändert übernommen
            ▼
NAS (läuft immer)                         Mac Studio (läuft immer)
├─ Trainings-Hub: Datenbank, Abgleich     ├─ lokales Sprachmodell
├─ Rechenlogik: CTL/ATL/TSB, Ampel,       │  nutzt dieselben MCP-Server
│  Zeitfenster, Makroplan, Regelwerk  ◀───┤  und liest Kennzahlen aus dem Hub
├─ Planer-Webseite (aus dem Artefakt)     └─ schreibt Vorschlag + Begründung
├─ TRMNL-Kachel „Heute“                       in den Hub
└─ Ausführen: SYSTM, ROAM, später Fenix
```

**Ablauf am Morgen**

1. **5:30, NAS:** Abgleich aller Quellen in den Hub, Kennzahlen neu rechnen.
2. **6:00, Mac Studio:** Die lokale KI liest Hub und freie Zeitfenster, schreibt den Vorschlag mit Begründung und legt ihn im Hub ab.
3. **Danach:** Der Vorschlag erscheint auf der Planer-Webseite und als TRMNL-Kachel. Auf deinen Klick wird in SYSTM bzw. auf den ROAM eingeplant.

**Grundsätze**

- **Rechnen im Code, nicht in der KI.** Belastung, Ampel und Zeitfenster sind feste, getestete Regeln auf dem NAS. Die KI formuliert, begründet und reagiert auf Ausnahmen („Knie zwickt“, „nur 40 Minuten“), rät aber keine Werte.
- **Der Planer wird eine Web-App auf dem NAS.** Logik und Oberfläche ziehen aus dem Artefakt um, die Daten kommen aus dem Hub statt über Claude-Konnektoren. Die Bedienung bleibt: Heute, Woche, Ausfälle, Brevetkarte.
- **Die MCP-Server bleiben.** `wahoo-mcp`, `garmin-mcp` und `wahoo-systm-mcp` laufen lokal über stdio und ziehen unverändert um.
- **Das NAS bleibt die Datenbasis**, obwohl der Mac Studio immer läuft: Dort laufen schon `calbridge` und Terminus, dort liegen die Sicherungen, und die Daten hängen nicht an Updates oder Neustarts der KI-Maschine.

**Vorbereitung bis dahin**

- Den Zustand des Planers (Ausfälle, Brevets, Einstellungen, Erledigt-Häkchen) regelmäßig als JSON sichern.
- Den Garmin-Schlüssel rechtzeitig erneuern; er läuft nach der gewählten Frist ab.

**Offen**

- Wie du mit der lokalen KI sprichst: Webseite, Mac, Handy, iMessage oder Mail. Das wird so gebaut, dass weitere Ausgänge nur angehängt werden müssen.

Keine medizinische Beratung: Bei Krankheit oder Schmerzen gilt das nicht, dann entscheidest du bzw. dein Arzt.
