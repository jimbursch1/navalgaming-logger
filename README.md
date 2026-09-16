# navalgaming-logger

Records your Letters of Marque battles and voyages while you play, and uploads them to your
Naval Gaming account at [lom.navalgaming.com](https://lom.navalgaming.com/).

- **Battles:** the scoreboard as the Tab screen shows it (Name, Ship, Kills, Assists, Damage,
  Status, and hull, side and sail condition). It also records every ship's position and
  heading once a second, and every broadside fired. On the site, each battle gets a page with
  a chart of the fight.
- **Voyages:** where your ship went, port to port, drawn on the site's map.

It runs in the background from the moment you log on to Windows, waits for the game, and
uploads each voyage when you reach port.

## Read this first: what it does, and the risk

The logger reads the running game's memory, **read-only**, with Windows' `ReadProcessMemory`.
It never writes to the game, never sends anything to the game's servers, and never reads the
game's server connection. That process also holds your login session. The logger never reads
that part, and it writes only the fields listed above.

**Letters of Marque's developers have not endorsed this tool.** Reading another program's
memory is also what cheat tools do, so an anti-cheat system could treat the logger as one.
**You run it at your own risk.**

## What gets published

- **Battle pages are public.** Anyone can see them, and they include **the other captains
  in your fights**: their names, ships, tracks and broadsides.
- **Your voyages and your track on the map are private** to your account, not public pages.

Uploads go only to the account you connect in step 4 below. You can revoke a machine on your
dossier at any time, and uploads from it stop.

## Install

You need Windows, a Naval Gaming account at lom.navalgaming.com, and Python 3.

1. **Install Python 3** from [python.org](https://www.python.org/downloads/windows/). On the
   installer's first screen, tick **"Add python.exe to PATH"**.
2. **Download the logger:** take the zip from the
   [latest release](https://github.com/jimbursch1/navalgaming-logger/releases/latest), and
   extract it to any folder you like. The logger keeps its records in that folder.
3. **Double-click `install.cmd`.** It checks Python, registers a scheduled task called
   "NavalGaming Logger" that starts the logger at logon, and starts it now. It is safe to run
   again.
4. **Connect it to your account.** The first time, the installer copies a 64-character code
   to your clipboard and opens the site. Sign in, go to the **Logger** card on your dossier,
   paste the code and click **Register**.

The code is the fingerprint of a secret that stays on your machine, in
`%APPDATA%\navalgaming-logger\upload.json`. Only the fingerprint is ever sent to the site.
Don't share `upload.json`: whoever has it can upload to your account.

## Check it works

- **The Logger card on your dossier** shows whether your logger is running and what version
  it is. It checks in every 5 minutes.
- **`navalgaming-logger.log`**, in the logger's folder, says what it is doing.
- **With the game running,** `py navalgaming_logger.py --once` in that folder prints the
  current battle and your position, and then exits.

## Updates

**When the game updates, the logger stops finding the game until a new release of the logger
comes out.** The memory layout it reads changes with each game build. You'll see this on
the Logger card, and in the log.

Your dossier's Logger card also tells you when a newer release is out. **To update:**
1. Download the new release's zip.
2. Extract it over your folder.
3. Double-click `install.cmd` again.

Your records and your account connection are kept.

## Uninstall

1. In PowerShell, run `Unregister-ScheduledTask 'NavalGaming Logger'`.
2. Delete the logger's folder and `%APPDATA%\navalgaming-logger`.
3. On your dossier's Logger card, click **Revoke** for the machine.

## Files

| File | What it is |
|---|---|
| `install.cmd`, `install.ps1` | The installer. |
| `start.ps1` | Restarts the logger. |
| `navalgaming_logger.py` | The logger. |
| `dumpbattle.py` | Reads a battle's ship records; the logger uses it. |
| `upload.py` | Uploads to the site, and makes the account code (`--new-token`). |
| `test_*.py` | Tests: `python test_upload.py`, `python test_navalgaming_logger.py`. |

The logger writes `battles/`, `track/`, `upload-state.json` and `navalgaming-logger.log`
beside itself.

## License

MIT. See [LICENSE](LICENSE).
