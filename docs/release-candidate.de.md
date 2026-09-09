# Release-Kandidat 0.4.0rc1

Dieser Kandidat enthält die überarbeitete Überschussregelung und optionale
Planungsfunktionen. Er läuft derzeit im Praxistest. Die stabile Version 0.4.0 folgt
nach Auswertung dieses Tests.

Enthalten sind persistente Tageszähler, korrigierte Gerätezuordnung und
Zustandswechsel, Hybrid-/Battery-First-Bilanzen, getrennte Solar- und Netzbudgets,
Stromstellintervalle und Freigabebedingungen. Hinzu kommen Startverzögerungen,
beobachtete Phasenzahlen, Prognoseaggregation und begrenzte Restlaufzeit-Anforderungen.
Batteriedrosseln werden auch nach Neustarts und vorübergehenden Aktuatorfehlern
zuverlässig freigegeben.

Home Assistant 2025.8 oder neuer ist erforderlich. Bestehende Einstellungen und
Entity-IDs bleiben erhalten; neue Optionen sind zunächst ungesetzt. Preise werden
als vollständiger Endkundenpreis in Währung/kWh angegeben. Positive
Abschaltschwellen halten einen solaren Einspeisepuffer frei.

Ein Restlaufzeitsensor liefert noch benötigte Minuten und schließt ein positives
festes Tagesminimum aus. Zusammenhängende Anforderungen benötigen ein Tagesmaximum.
Explizite Morgen-Prognosen ersetzen den morgigen Anteil und müssen alle Flächen
umfassen. Bereits enthaltene Summensensoren nicht zusätzlich angeben.

Beim Wechsel von Blueprint/pyscript die alte Regelung deaktivieren, bevor beide
Regler dieselben Verbraucher steuern.

Die Prüfung umfasst beide festgelegten HA-Testumgebungen, 49 HA-Prüffälle und eine
etwa 15-stündige Beobachtung über Mitternacht ohne unerwartete Verfügbarkeitsfehler.
Die ursprünglich geplanten 24 Stunden wurden auf Wunsch des Nutzers verkürzt; ein
vollständiger 24-Stunden-Test wird damit nicht behauptet. [Testhinweise](testing.md).
Der ursprüngliche Hardware-Fall [#57](https://github.com/InventoCasa/PV-Excess-Control/issues/57)
bleibt diagnostisch offen.

Details: [Bedienelemente](generic-controls.de.md),
[Prognosen und Restlaufzeit](forecast-runtime.de.md).
