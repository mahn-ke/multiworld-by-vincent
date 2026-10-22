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

After the normal fill, a best-effort pass swaps items between worlds to spread
rewards across **other players**, regardless of whether they play the same game.
It favors underserved finder/recipient pairs over concentrating rewards in a few
pairs, then minimizes self-items when the spread is equal. This can leave more
self-items than the absolute minimum, so smaller worlds still reward each other
instead of exclusively rewarding the largest world. Every game rule is respected
and the seed stays beatable. Unequal world sizes, locked locations, item restrictions
and progression rules can limit the spread. Turn this off
with the editor's checkbox, or set `"prefer_other_players": false` in
`room-settings.json` beside `start.sh`. Unchecked uses normal Archipelago
randomized placement without the extra cross-player pass (game rules still
apply). Changes apply to the next generated seed, not live checks.

The **Players** section's **Who finds what for whom** table counts all checks
in the seed, not just progression required by a goal. **Path to the goal** is
hidden when all goals are already satisfied without items, as with the
no-logic Pathogenic and Peglin worlds. Actual unreachable goals remain visible.

The host's console and check buttons are only available through localhost.
Activity uses the full width when the host console is hidden. The header's
**Downloads: Spoiler Log** link downloads the seed's spoiler text and appears
only on localhost, right-aligned before the editor button.
The header fades from its normal background at the screen's midpoint to dark
green at the right edge when the game server is online, or dark red when
it is offline or a status request fails. Non-local visitors see right-aligned connection
guidance using the hostname (or IP) in their browser URL and the game server's
port, falling back to the server's LAN IP if no browser hostname is available.
While the game server is offline, visitors' header expands to fill the screen,
vertically centers its contents and hides the rest of the page; it collapses
again once the server is back.
Start/stop progress notices are shown only on localhost.
On localhost, **Stop server** saves progress and stops only the game server;
the website remains available. **Start server** resumes the same seed and save.
Players must reconnect. To reset progress instead, use the editor's
**Save & start new seed…** workflow. Server controls are disabled while another
start, stop or generation operation is in progress.

### Players who have left (localhost only)

Each player's card has a **Player has left** checkbox on localhost. While it is
checked, the room unlocks one of that player's remaining checks **whose item goes
to another player** every 60 seconds (checks that would reward the departed
player are never unlocked). Progression items earliest on the path to the goal
go first. The first unlock happens one interval after checking the box. A bar
above the checkbox (hidden while unchecked) drains until the next unlock and
refills after each one. The card also shows how many such checks remain. After
the last one is unlocked, the bar hides again and the checkbox stays checked and
locked. Unlocks pause while the game server or that player's tracker is offline. Unchecking stops them.
The checkbox survives **Stop server**/**Start server**, but it is cleared for a
new seed or a restart of `start.sh`.

The interval can be changed in the editor (**Unlock interval for players who
have left**, 1–3600 seconds). It is saved to `room-settings.json`
(`"left_player_interval"`) and applies immediately; no new seed is needed.
The website, spoiler and tracker are visible to anyone who can reach its port.
Only host on a trusted network.

### Remote admins via OpenID Connect (optional)

Everything marked "localhost only" above stays available on localhost without
logging in. To also give remote admins these controls, put an `oidc.json`
beside `start.sh` (it holds a secret and is git-ignored):

```json
{
  "issuer": "https://sso.example.com/realms/my-realm",
  "client_id": "multiworldgg",
  "client_secret": "..."
}
```

Remote visitors then see a **Log in** button. After logging in, users whom
Keycloak considers admins (the `realm-management` client role `realm-admin`, or a
realm role named `admin`) get the full host page. Everyone else sees the normal
visitor page, without the login button. Override the roles with
`"admin_roles": ["..."]` (realm roles) and
`"admin_client_roles": {"client-id": ["..."]}`. Set `"public_url"` when the room is
behind a reverse proxy (e.g. `"https://room.example.com"`); otherwise the
browser's host is used.

In Keycloak, create a confidential OpenID Connect client (standard flow, PKCE
S256) with the redirect URI `<room URL>/auth/callback` and the post-logout
redirect URI `<room URL>/`. Sessions are kept in memory, so a room restart signs
everyone out; roles are re-checked whenever the access token is refreshed.

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
node --test dependencies/test_site.js
```

The HTTP tracker tests also require `websockets` (installed by the first launch).
