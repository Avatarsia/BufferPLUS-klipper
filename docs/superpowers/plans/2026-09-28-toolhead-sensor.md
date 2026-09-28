# Druckkopfsensor: Umsetzung

**Freigegebener Ablauf:** Optionaler Sensor steuert Ankunft beim Laden und
Freigabe beim Entladen. Nach Ankunft 10 mm langsame Buffer-Foerderung,
konfigurierbar; danach Extruder-SYNC. Ohne Sensor bisheriger Ablauf.
Ein konfigurierter, aber fehlender/defekter Sensor muss klaren Fehler melden.

**Architektur:** Eigenes `buffer_toolhead.py` fuer Sensor-Workflow und
try/finally-Cleanup; Anbindung in buffer_feeder, Config und LOAD-Macro.
Physische Sensorauswertung bleibt aktiv; automatische Insert-/Runout-Aktionen
werden fuer den Workflow unterdrueckt und danach wiederhergestellt.
HALL/JAM/HALT bleiben wirksam, keine Pin-Doppeldefinition.

- [x] Tests zuerst: 10-mm-Default/Override, Sensorfehler, Load-Reihenfolge,
  Unload-Sensorfreigabe, Zeit-/Distanzlimits, Cleanup bei Fehlern.
- [x] Sensor-Koordinator implementieren; SYNC-Moves kurz und abgeschlossen
  ausfuehren, damit freie Sensormeldung weitere Extruder-Retracts stoppt.
- [x] Config `load_sensor_to_extruder` (mm, Default 10), Mux-Command und
  bedingte Macro-/Unload-Anbindung; kein Sensor bleibt Legacy.
- [x] Fehlertexte und Config-Beispiel dokumentieren.
- [x] Testsuite, C-Disable-Regression und unabhaengiger Review.

**Randfaelle:** Bereits belegter Sensor ueberspringt Fast-Load und Nachlauf;
bereits freier Sensor ueberspringt synchronen Unload. Fehlendes Signal muss
innerhalb Zeit/Distanz abbrechen. Kein stiller Fallback bei konfiguriertem
Sensorfehler. Externe Insert-Makros duerfen keinen rekursiven Load starten.
Es gibt keinen Commit/Push ohne gesonderten Pre-Push-Workflow.

**Verifikation:** 669 Pytest-Tests bestanden, 8 uebersprungen; beide Stock-C-
Disable/Enable-Regressionen bestanden. Sensorloses LOAD-Macro in drei
HALL/Overflow-Konstellationen gegen HEAD verglichen: identische Commands.
Unabhaengiger Review abgeschlossen; Active-Extruder-Pruefung mit Regression
abgesichert. Kein Test am physischen Drucker, kein Deployment oder Push.
