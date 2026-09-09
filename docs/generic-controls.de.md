# Bedingungen, Startverzögerung und Stromregelung

Alle neuen Einstellungen sind optional. Bestehende Konfigurationen behalten ihre Standardwerte.

| Einstellung | Bedeutung |
| --- | --- |
| `enable_condition_entity` | Binärsensor oder Eingabe-Schalter als Startfreigabe. Unbekannt/nicht verfügbar erlaubt keinen Start. |
| `enable_condition_mode` | `start_only` prüft nur neue Starts. `while_running` beendet den Betrieb bei fehlender Freigabe, auch bei `on_only`. Ein manueller Override behält seinen bisherigen Vorrang. |
| `start_delay` | Sekunden mit durchgehend ausreichendem Solarbudget vor dem Start. Fehlende Freigabe oder Leistung setzt die Wartezeit zurück. Bewusster günstiger Netzbezug, zwingender Deadline-Betrieb und Overrides umgehen sie. |
| `phase_count_entity` | Tatsächlich aktive Phasen als ganze Zahl 1–3. Bei 0/unbekannt gilt der letzte gültige Wert, anfangs die statische Konfiguration. Es wird keine Hardware-Phasenumschaltung ausgelöst. |
| `current_update_interval` | Mindestabstand in Sekunden für automatische Stromerhöhungen nach einem erfolgreichen Stellbefehl. Standard 0. |
| `current_min_change` | Kleinste automatische Erhöhung in Ampere. Standard 0. |

Senkungen und Korrekturen außerhalb der Stromgrenzen wirken sofort. Laufende
dynamische `on_only`-Verbraucher regeln bei sinkendem Überschuss bis zum Mindeststrom
herunter. `on_only` startet einen Verbraucher nicht automatisch ohne ausreichende
Freigabe und Finanzierung. Für eine sofortige Stromversorgung beim Anstecken kann
eine HA-Automation die Versorgung einschalten; danach greift die Stromregelung für
laufende Verbraucher.

Während einer Startverzögerung werden weder Leistung reserviert noch benötigte
Hilfsgeräte eingeschaltet. Reine Hilfsgeräte erhalten keine eigenen Bedingungen
oder Verzögerungen; diese gehören an ihre abhängigen Verbraucher. Normale
Abhängigkeiten beachten hingegen ihre eigenen Bedingungen und Wartezeiten.

Ein positiver `off_threshold` bis 500 W hält bei Solarbetrieb einen Einspeisepuffer
frei. Bewusst genehmigter Netzbezug folgt weiterhin seinen eigenen Regeln. Der
globale `on_threshold` ist optional: Ein Wert am Verbraucher hat Vorrang, ohne
beide Angaben bleiben die bisherigen geräteabhängigen Startabstände erhalten.

Der Schalter **Netzunterstützung erlauben** am Verbraucher und die beiden globalen
Preisgrenzen sind dauerhaft gespeichert. Preise gelten in Währung/kWh, auch bei
negativen Werten. Verwende den vollständigen Endkundenpreis einschließlich der
zutreffenden Preisbestandteile.
