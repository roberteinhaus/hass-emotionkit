# EmotionKit — Home Assistant Integration

Bringe CS2-Spielereignisse direkt in dein Smart Home. Die EmotionKit-Integration verbindet Home Assistant mit [emotionkit.de](https://emotionkit.de) und stellt Echtzeit-Game-Events als Automations-Trigger bereit.

## Features

- **Einfache Einrichtung** — Claim-Code eingeben, fertig. Kein Token, kein YAML.
- **Native Automationen** — Alle CS2-Ereignisse als Device Triggers im Automation-Editor.
- **Volle Flexibilität** — Steuere jede beliebige Home Assistant Entität mit HA-Bordmitteln.
- **Kein separater Service** — Läuft direkt in Home Assistant, kein Docker-Container nötig.

## Verfügbare Trigger

| Trigger | Beschreibung |
|---------|-------------|
| `bomb_planted` | Bombe wurde gelegt |
| `bomb_defused` | Bombe wurde entschärft |
| `bomb_exploded` | Bombe ist explodiert |
| `round_live` | Runde gestartet (Freezetime vorbei) |
| `round_over_t` | Terroristen haben gewonnen |
| `round_over_ct` | Counter-Terrorists haben gewonnen |
| `freezetime` | Freezetime hat begonnen |

## Installation

### Über HACS (empfohlen)

1. Öffne HACS in Home Assistant
2. Klicke auf **Integrationen** → **⋮** → **Benutzerdefinierte Repositories**
3. Füge die Repository-URL hinzu und wähle Kategorie **Integration**
4. Installiere **EmotionKit**
5. Starte Home Assistant neu

### Manuell

1. Kopiere den Ordner `custom_components/emotionkit` in dein Home Assistant `config/custom_components/` Verzeichnis
2. Starte Home Assistant neu

## Einrichtung

1. Gehe zu **Einstellungen** → **Geräte & Dienste** → **Integration hinzufügen**
2. Suche nach **EmotionKit**
3. Gib einen Gerätenamen ein
4. Ein **Claim-Code** wird angezeigt (z.B. `ABCD-5HKL`)
5. Öffne die EmotionKit-Weboberfläche unter https://emotionkit.de und gib den Code dort ein
6. Die Integration verbindet sich automatisch

## Automation erstellen

Nach der Einrichtung kannst du Automationen direkt im HA-Editor erstellen:

1. Gehe zu **Einstellungen** → **Automatisierungen** → **Neue Automatisierung**
2. **Auslöser**: Gerät → EmotionKit → z.B. "Bomb planted"
3. **Aktion**: Beliebige HA-Aktion (Licht, Szene, Skript, …)

### Beispiel: Bombe → Licht rot

```yaml
trigger:
  - platform: device
    device_id: <dein_emotionkit_gerät>
    domain: emotionkit
    type: bomb_planted
action:
  - service: light.turn_on
    target:
      entity_id: light.wohnzimmer
    data:
      color_name: red
      brightness: 255
```

## Entwicklung

Die Integration ist Teil des [EmotionKit Cloud](https://gitlab.com/emotionkit/emotionkit-cloud) Monorepos und befindet sich unter `integrations/homeassistant/`.
