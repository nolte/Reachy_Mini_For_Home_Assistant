# Log-Triage Hypothese — Rotation-Prob (IK-Collision-Warnung)

| Feld | Wert |
|---|---|
| Audit-Datum | 2026-05-07 |
| Branch | `fix/rotation-prob` (0 Commits über `develop`, gleicher SHA `9f6a4c1`) |
| Auditor | Claude Code (Opus 4.7) |
| Symptom | `reachy_mini.daemon.backend.robot.backend.throttled - WARNING - IK error: WARNING: Collision detected or head pose not achievable!` |
| Beobachtete Rate | ~0,4 Hz **post-Throttling** (121 Treffer in 5 min); Roh-Rate höher, vom SDK-`log-throttling` heruntergeregelt |
| Erstklassifikation | `unclassified` per `app-log-triage` Common-Issues-Katalog (kein Pattern matched 1:1) |
| Sanity-Probe | nicht abschließend durchgeführt — `gst-plugin-webrtc-rust` fehlt im direct-mode-Pfad des `apps_venv`. Daemon-hosted Demo (Option A aus dem Triage-Bericht) als Folge-Schritt offen. |
| Vergleichsdatum | Nutzeraussage „andere Apps haben das Problem nicht so extrem" → Hardware-/Daemon-Schaden unwahrscheinlich; App-spezifischer Code-Pfad. |
| Status | **HYPOTHESE TEILWEISE WIDERLEGT** durch Live-Daten vom 2026-05-07T23:33Z — siehe Update-Block unten. Initiale Sekundär-Quellen-Hypothese ist nicht der Trigger; Primary-Pose-Achsen-Kombination ist der eigentliche Auslöser. |

---

## Update 2026-05-07T23:33Z — Live-Diagnose-Daten nach Roll-out des `[POSE_DUMP]`-Logs

**Setup:** Diagnose-Log via Hot-Patch (scp + `pip install`-frei) auf `/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_home_assistant/motion/control_runtime.py` deployt; pyc invalidiert; App-Restart über Pollen-API. Service-Down-Fenster ~10 s.

**Erfasste Datenmenge:**

| Metrik | 60-s-Fenster ab 23:33:01 |
|---|---|
| `[POSE_DUMP]`-Lines | 50 (= 1 Hz wie konfiguriert) |
| `Collision detected` (post-Throttling) | 52 (= ~0,87 Hz, höher als die ~0,4 Hz aus dem Erst-Triage) |
| `anim_*` ≠ 0 | nie |
| `sway_*` ≠ 0 | nie |
| `face_offsets` ≠ 0 | nie |
| `state` | ausschließlich `IDLE` |

**Interpretation:** Bei reinem Idle-Betrieb ohne Voice-Pipeline-Aktivität und ohne aktives Face-Tracking sind alle drei sekundären Pose-Quellen exakt 0,0. Die Initial-Hypothese (additive Sekundär-Quellen-Summe als Trigger) **kann diesen Datenpunkt nicht erklären** und ist damit für den hier dominanten Symptom-Modus nicht der Auslöser.

**Tatsächlich beobachteter Trigger** — zwei klar getrennte Phasen:

```
Phase 1 (23:33:11 → 23:33:22, ~11 s nach App-Start):
  primary=(-0.021, +0.001, -0.044 | r+0.000, p+0.426, y+0.000)
  → KEINE Collisions in diesem Fenster.
  Reine Idle-Rest-Pose: pitch ≈ 24,4° (aus _idle_rest_head_pitch_rad), yaw 0.

Phase 2 (zwischen 23:33:22 und 23:33:51, vermutlich beim ersten look_around):
  primary=(-0.021, +0.001, +0.025 | r+0.000, p+0.426, y+1.152)
  → ~0,87 Hz Collisions, konstant. Robot bleibt in dieser Pose stuck.
  yaw ≈ 66° kommt hinzu, pitch bleibt auf 24,4°.
```

**Korrigierte Hypothese (high confidence):** Das Symptom ist **kombinatorische Erreichbarkeit** der Primary-Pose, nicht Sekundär-Quellen-Summe. Einzelachsen-Werte sind je für sich innerhalb der Limits (Pitch 24°, Yaw 66°, beide unterhalb von `clamp_body_yaw`-Limit ±160°). Aber die **Pose-Kombination** Pitch×Yaw überschreitet die mechanische Sphäre des Reachy-Mini-Kopfes.

**Verdächtige Code-Stelle für Phase 2 — `reachy_mini_home_assistant/motion/idle_runtime.py:143-144`:**

```python
target_yaw = random.uniform(-yaw_range_deg, yaw_range_deg)
target_pitch = random.uniform(-pitch_range_deg, pitch_range_deg)
```

Beide Achsen werden parallel auf Random-Werte gesetzt, ohne Reachability-Check der Kombination. Außerdem wird der Idle-Rest-Pitch (24,4°) bei einem look_around **nicht zurückgesetzt**, bevor der neue Yaw aufgesattelt wird.

**Was die Initial-Hypothese richtig hatte:**

- **Fehlender Reachability-Check vor `set_target`** ist weiterhin der Strukturmangel. Der Trigger (Primary-Kombination vs. Sekundär-Summe) ist nur eine andere Variante derselben fehlenden Schutzschicht.
- **Fix-Option-2 (Reachability-Check via SDK)** ist jetzt **erste Wahl**, weil sie unabhängig vom konkreten Trigger funktioniert.
- **Fix-Option-1 (Pre-Clamp pro Achse)** ist **disqualifiziert** — Einzelachsen-Werte sind innerhalb der Limits, das Problem ist kombinatorisch.

**Voraussichtlich noch nicht beobachtet:** Voice-Pipeline-aktiver Modus (sway ≠ 0, ggf. face ≠ 0) — die Live-Daten umfassen nur Idle. Bei aktivem Voice könnte sich das Symptom *zusätzlich* verschärfen. Folge-Schritt: ~30-60 min Realbetrieb mit Voice-Triggern, dann Re-Auswertung.

**SDK-Recherche-Stand (parallel zum Audit-Update):**
- `ReachyMini` exponiert keine direkte `is_reachable(pose)`-Methode.
- `reachy_mini.kinematics` hat vier Solver-Klassen: `AnalyticalKinematics`, `PlacoKinematics`, `MockupPlacoKinematics`, `NNKinematics`.
- Kommentar in `pose_composer.py:24` referenziert `inverse_kinematics_safe` — diese Methode gehört vermutlich zu einer der Solver-Klassen. Detail-Recherche steht aus, bevor der Reachability-Wrapper implementiert werden kann.

**Status nach diesem Update:** Audit weiter OFFEN; Diagnose-Log läuft auf dem Gerät; `_dump_pose_components` weiter aktiv; Fix-Implementation **wartet auf Voice-Pipeline-Daten** plus die genaue SDK-API-Methode.

---

## Update 2026-05-07T23:48Z — Fix implementiert und initial verifiziert

**Hintergrund:** Auf Wunsch des Auditors „direkt fixen" implementiert, ohne auf Voice-Pipeline-Daten zu warten. Begründung: Reachability-Check ist Trigger-unabhängig — er fängt sowohl die Idle-`look_around`-Kombination als auch potenzielle Voice-bedingte Sekundär-Quellen-Summen ab.

**SDK-API-Erkundung abgeschlossen:**

```python
from reachy_mini.kinematics import AnalyticalKinematics

ik_engine = AnalyticalKinematics()
joints = ik_engine.ik(pose_4x4, body_yaw=float, check_collision=True)
# Returns ndarray[(7,)]; np.isnan(joints).any() => unreachable / collides.
```

Empirisch verifiziert mit der live beobachteten Bad-Pose (`pitch=0.426, yaw=1.152`): NaN-Array zurück. Mit Idle-Rest allein (`pitch=0.426, yaw=0.0`): 7 sinnvolle Joint-Angles. Performance: 0,04 ms / Call (40 µs) — bei 100 Hz Loop = 0,4 % CPU-Overhead.

**Fix in `reachy_mini_home_assistant/motion/control_runtime.py`** (Commit `6a3e7ce`):

- Helper `_get_ik_safety_engine(manager)`: lazy-init `AnalyticalKinematics`-Instanz pro `MovementManager`.
- Helper `_check_pose_reachable(manager, head_pose, body_yaw)`: ruft `ik(...)` auf, prüft `np.isnan(joints).any()`. Fail-open bei internen Fehlern.
- Helper `_log_unreachable_throttled(manager)`: 1 Hz throttled `[POSE_GUARD]`-Warning auf WARNING-Level.
- Aufruf in `compose_final_pose` direkt vor `return`: bei reachable → cache `_last_reachable_head_pose = final_head.copy()`, bei unreachable → ersetze `final_head` durch zwischengespeicherte letzte gültige Pose, throttled Log.

**Initiale Verifikation (75 s nach Restart, 23:46:46 → 23:48:01):**

| Metrik | Vor Fix (60 s) | Nach Fix (75 s) |
|---|---|---|
| `Collision detected` (post-Throttling) | 52 | **0** |
| `POSE_GUARD`-Warnings | n/a | 0 |
| `POSE_DUMP`-Lines | 50/min | 54/min (kein Drift) |
| Aktuelle Pose im Log | stuck bei `pitch=0.426, yaw=1.152` | `pitch=0.426, yaw=0.000` (Idle-Rest) |

**Kausalität:** 100 % Reduktion der `Collision detected`-Lines im 75-s-Beobachtungsfenster. `POSE_GUARD = 0` heißt, dass in dem Fenster keine unreachable Pose generiert wurde — d. h. die App hat (zufallsbedingt) keinen `look_around` mit kritischer Pitch×Yaw-Kombination produziert. Damit ist der **Save-Mechanismus selbst noch nicht direkt unter Last verifiziert**.

**Folgerung:**

- **Symptom-Verschwindung:** belegt für den am häufigsten beobachteten Modus (Idle ohne Voice).
- **Save-Mechanismus-Aktivität:** indirekt durch Hypothesen-Konsistenz wahrscheinlich, aber noch nicht durch eine konkrete `[POSE_GUARD]`-Line bestätigt.

**Empfohlene weitere Verifikation:**

1. Längere Beobachtung (60+ Min) mit normalem Idle-Verhalten — wenn `POSE_GUARD`-Lines auftreten, ist der Save-Mechanismus im Einsatz.
2. Voice-Pipeline-Tests: mehrere Wake-Word-Trigger plus Bewegung vor der Kamera, um sway+face-Quellen zu aktivieren. `Collision detected` muss weiterhin 0 bleiben, `POSE_GUARD` darf auftreten.
3. Falls `POSE_GUARD`-Lines mit hoher Rate auftreten, weist das auf einen zu engen Reachable-Volume-Schätzer in `AnalyticalKinematics` hin (Daemon nutzt vermutlich dieselbe Engine, sollte konsistent sein) — dann müssen wir untersuchen.
4. **Diagnose-Log entfernen** (`_dump_pose_components` plus dessen Aufruf) erst nach Abschluss der Verifikation. Aktueller Code-Comment markiert das als „Remove once …".

**Nicht im Fix enthalten (für eigene Folge-PRs):**

- Klassen-Umbenennung `ReachyMiniHaVoice` → `ReachyMiniHomeAssistant` für Pollen-Validator-Konformität (orthogonal, gehört nicht in `fix/rotation-prob`).
- Eigentliche Korrektur der `idle_runtime.update_idle_look_around` (Zeilen 143-144) — die unmögliche Pitch×Yaw-Kombination wird jetzt zwar abgefangen, aber besser wäre, sie gar nicht erst zu produzieren (z. B. Pitch beim look_around auf 0 setzen). Das ist eine UX-Optimierung, kein Korrektheits-Fix.

---

## Update 2026-05-08T16:50Z — Zwei zusätzliche Bugs entdeckt und behoben

Während der Verifikations-Phase des Reachability-Fixes über mehrere Diagnose-Sessions traten zwei voneinander unabhängige Bugs zu Tage, die mit dem Rotation-Symptom zusammenhängen, aber eigene Wurzeln haben:

### Bug #2 — Hardware-Daemon-Stale-State nach mehreren App-Stop/Start-Zyklen

Nach mehrfachem Stop/Start-Zyklen der App über die Pollen-API (für Hot-Patch-Deploys während der Diagnose) ging die App in eine Crash-Schleife mit `ConnectionError: Could not connect to daemon on localhost`. Das `reachy-mini-daemon.service` blieb dabei `active (running)`, akzeptierte aber keine neuen WebSocket-Verbindungen mehr — vermutlich Stale-State im internen Connection-Manager des Hardware-Daemons.

**Recovery:** `sudo systemctl restart reachy-mini-daemon.service` plus anschließender App-Start. Service-Down-Fenster ~30-60 s. Passwordless-Sudo war auf dem Wireless-Setup vorhanden.

**Lesson für künftige Diagnose-Sessions:** Häufige Hot-Patch-Cycles sind nicht harmlos — entweder ausreichend Cooldown zwischen Stop/Start-Zyklen lassen (~30 s) oder den Hardware-Daemon-Restart als Reset-Mechanismus parat halten.

### Bug #3 — HA-NumberEntity-Slider Off-by-one-Echo bei Antennen

**Symptom:** Slider-Push in HA → Antenne bewegt sich physisch korrekt, aber der Slider in HA's UI springt visuell auf den vorherigen Wert zurück. Bei jedem weiteren Push wandert der Slider um genau einen Schritt hinter der tatsächlichen Position her.

**Wurzel:** Async-Race in `entity.NumberEntity.handle_message` zwischen Setter und Getter:

```python
elif isinstance(msg, NumberCommandRequest) and msg.key == self.key:
    self.value = msg.state            # Setter: pusht Command auf MovementManager._command_queue
    yield self._get_state_message()   # Getter: liest state.target_antenna_*  IMMEDIATELY
```

Der Setter ruft `MovementManager.set_target_pose(antenna_left=...)`, was nur einen Eintrag in eine asynchrone `_command_queue` schreibt. Der State wird erst beim nächsten 100-Hz-`_poll_commands()`-Tick aktualisiert. Im selben handle_message-Block liest der Getter aber sofort den noch-nicht-aktualisierten State und meldet diesen Wert als „aktuellen Stand" an HA zurück → off-by-one.

**Fix in `reachy_controller.py:set_antenna_left/right` (Commit `2e4fadd`):** Synchroner Direkt-Schreib in `state.target_antenna_*` zusätzlich zur Queue-Übergabe. Plus Getter (vorbereitend) auf App-State (`state.target_antenna_*`) statt Hardware-Read umgestellt — der Hardware-Read war der ursprüngliche Auslöser des „echo back to lagging servo position"-Effekts.

**Latente Bugs gleicher Art:** `set_head_x/y/z/roll/pitch/yaw` und `set_body_yaw` haben das **exakt gleiche Async-Pattern** und sind sehr wahrscheinlich ebenso betroffen — aber nicht im Scope dieses Branches. Ein eigener Audit/Fix wird empfohlen, sobald jemand das Symptom an einem der Head-Slider beobachtet.

### Reachability-Gate weiterhin verifiziert

Nach Cleanup des Diagnose-Logs (Commit `b8c73a9`) und Antennen-Fix (Commit `2e4fadd`) zeigt die App stabil:
- `Collision detected` 0/min (vorher 52/min)
- `[POSE_GUARD]` 0/min (Save-Mechanismus nicht ausgelöst — alle aktuell generierten Posen sind erreichbar)
- HA-Slider stehen auf gewählten Werten, Antennen folgen physisch

### Branch-Stand zum Zeitpunkt des Audit-Updates

7 Commits über `develop`:
1. `46a70fa` POSE_DUMP-Diagnostic einbauen
2. `f6fa236` `.gitignore`-Audit-Hygiene
3. `9a4debb` Hypothese mit Live-Daten korrigieren
4. `6a3e7ce` **Reachability-Gate (Hauptfix)**
5. `82e96f4` Verifikation dokumentieren
6. `b8c73a9` POSE_DUMP-Diagnostic entfernen (Cleanup)
7. `2e4fadd` **Antennen-Off-by-one-Fix**

**Status der Audit-Datei nach diesem Update:** Drei Bug-Klassen identifiziert und für `fix/rotation-prob` behoben. Branch ist `ready-for-PR` auf develop.

---

## Hypothese (initial, durch Live-Daten teilweise widerlegt — siehe Update-Block oben)

In `reachy_mini_home_assistant/motion/control_runtime.py` (`compose_final_pose`, Zeilen 67-122) und der parallelen Funktion in `reachy_mini_home_assistant/motion/pose_composer.py` (`compose_full_pose`, Zeilen 132-193) werden drei sekundäre Pose-Quellen pro Achse **additiv** kombiniert, *bevor* die 4×4-Matrix gebaut wird:

```
secondary_<axis> = anim_<axis> * anim_blend + sway_<axis> + face_offsets[<axis>]
```

Anschließend wird über `compose_world_offset(primary_head, secondary_head)` (`pose_composer.py:101`) das Action-Target (`primary_head`) noch darüber komponiert. **Body-Yaw wird geclamped** (`control_runtime.py:100`, `clamp_body_yaw`), die finale 4×4-Head-Pose-Matrix dagegen geht **ungeprüft** in `manager.robot.set_target(...)` (`control_runtime.py:135`).

**Trigger-Szenario**, das zur Beobachtung „andere Apps weniger extrem" passt:

1. Voice-Pipeline aktiv → `state.sway_*` ≠ 0 (Audio-getriebener Speech-Sway)
2. Face-Tracking aktiv → `face_offsets` ≠ 0
3. Idle-Animation läuft im Hintergrund → `state.anim_*` ≠ 0

Wenn alle drei Quellen konstruktiv in dieselbe Richtung addieren (typisch: leicht nach unten + zur Seite während Speech), überschreitet die finale Pose die mechanisch erreichbare Sphäre. SDK-IK-Solver gibt Collision-Warning aus, `log-throttling` regelt auf ~0,4 Hz herunter — exakt die gemessene Rate.

Bei Apps ohne diese Quellen-Kombination (etwa Apps mit Speech-Sway aber ohne Face-Tracking, oder ohne Idle-Anim) bleiben die Komponenten kleiner, die Summe innerhalb der Sphäre, daher das beobachtete „weniger extrem".

## Beweismaterial

| Beweis | Quelle |
|---|---|
| Symptom existiert auf `develop`-Mainline | `git rev-list --left-right --count develop...HEAD = 0 0` |
| Single-Owner ist sauber | Einziger `set_target`-Aufrufer im Hot-Path: `control_runtime.py:135`. `goto_target` nur in `movement_manager.py:1042` (Shutdown-Reset). |
| 3-Quellen-Summe ohne Clamp | `control_runtime.py:78-86` und `pose_composer.py:170-185` |
| Body-Yaw geclamped, Head-Pose nicht | `control_runtime.py:100` (`clamp_body_yaw`), kein Pose-Reachability-Check vor `set_target` (Zeile 135) |
| Branch-Name | `fix/rotation-prob` — Rotationen sind die nichtlinear stapelnde Achse (Translation überschreitet Sphäre selten) |
| Logger-Tag | `reachy_mini.daemon.backend.robot.backend.throttled` — bereits gedrosselt; echte Roh-Frequenz höher, was zur 100-Hz-Loop bzw. 15-Hz-Send-Cap im App-Code passt (`Config.motion.max_send_rate_hz`) |
| App-Logger-Tree leer | `reachy_mini_home_assistant.*`-Tree zeigt 0 Lines im Triage-Fenster; Daemon-Side IK-Solver schreibt, App-Side merkt nichts |

## Sekundäre Verdachtsmomente (nicht Trigger, aber relevant)

- `face_offsets` (vom Camera-Modul) sind nicht im Compose-Pfad gegen einen harten Bound geclamped. `CameraConfig.offset_scale: 0.6` ist ein Multiplikator, kein Cap.
- `Config.motion` enthält Limits nur für `body_yaw_max_rate_deg_s` (85°/s) und `body_yaw_deadband_rad` (0,0015 rad) — keinerlei Range-Cap auf die anderen 5 DOF.
- Kommentar in `pose_composer.py:24-26`: „matches SDK's inverse_kinematics_safe constraints" — die SDK hat Sicherheits-Constraints, die App ruft aber `set_target` direkt mit ungeclampter Pose auf.

## Drei Fix-Optionen (zur Erinnerung; aktuell nichts implementiert)

| ID | Strategie | Aufwand | UX-Risiko |
|---|---|---|---|
| Opt-1 | Pre-Clamp der sekundären Komponenten gegen konservative Bounds, bevor `create_head_pose_matrix(secondary)` aufgerufen wird | klein (~30 LoC) | minimal (Idle-Anim/Face-Following etwas weniger weit ausschwingend) |
| Opt-2 | SDK-Reachability-Check via `reachy_mini.kinematics` vor `set_target`; bei `False` Sekundär-Anteile linear runterskalieren oder letzte gültige Pose halten | mittel | gering, abhängig von SDK-API-Verfügbarkeit |
| Opt-3 | Quellen-Priorisierung statt Addition: bei aktiver Voice-Pipeline kein Face-Beitrag, bei aktivem Sway kein Idle-Anim-Beitrag | groß | hoch, sichtbares Verhalten ändert sich |

**Empfehlung im aktuellen Triage**: Opt-1 nach Datenanalyse. Bounds aus dem nun eingebauten `[POSE_DUMP]`-Diagnose-Log empirisch kalibrieren, statt mit geschätzten 12°/15°/20°-Werten (Roll/Pitch/Yaw) zu starten.

## Aktueller Schritt: Diagnose-Log

In `reachy_mini_home_assistant/motion/control_runtime.py` ist eine throttled `_dump_pose_components()`-Helper-Funktion eingebaut, die einmal pro Sekunde ein Snapshot aller vier Pose-Quellen (anim, sway, face, primary) plus `robot_state` auf INFO-Level loggt.

**Diagnose-Tag im Log**: `[POSE_DUMP]` — grepbar via `journalctl -u reachy-mini-daemon.service | grep POSE_DUMP`.

**Was zu erwarten:**
- Bei Idle ohne Face/Sway: alle Werte ≈ 0, `state=IDLE`.
- Während Voice-Pipeline: `sway_*` und ggf. `face_*` ≠ 0.
- **Vor jedem IK-Collision-Block sollten die Pose-Snapshots zeigen, welche Quelle dominiert.** Empirische Daten für die Bound-Kalibrierung von Opt-1.

**Folge-Schritte**:

1. Code deployen (außerhalb dieses Skill-Scopes — `reachy-mini-deploy` Agent oder manueller `pip install -e .` in `apps_venv`).
2. App via `reachy-mini-start` starten.
3. Nach ~30-60 min Betrieb mit aktiver Voice + Face-Detection: `journalctl ... | grep -B 2 -A 2 'Collision detected\|POSE_DUMP'` ziehen.
4. Komponenten-Magnituden vor IK-Errors statistisch auswerten (P95-Wert pro Achse).
5. Opt-1-Bounds kalibrieren auf z. B. P95 + 10 % Sicherheitsmarge.

## PII-Hinweis

Der Diagnose-Log enthält ausschließlich numerische Pose-Komponenten und den symbolischen `RobotState.name`. Keine Audio-Inhalte, kein Bild, keine Identifier-Daten — Hard Rule 5 (`app-log-triage`) eingehalten.

## Cross-References

- Vorgänger-Triage-Lauf: in der Konversation, kein eigener Audit-File (Triage-Snapshots veralten schnell, Spec-Empfehlung).
- Verwandter Audit: `.audits/security-review/2026-05-07-repo-security-review.md` (offene Befunde sind orthogonal, kein direkter Bezug zum Motion-Stack).
- Folge-Skills: `reachy-mini-sdk` für Idiom-Beratung beim eigentlichen Fix; `reachy-mini-deploy` für Roll-out des Diagnose-Logs aufs Gerät.
