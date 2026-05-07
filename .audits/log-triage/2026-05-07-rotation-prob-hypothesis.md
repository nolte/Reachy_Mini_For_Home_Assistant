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
| Status | OFFENE HYPOTHESE — kein Code-Change am Verdacht selbst, nur Diagnose-Log eingebaut |

---

## Hypothese (high confidence)

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
