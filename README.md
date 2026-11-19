# EmotionKit — Home Assistant Integration

Bringe CS2-Spielereignisse direkt in dein Smart Home. Die EmotionKit-Integration verbindet Home Assistant mit [emotionkit.de](https://emotionkit.de) und stellt Echtzeit-Game-Events als Automations-Auslöser und Entitäten bereit.

## Features

- **Einfache Einrichtung** — Claim-Code eingeben, fertig. Kein Token, kein YAML.
- **Native Automationen** — Alle CS2-Ereignisse als Geräte-Auslöser im Automations-Editor.
- **Entitäten** — Bombe, Rundenphase, Karte, Spielstand sowie Leben, Rüstung und Geld des Besitzers.
- **Volle Flexibilität** — Steuere jede beliebige Home-Assistant-Entität mit HA-Bordmitteln.
- **Kein separater Service** — Läuft direkt in Home Assistant, kein Docker-Container nötig.

## Voraussetzungen

- Home Assistant **2025.8** oder neuer
- Ein Konto auf [emotionkit.de](https://emotionkit.de) mit eingerichtetem CS2-Client

## Installation

### Über HACS (empfohlen)

1. Öffne HACS in Home Assistant
2. Klicke auf **⋮** → **Benutzerdefinierte Repositories**
3. Füge `https://github.com/roberteinhaus/hass-emotionkit` hinzu und wähle den Typ **Integration**
4. Installiere **EmotionKit**
5. Starte Home Assistant neu

### Manuell

1. Kopiere den Ordner `custom_components/emotionkit` in dein Home-Assistant-Verzeichnis `config/custom_components/`
2. Starte Home Assistant neu

## Einrichtung

1. Gehe zu **Einstellungen** → **Geräte & Dienste** → **Integration hinzufügen**
2. Suche nach **EmotionKit**
3. Gib einen Gerätenamen ein
4. Ein **Claim-Code** wird angezeigt (z. B. `ABCD-5HKL`)
5. Öffne die EmotionKit-Weboberfläche unter https://emotionkit.de, füge ein Gerät hinzu und gib den Code dort ein
6. Der Dialog in Home Assistant geht von selbst weiter, sobald der Code eingegeben wurde

Ein Code ist 15 Minuten gültig. Läuft er ab, zeigt der Dialog automatisch einen neuen an.

## Auslöser

Alle Auslöser stehen im Automations-Editor unter **Gerät → EmotionKit** zur Verfügung.

### Runde und Bombe

| Auslöser | Beschreibung |
|----------|--------------|
| `bomb_planted` | Bombe wurde gelegt |
| `bomb_defused` | Bombe wurde entschärft |
| `bomb_exploded` | Bombe ist explodiert |
| `freezetime` | Freezetime (Kaufzeit) hat begonnen |
| `round_live` | Runde gestartet (Freezetime vorbei) |
| `round_over_t` | Terroristen haben die Runde gewonnen |
| `round_over_ct` | Counter-Terrorists haben die Runde gewonnen |

### Match

| Auslöser | Beschreibung |
|----------|--------------|
| `match_start` | Aufwärmphase vorbei, das Match beginnt |
| `halftime` | Halbzeit |
| `match_over` | Match beendet |
| `match_won` | Match gewonnen (aus Sicht des Besitzers) |
| `match_lost` | Match verloren (aus Sicht des Besitzers) |

### Spieler

Diese Auslöser feuern standardmäßig nur für den **Besitzer** des Geräts — also für dich, nicht für Mitspieler, die das Gerät mitbenutzen. Das lässt sich in den Optionen ändern.

| Auslöser | Beschreibung |
|----------|--------------|
| `player_kill` | Kill |
| `player_headshot` | Kill per Kopfschuss (zusätzlich zu `player_kill`) |
| `ace` | Alle Gegner in einer Runde ausgeschaltet |
| `player_death` | Ausgeschaltet worden |
| `player_flashed` | Geblendet |
| `player_low_health` | Unter 25 HP gefallen |
| `player_mvp` | MVP der Runde |
| `match_left` | Match verlassen (zurück im Menü, Verbindung getrennt) |

### Sonstiges

| Auslöser | Beschreibung |
|----------|--------------|
| `idle` | Seit der eingestellten Zeit sind keine Spieldaten mehr angekommen |

### Ereignisdaten

Jeder Auslöser ist ein Ereignis `emotionkit_event` auf dem Home-Assistant-Bus. Neben `device_id` und `type` enthält es, soweit vorhanden:

| Feld | Bedeutung |
|------|-----------|
| `round` | Rundennummer |
| `side` | Betroffene Seite (`CT` oder `T`), z. B. der Sieger |
| `reason` | Grund des Rundenendes (`elimination`, `bomb`, `defuse`, `time`) |
| `value` | Zahlenwert, z. B. Kills in dieser Runde, Blendstärke (0–255) oder HP |
| `subject` | SteamID des betroffenen Spielers |
| `headshot` | `true` bei einem Kopfschuss-Kill |
| `match_id` | Kennung des Matches |
| `resync` | `true`, wenn das Ereignis wegen einer kurzen Verbindungsunterbrechung verspätet gemeldet wird |

In Vorlagen erreichst du die Felder über `trigger.event.data`, z. B. `{{ trigger.event.data.round }}`.

## Entitäten

| Entität | Typ | Beschreibung |
|---------|-----|--------------|
| Im Match | Binärsensor | An, solange ein Match läuft |
| Bombe gelegt | Binärsensor | An, solange die Bombe liegt |
| Rundenphase | Sensor | `freezetime`, `live` oder `over` |
| Karte | Sensor | Name der Karte, z. B. `de_dust2` |
| Punkte CT / Punkte T | Sensor | Spielstand |
| Runde | Sensor | Nummer der laufenden Runde |
| Leben, Rüstung, Geld | Sensor | Werte des Besitzers |
| Spielereignis | Ereignis | Spiegelt die Auslöser als Ereignis-Entität |

Außerhalb eines Matches sind die Binärsensoren aus und die Sensoren „unbekannt“. Ist die Verbindung zu EmotionKit unterbrochen, sind die Entitäten „nicht verfügbar“.

## Optionen

Unter **Einstellungen** → **Geräte & Dienste** → **EmotionKit** → **Konfigurieren**:

- **Leerlauf-Timeout** — Nach so vielen Sekunden ohne Spieldaten feuert der Auslöser `idle`. `0` schaltet ihn ab. Standard: 30 Sekunden.
- **Auf alle Spieler reagieren** — Spieler-Auslöser feuern dann für alle Spieler, die das Gerät mitbenutzen, statt nur für den Besitzer.

Name und Leerlauf-Timeout lassen sich auch in der EmotionKit-Weboberfläche ändern; die Integration übernimmt die Änderung.

## Automationen

Nach der Einrichtung kannst du Automationen direkt im HA-Editor erstellen:

1. Gehe zu **Einstellungen** → **Automatisierungen & Szenen** → **Automatisierung erstellen**
2. **Auslöser**: Gerät → EmotionKit → z. B. „Bombe gelegt“
3. **Aktion**: Beliebige HA-Aktion (Licht, Szene, Skript, …)

### Beispiel: Bombe → Licht rot

```yaml
triggers:
  - trigger: device
    device_id: <dein_emotionkit_gerät>
    domain: emotionkit
    type: bomb_planted
actions:
  - action: light.turn_on
    target:
      entity_id: light.wohnzimmer
    data:
      color_name: red
      brightness: 255
```

### Beispiel: Blendgranate → Licht kurz voll aufdrehen

```yaml
triggers:
  - trigger: device
    device_id: <dein_emotionkit_gerät>
    domain: emotionkit
    type: player_flashed
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.value | int(0) > 150 }}"
actions:
  - action: light.turn_on
    target:
      entity_id: light.schreibtisch
    data:
      brightness: 255
      color_temp_kelvin: 6500
      flash: short
```

### Beispiel: Szene, solange ein Match läuft

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.<dein_gerät>_im_match
actions:
  - action: scene.turn_on
    target:
      entity_id: >-
        {{ 'scene.gaming' if trigger.to_state.state == 'on' else 'scene.normal' }}
```

## Problemlösung

- **„Erneute Authentifizierung erforderlich“** — Der EmotionKit-Server akzeptiert die Zugangsdaten nicht mehr, meist weil das Gerät in der Weboberfläche gelöscht wurde. Folge dem Dialog: Du bekommst einen neuen Claim-Code. Gerät, Entitäten und Automationen in Home Assistant bleiben erhalten.
- **Andere Broker-Adresse** — Über **Neu konfigurieren** lässt sich die Adresse des MQTT-Brokers ändern. Unterstützt werden `tcp://host:1883` und `mqtt://` (unverschlüsselt), `tls://host:8883`, `ssl://` und `mqtts://` (TLS) sowie `ws://host/pfad` und `wss://host/pfad` (MQTT über WebSockets). Ein leeres Feld verwendet wieder die Adresse, die der EmotionKit-Server meldet.
- **Fehlersuche** — Auf der Geräteseite lässt sich über **Diagnose herunterladen** ein Bericht erzeugen. Zugangsdaten sind darin geschwärzt.

## Entwicklung

Die Logik der Integration (Protokoll, Zustandsverfolgung, Auslöser-Zuordnung, Verbindungsaufbau) liegt in Modulen ohne Home-Assistant-Abhängigkeit und wird mit Unit-Tests geprüft:

```sh
python3 -m pytest tests -q
```

Ohne pytest laufen dieselben Tests mit `python3 -m unittest discover -s tests`.

Zusätzlich gibt es Tests gegen den echten Home-Assistant-Core (`tests_ha/`). Sie brauchen Python 3.14 und werden getrennt gestartet:

```sh
pip install -r tests_ha/requirements.txt
python3 -m pytest tests_ha
```

Fehler und Wünsche bitte als [Issue auf GitHub](https://github.com/roberteinhaus/hass-emotionkit/issues) melden.
