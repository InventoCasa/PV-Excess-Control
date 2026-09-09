# Prognosequellen und verbleibende Laufzeit

Der bisherige `forecast_sensor` bleibt bestehen. Mit
`additional_forecast_sensors` können separate Dachflächen ergänzt werden. Ihre
Tageswerte und zeitlich überlappenden Leistungswerte werden summiert. Doppelte
Entity-IDs und doppelte Intervalle innerhalb einer Quelle zählen nicht mehrfach.
Ein bereits aggregierter Sensor darf nicht zusammen mit seinen Teilflächen
angegeben werden.

`forecast_tomorrow_sensor` und `additional_forecast_tomorrow_sensors` bilden
zusammen die vollständige Prognose für morgen. Wenn diese angegeben werden,
ersetzen sie den morgigen Anteil der primären Quellen. Maßgeblich ist das lokale
HA-Datum einschließlich der Zeitumstellung.

Solcast-Halbstunden- und Stundenwerte werden anhand ihrer tatsächlichen Dauer in
Energie umgerechnet. Python-Datumsattribute und ISO-Zeitstempel werden unterstützt.
Alte Zeitstempel ohne Zeitzone gelten als UTC. Bei Forecast.Solar wird die bekannte
Intervallfolge verwendet; ein isolierter letzter Wert behält aus Kompatibilität
die bisherige Ein-Stunden-Annahme. Vollständige Zeitreihen mit abschließender Null
sind daher vorzuziehen. Ein reiner Tageswert – auch von einem generischen
Summensensor – bleibt ein Tageswert und liefert keine erfundene Stundenkurve.

Fehlt eine konfigurierte Quelle, gilt die kombinierte Prognose als unvollständig.
Die fehlende Fläche wird nicht als null Erzeugung verrechnet. Die unmittelbare
Überschussregelung kann mit gültigen Live-Messwerten weiterarbeiten.

`remaining_runtime_entity` beschreibt die **noch benötigten Minuten**. Dafür kann
beispielsweise ein HA-Template einen Temperatur- oder Prozessbedarf umrechnen.
Ein positives festes Tagesminimum und dieser Restbedarf schließen sich aus.
Das verbleibende Tagesmaximum begrenzt die Anforderung. Null beendet sie;
`on_only` lässt danach die Versorgung an. Unbekannt erlaubt keine neuen
automatischen Starts und bedeutet nicht „fertig“.

`require_contiguous_runtime` hält eine begonnene Anforderung zusammenhängend und
setzt ein positives Tagesmaximum voraus. Bestehende Sicherheitsgrenzen, maximale
Laufzeiten und Zeitfenster bleiben wirksam. Änderungen des Bedarfs stoßen eine
Neuplanung an.

Bei ausdrücklich zusammenhängenden Anforderungen zählt die eingeschaltete Zeit
auch unterhalb einer Completion-Leistungsschwelle zum Tagesmaximum. Dadurch bleibt
eine festhängende Restquelle auch bei 0 W zeitlich begrenzt. Plan Confidence zeigt
Quellenstatus, Tages-/Morgensumme und Intervallzahl. Bei Ausfall einer konfigurierten
Prognose gibt die dynamische Batteriedrossel einmalig den maximalen Ladedeckel frei;
bei Erholung wird im Regelzyklus neu geplant.

Fehlt die konfigurierte Prognose nach einem Neustart, wird eine verbliebene
Ladebegrenzung nach der Startwartezeit freigegeben. Fehlgeschlagene Freigaben bleiben
ausstehend; eine später verfügbare Inverter-Entity wird ohne erneuten Reload
übernommen. Das Statusattribut `dynamic_battery_charge_release_pending` zeigt
eine noch nicht abgeschlossene Freigabe an.
