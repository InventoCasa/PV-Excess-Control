# Eingang für externe Strompreisprognosen

Das Vorhersagemodell läuft unabhängig von dieser Integration. Wähle seinen
Home-Assistant-Sensor unter **Konfigurieren → Strompreise → Strompreis-Prognosesensor**.
Die Angabe ist optional und unabhängig vom Tarifanbieter. Zum Entfernen die
Auswahl leeren. Nach Abschluss der Optionen wird der Eintrag neu geladen.

Versionen 1 und 2 dienen der Beobachtung und Auswertung. Sie liefern einen Diagnosesensor
**Price Forecast** mit Quelle, Modell, Zeitstempeln und angenommenen Intervallen.
`available` bedeutet, dass Struktur und Datenfrische geprüft wurden; es bestätigt
keine Prognosegenauigkeit. Prognosen ersetzen keine bestätigten Tarifintervalle,
starten keine Ladung, ändern kein Batterieziel und geben keine Verbraucher frei.

## Sensorformat

Die Attribute des Quellsensors enthalten das unten gezeigte JSON-Objekt. Sein
Zustand kann `experimental` oder ein anderer verfügbarer Wert sein; `unknown`
und `unavailable` werden immer abgewiesen. Die Werte zeigen das Format und sind
keine aktuelle Prognose.

```json
{
  "schema_version": 1,
  "market": "DE-LU",
  "unit": "EUR/kWh",
  "price_basis": "wholesale",
  "generated_at": "2026-11-01T12:00:00+00:00",
  "expires_at": "2026-11-01T18:00:00+00:00",
  "model": "calendar-v1",
  "quality": {"status": "experimental", "automation_eligible": false},
  "intervals": [
    {
      "start": "2026-11-01T13:00:00+00:00",
      "end": "2026-11-01T14:00:00+00:00",
      "price": 0.08,
      "lower": 0.02,
      "upper": 0.14
    }
  ]
}
```

- `schema_version` ist die ganze Zahl `1` oder `2`, der Markt `DE-LU`, die Einheit
  `EUR/kWh`.
- `price_basis` ist `wholesale` oder `retail`. Großhandelspreise enthalten keine
  Gebühren und Steuern des Haushaltstarifs; dieser Eingang rechnet sie nicht um.
- Jeder Zeitstempel enthält einen Zeitzonen-Offset; UTC wird empfohlen. Die
  Erstellung darf nicht in der Zukunft liegen. Der Ablauf muss nach der
  Erstellung und höchstens sechs Stunden später liegen. Ab diesem Zeitpunkt
  wird der Datensatz abgewiesen.
- Zulässig sind 1–672 chronologische, lückenlos anschließende Intervalle mit
  jeweils 15, 30 oder 60 Minuten Dauer. Jedes Ende liegt höchstens sieben
  tatsächlich vergangene Tage nach der Erstellung. Mindestens ein Intervall
  reicht noch in die Zukunft. UTC-Vergleiche erhalten Zeitumstellungen korrekt.
- Preise sind endliche JSON-Zahlen; der Zahlenwert `0` und negative Preise sind gültig.
  Version 1 benötigt endliche Grenzen mit `lower <= price <= upper`. Version 2
  erlaubt entweder solche Grenzen oder ausdrücklich `lower: null` und
  `upper: null` gemeinsam. Beide Schlüssel bleiben Pflicht. Fehlende Preise,
  Wahrheitswerte, NaN und Unendlich werden abgewiesen.
- Jedes Intervall der Version 2 benötigt zusätzlich eine nichtleere Zeichenkette
  `source` mit höchstens 128 Zeichen. Diese Anbieterkennung wird unverändert in
  die Diagnose übernommen; die Integration interpretiert sie nicht und leitet
  daraus keine Steuerungsfreigabe ab.
- `model` ist eine nichtleere Zeichenkette mit höchstens 128 Zeichen. Unterstützt
  wird `experimental` mit dem booleschen Wert `automation_eligible: false`.
  Grenzen beschreiben empirische Streuung und keine nachgewiesene
  Wahrscheinlichkeitsabdeckung.
- Zusätzliche Metadaten sind erlaubt, werden aber nicht in den angenommenen
  Datensatz übernommen.

Ein unabhängiger Anbieter kann dieses JSON über einen Home-Assistant-REST- oder
MQTT-Sensor bereitstellen. Anbieter können dasselbe Format liefern, ohne
Modellabhängigkeiten in dieser Integration zu installieren.

## Punktprognosen in Version 2

Die übrigen Angaben bleiben gleich. Setze `schema_version` auf `2` und ergänze
`source` in jedem Intervall. Ein Intervall ohne angegebenes Unsicherheitsband
sieht so aus:

```json
{
  "start": "2026-11-01T13:00:00+00:00",
  "end": "2026-11-01T14:00:00+00:00",
  "price": 0.08,
  "lower": null,
  "upper": null,
  "source": "external-provider"
}
```

Dieser Preis wird ohne Band dargestellt. Die Integration erhält beide `null`-
Werte und die Anbieterkennung. Ein Datensatz der Version 2 darf Punktprognosen
und Intervalle mit Grenzen kombinieren; eine einzelne `null`-Grenze zusammen mit
einer Zahl ist ungültig. Version 1 behält ihre verpflichtenden numerischen
Grenzen und ihr Ausgabeformat. Für beide Versionen gelten dieselben Regeln für
Frische, zeitliche Abdeckung und reine Beobachtung.

## Diagnose und Historie

Der Diagnosesensor meldet `not_configured`, `missing_sensor`, `unavailable`,
`expired`, `no_future_intervals` oder einen Prüfgrund wie `unsupported_schema`,
`unsupported_market_or_unit`, `unsupported_quality`, `future_generation`,
`invalid_price`, `invalid_bounds`, `invalid_source` oder `noncontiguous_intervals`. Abgewiesene Daten
entfernen den angenommenen Datensatz bei der nächsten Koordinatoraktualisierung;
eine frühere erfolgreiche Prognose wird nicht als aktuell weitergeführt.

Die angenommenen Attribute enthalten `forecast_start`, `forecast_end`,
`interval_count`, Erstellungs- und Ablaufzeit sowie die Intervalle. Das große
Attribut `intervals` dieses Diagnosesensors wird nicht im Recorder gespeichert.
Den rohen Quellsensor bei großen Kurven gegebenenfalls ebenfalls vom Recorder
ausschließen.

Die Zuverlässigkeitsbewertung und eine spätere Verwendung für Batterieentscheidungen
sind eine getrennte Entwicklungsstufe. Dieser Eingang verlängert nicht den
Horizont des aktiven Planers.
