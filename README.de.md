<p align="center"><a href="README.md">English</a> · Deutsch</p>

# PV Excess Control

*Hinweis: Diese Übersetzung kann der englischen Version hinterherhinken — die [englische README](README.md) ist die maßgebliche Version.*

**Eine umfassende Home Assistant-Integration für intelligente Solarüberschuss-Optimierung und günstige Netztarif-Verwaltung.**

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/HA-2025.8%2B-blue)](https://www.home-assistant.io)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa)](https://github.com/sponsors/InventoCasa)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-%E2%98%95-FFDD00)](https://buymeacoffee.com/henrikic)

Der [Release-Kandidat 0.4.0rc1](docs/release-candidate.de.md) dokumentiert Migration und Prüfstand. Neue optionale [Bedingungen und Bedienelemente](docs/generic-controls.de.md) sowie [Prognose- und Restlaufzeit-Eingaben](docs/forecast-runtime.de.md) behalten bestehende Standardwerte bei.

## Über das Projekt

PV Excess Control wird entwickelt und gepflegt von Henrik Wasserfuhr, Gründer von [**InventoCasa**](https://inventocasa.de). Wir sind spezialisierte Smart Home Integratoren und entwerfen sowie implementieren komplette Home Assistant Umgebungen für Neubauten, Renovierungen und Nachrüstungen.

Diese Integration ist Open Source, weil ich daran glaube, der Community, die Home Assistant so großartig macht, etwas zurückzugeben. Sie ist auch ein Kernbestandteil meiner professionellen Installationen. Wenn du ein komplettes Smart Home von Anfang bis Ende planen, konfigurieren und in Betrieb nehmen lassen möchtest: InventoCasa übernimmt jedes Jahr eine begrenzte Anzahl von maßgeschneiderten Projekten.

→ [inventocasa.de](https://inventocasa.de/kontakt/) — Komplette Smart Home Projekte, herstellerunabhängig.

## Funktionen

### Optimierung & Planung
- **Smarte Planung** - 24-Stunden vorausschauender Optimierungsalgorithmus mit wetterabhängiger Vorplanung und konfigurierbarem Planeinfluss.
- **Prioritätsbasierte Gerätesteuerung** - Verwaltung mehrerer Geräte mit konfigurierbaren Prioritäten (1-1000).
- **Opportunitätskosten** - Berücksichtigung der entgangenen Einspeisevergütung
- **Geräteabhängigkeiten** - Abhängigkeits-Verknüpfungen zwischen Geräten (Bsp.: Pool-Filterpumpe muss laufen, wenn Pool-Wärmepumpe läuft)
- **Min/Max Laufzeit & Zeitfenster** - Zeitbeschränkungen für einzelne Geräte

### E-Auto & Batteriemanagement
- **SoC-basiertes E-Auto Laden** - Berücksichtigt den Batteriestand des E-Autos und den Verbindungsstatus
- **Zeitplan-Fristen** - Setzen von Bedingungen wie bspw. "E-Auto muss bis 7 Uhr geladen sein".
- **Dynamische Stromsteuerung** - Variable Ampere-Regelung für E-Auto-Ladegeräte und Wallboxen (6-32 A).
- **Batteriebewusste Optimierung** - Drei Strategien: Batterie zuerst, Gerät zuerst, Ausgeglichen.
- **Minimum-SoC Batterie-Schutz** - Schaltet Geräte ab, wenn der Batteriestand unter einen konfigurierten Schwellenwert fällt.
- **Batterieentladeschutz** - Begrenzt die Entladerate, wenn große Verbraucher laufen.
- **Dynamische Batterieladung** — Eine optionale Prognosekurve verschiebt das Laden in erwartete Abregelungszeiten; die reaktive Regelung hebt die Ladegrenze bei überschüssiger PV-Leistung an. Siehe [Dokumentation](docs/dynamic-battery-charging.md).

### Stromtarife & -netz
- **Tarif-Integration** - Unterstützung für Tibber, Awattar, Nordpool, Octopus Energy und generische Preissensoren.
- **Einspeiselimit-Management** - Absorbieren potenziell abgeregelter Leistung möglich, z.B. wenn Einspeisebegrenzungen gelten.
- **Netzsupplementierung** - Möglichkeit, eine bestimmte Menge an Netzstrom zu erlauben, um Geräte zusätzlich zu supplementieren.

### UI, Analyse & Integrationen
- **Solarprognose-Integration** - Solcast, Forecast.Solar und generische Prognosesensoren.
- **Umfangreiche Dashboard-Beispiele** — Erstelle dein eigenes Dashboard mit Mushroom, ApexCharts und anderen Community-Karten. [Vollständige YAML-Beispiele enthalten](docs/dashboard-examples.md).
- **Eigenverbrauchs-Analyse** - Verfolgen von Einsparungen, Eigenverbrauchsquote, Energiestatistiken.
- **Manuelle Steuerung** - Forcieren von Ein-/Ausschalten von Geräten über das Dashboard.
- **Konfigurierbare Benachrichtigungen** - Individuelle Schalter für Geräteänderungen, tägliche Zusammenfassungen, Warnungen.

## Voraussetzungen

- Home Assistant 2025.8 oder neuer
- Ein Wechselrichter, dessen Leistungssensoren in Home Assistant verfügbar sind
- [HACS](https://hacs.xyz/) für die empfohlene Installationsmethode

## Installation

### HACS (Empfohlen)

1. Öffne HACS in deiner Home Assistant Seitenleiste
2. Klicke auf das Drei-Punkte-Menü und wähle **Benutzerdefinierte Repositories** (Custom repositories)
3. Füge `https://github.com/InventoCasa/PV-Excess-Control` als **Integration** hinzu
4. Suche nach "PV Excess Control" und klicke auf **Herunterladen**
5. Starte Home Assistant neu
6. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen** und suche nach **PV Excess Control**

### Manuell

1. Lade dieses Repository herunter oder klone es
2. Kopiere den Ordner `custom_components/pv_excess_control` in dein Verzeichnis `config/custom_components/`
3. Starte Home Assistant neu
4. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen** und suche nach **PV Excess Control**

## Schnellstart

1. **Integration hinzufügen** - Einstellungen → Geräte & Dienste → Integration hinzufügen → PV Excess Control
2. **Wechselrichter konfigurieren** - Wähle Standard oder Hybrid und weise dann deine Leistungssensoren zu
3. **Strompreise konfigurieren** - Wähle deinen Tarifanbieter oder lasse es auf None
4. **Geräte hinzufügen** - Verwende die Sub-Geräte-UI der Integration, um jedes Gerät hinzuzufügen ("+"-Symbol zum Hinzufügen von Geräten)
5. **Dashboard einrichten** — Siehe die [Dashboard-Beispiele](docs/dashboard-examples.md) für fertige YAML-Konfigurationen mit beliebten Community-Karten.

Siehe die [vollständige Dokumentation](docs/) für detaillierte Anleitungen (nur auf Englisch verfügbar).

## Dokumentation

- [Installationsanleitung](docs/installation.md)
- [Konfiguration](docs/configuration/)
  - [Ersteinrichtung](docs/configuration/initial-setup.md)
  - [Sensor-Zuweisung](docs/configuration/sensor-mapping.md)
  - [Geräte hinzufügen](docs/configuration/adding-appliances.md)
  - [Strompreise](docs/configuration/energy-pricing.md)
  - [Solarprognose](docs/configuration/solar-forecast.md)
  - [Multi-Wechselrichter-Setup](docs/configuration/multi-inverter.md)
- [Funktionen](docs/features/)
  - [Batteriemanagement](docs/features/battery-management.md)
  - [Dynamische Stromsteuerung](docs/features/dynamic-current.md)
  - [E-Auto Laden](docs/features/ev-charging.md)
  - [Tarif-Optimierung](docs/features/tariff-optimization.md)
  - [Einspeisebegrenzung](docs/features/export-limiting.md)
  - [Wetter-Vorplanung](docs/features/weather-preplanning.md)
  - [Benachrichtigungen](docs/features/notifications.md)
  - [Analyse](docs/features/analytics.md)
- [Dashboard](docs/dashboard/)
  - [Dashboard-Beispiele](docs/dashboard-examples.md)
  - [Entitäten-Referenz](docs/dashboard/custom-dashboards.md)
- [Erweitert](docs/advanced/)
  - [Wie es funktioniert](docs/advanced/how-it-works.md)
  - [Prioritäten-Leitfaden](docs/advanced/priority-guide.md)
  - [Fehlerbehebung](docs/advanced/troubleshooting.md)
  - [Automatisierungs-Beispiele](docs/advanced/automation-examples.md)
- [Migration vom Blueprint](docs/migration.md)

## Architektur

Die Integration nutzt einen hybriden Ansatz aus Echtzeitsteuerung und Planung:

- **Echtzeit-Controller** (alle 30 s) - Liest Live-Sensordaten, wendet Entscheidungen des Optimierers an
- **Vorausschauender Planer** (alle 15 Min) - Erstellt optimale 24-Stunden-Zeitpläne anhand von Prognose- und Tarifdaten
- **Pure-Logic Optimierer** - Keine HA-Abhängigkeiten, vollständig durch Unit-Tests abgedeckte Entscheidungs-Engine

## Unterstütze dieses Projekt

PV Excess Control wurde entwickelt, um dir dabei zu helfen, deine Stromrechnung nachhaltig zu senken und deine Solarinvestition zu maximieren. Wenn diese Integration einen messbaren Mehrwert für dein Zuhause bringt und du die kontinuierliche Weiterentwicklung unterstützen möchtest, würde ich mich freuen wenn du mich [auf GitHub sponsern](https://github.com/sponsors/InventoCasa) möchtest oder mir [einen Kaffee spendierst ☕](https://buymeacoffee.com/henrikic). Jede Spende hilft, den Sourcecode dieses Projektes weiterhin kostenfrei und aktiv maintained zur Verfügung zu stellen.

## Mitwirken

PRs sind gerne gesehen! Bitte eröffne zuerst ein Issue, um geplante Änderungen zu besprechen. Pull Requests sollten Tests für neue Logik enthalten und die bestehende Test-Suite erfolgreich durchlaufen.

Entwicklungstests benötigen Python 3.14.2+; die festgelegte Testumgebung verwendet Home Assistant 2026.8.0. Details zu den Änderungen stehen in der [Dokumentation zu Steuerung und Neustarts](docs/stabilization.md).

```bash
pip install -r requirements_test.txt
python3 -m pytest tests/ -q
```

## Lizenz

Dieses Projekt steht unter der **GNU Affero General Public License v3.0 (AGPL-3.0)** — siehe die [LICENSE](LICENSE)-Datei für die maßgeblichen rechtlichen Bedingungen (Englisch).

**Zusammenfassung (nicht rechtsverbindlich):**

- **Private Nutzung** — vollständig kostenlos, keine Einschränkungen
- **Kommerzielle Nutzung** — Wenn Sie dies in ein Produkt oder eine Dienstleistung integrieren, müssen Sie Ihr gesamtes Werk unter AGPL-3.0 als Open Source veröffentlichen
- **Kommerzielle Lizenzierung** — Für proprietäre/kommerzielle Nutzung ohne die AGPL-Verpflichtungen [kontaktieren Sie InventoCasa](https://inventocasa.de/kontakt/) für eine kommerzielle Lizenz
