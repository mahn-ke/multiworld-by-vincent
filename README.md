# MultiWorldGG portable room

## Start a new multiworld

1. Install **Python 3.11.13+, 3.12, or 3.13** (not 3.14). macOS/Linux, Bash,
   Python's `venv` and `pip` are required.
2. Put all player `.yaml` / `.yml` files directly beside `start.sh`.
   Pathogenic and Peglin examples are included. Remove either to omit that player.
3. Run:

   ```sh
   ./start.sh
   ```

Every invocation generates a **new seed with fresh checks and item history** from
all adjacent YAMLs. It does **not** resume an old campaign. Reconnect the game
clients and start new runs for the new seed. Player names and counts follow the
YAMLs, including any `name`, multiple-document or `game` options supported by the
bundled generator.

Craig's Pathogenic configuration enables `start_with_all_characters`. With the
updated Pathogenic client, the campaign starts with the full roster unlocked.
This ignores `remote_characters` and omits character items, their randomized
shipments and their optional Earn checks. Other categories remain unchanged.
The bundled APWorld supports the option; generate a new seed to apply it.
Changing these files does not change an already running room.

- Website / tracker: <http://localhost:8085/>
- Game clients on this computer: `127.0.0.1` (port 38281; Pathogenic uses it by default)
- Other computers: use the LAN address printed at startup.
- Stop both servers with **Ctrl+C**. An occupied port produces an error; no
  existing server is stopped automatically.

Override ports or Python when needed:

```sh
./start.sh --web-port 8086 --ap-port 38286
PYTHON=python3.13 ./start.sh
./start.sh --generate-only
```

### Edit mode (new seed without restarting)

On localhost, the website has an **Edit players & settings** button. There you
can rename players, pick each player's game from the installed games, change
that game's settings (hover over a setting for its description), and add or
remove players. **Save & start new seed…** warns that this resets progress,
randomizes items and requires players to reconnect. After you confirm:

1. The new seed generates while the current game server keeps running.
   Progress and the generation log stream live over a WebSocket (`/ws`).
   If generation fails, nothing changes and the error is shown.
2. The YAMLs beside `start.sh` and `room-settings.json` are updated. Edited
   YAMLs are rewritten without their comments. Removed players are moved to
   `inactive-players/`.
3. The old game server stops and the new one starts on the same port. Open
   pages reload to the new seed automatically.

YAMLs with several documents or weighted names/games can only be kept or
removed in the editor. Edit those files by hand.

### Checks for other players (on by default)

After the normal fill, items are swapped between worlds so that a check unlocks
something for **another** player whenever possible. Every game rule is
respected and the seed stays beatable. Locked locations and players with many
more locations than everyone else keep some of their own items. Turn this off
with the editor's checkbox, or set `"prefer_other_players": false` in
`room-settings.json` beside `start.sh`.

The host's console and check buttons are only available through localhost.
The website, spoiler and tracker are visible to anyone who can reach its port.
Only host on a trusted network.

## Moving or sharing

Copy **`start.sh` + `dependencies/` + your YAMLs** together to any folder,
including paths with spaces. You can instead copy this whole directory.
There are no links to the original workspace, no bundled credentials, and no
dependencies on its old runtime or virtual environment.

The first launch installs Python packages from PyPI and needs internet.
The generator source (Archipelago 0.6.8, matching the existing working rooms),
tracker, and Pathogenic/Peglin custom worlds are bundled in `dependencies/`.
The source archive retains its upstream license. Game mods are not installed by
this launcher; install the matching game clients separately.

`.cache/` contains generated local runtime and Python environments. It need not
be copied. Moving the directory or switching Python rebuilds the environment
automatically instead of reusing broken absolute virtual-environment paths.
Internet is needed again for that setup.

`runs/` retains each seed, exact input YAML snapshots, generation log and server
save. `latest.json` identifies the last generated seed with a relative path.
Neither is required to start a new room. Old progress is retained, never reused
by `start.sh`.

## More games

Add more YAMLs beside `start.sh`; changing a YAML or adding/removing players is
picked up on the next invocation. For additional custom games, put their
`.apworld` files in `dependencies/worlds/` (or beside `start.sh`). Do not install
two files with the same name in both places. Custom worlds are executable Python;
use trusted packages compatible with Archipelago 0.6.8.

The included Python requirements cover headless hosting for Pathogenic and
Peglin. Other games may require extra Python packages, ROMs or game-specific
setup. Add their pip requirements to `dependencies/requirements-extra.txt`;
the launcher detects this and prepares a new environment. Generation errors are
reported with the full log path; a failed generation never hosts an older seed.

## Tests

```sh
python3.11 -m unittest discover -s dependencies -p 'test_*.py'
```

The HTTP tracker tests also require `websockets` (installed by the first launch).
