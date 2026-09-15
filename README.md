# Pocket Rogues Save Manager

Back up, restore, revive and rewind your **Pocket Rogues: Ultimate** save on Windows. A small desktop window plus a command line tool. Download the exe and run it, nothing to install.

The Windows version keeps its save in the registry as Unity PlayerPrefs. This tool snapshots that key every time it changes, shows what each snapshot holds (hero, floor, gold, gear, lifetime stats), and can put any of them back, including a few tricks the game does not offer itself.

## What it does

- **Backup Now**: one snapshot, with an optional label.
- **Start Watching**: snapshots every change while you play. If a hero dies and you quit within three minutes, the last safe state is restored automatically.
- **Revive**: undo the newest death. Loads the last dungeon layout the game saved, keeps gold, XP and counters from one second before the death, and re-equips the gear you were wearing when you died.
- **Rewind here, keep progress**: load an older backup's floor and position but keep gold, gear, XP and counters from the newest backup. Useful when a side room turned out to be a mistake.
- **Restore**: put any backup back exactly as it was.
- **Details panel**: class, level, floor, gold and gems, kills, deaths by cause, deepest floor per area, play time, unspent skill points, and the change of each number against the live save.
- **Timeline tab**: dungeon floor and gold across your backups, with death markers.
- **Convert tab**: Android `shared_prefs` XML to Windows `.reg` and back, and the game's `PRogues.prs` local save to editable JSON and back.

## Requirements

- Windows 10 or 11
- Pocket Rogues: Ultimate (Steam)
- Python 3.10 or newer only if you run from source, from [python.org](https://www.python.org/downloads/windows/) (tick "Add python to PATH")

## Running it

**Easiest:** download `Pocket-Rogues-Save-Manager-<version>.zip` from the [releases page](https://github.com/RobThePCGuy/Pocket-Rogues-Save-Manager/releases), unzip it anywhere, and run **Pocket Rogues Save Manager.exe**. No Python needed. Backups go into a `Backups` folder next to the exe.

**From source:** clone this repository and double-click **Pocket Rogues Saves.cmd** (needs Python). Backups go next to the scripts.

To build the exe yourself: `pip install pyinstaller` then run `build.cmd`; the result lands in `dist\`.

Command line, from the same folder:

```
python save_manager.py backup [label]
python save_manager.py list
python save_manager.py restore [number | label | path]
python save_manager.py revive
python save_manager.py rewind <checkpoint> [progress]
python save_manager.py watch [--no-revive]
python prefs_convert.py in.xml out.reg [--base existing.reg]
python prefs_convert.py in.prs out.json
```

Restore, revive and rewind refuse to run while the game is open, because the game rewrites its save when it exits.

## How the game saves, and what that means for you

Learned by watching the registry while playing. It shapes what any backup can and cannot give back.

- **Gold, gems, kills, XP and skill points** are written within a second of every change. They are always in the newest backup.
- **Your position, the floor layout, the monsters alive and your bag** are written only when the game saves the dungeon: on entering or leaving a floor or side room, and when you use **Save and Exit** in the pause menu. Items found since the last of those are in no backup, because they never reached the disk.
- **Gear you are wearing** is also written into the death record at the death screen, which is how Revive can hand it back even when it never made it into a layout save.
- **Save and Exit** is the on-demand save. Use it after finding anything you care about. Those backups show in blue as load points: the game starts straight back into that floor at that spot.
- Setting the game's `NeedLoading` flag on a backup that holds a dungeon layout makes the game load straight into it. Revive and Rewind rely on this.
- A **mercenary** lives in a third set of keys that survive floor changes, so hires ride along with every restore.

## Files

| File | Purpose |
|---|---|
| `save_manager_ui.py` | The desktop window |
| `save_manager.py` | Backup, restore, revive, rewind, watch. Also the command line tool |
| `prefs_convert.py` | PlayerPrefs format conversion (XML, .reg, .prs, JSON) |
| `Pocket Rogues Saves.cmd` | Launcher that opens the window without a console (source only) |
| `build.cmd` | Builds the single-file exe with PyInstaller |
| `assets/` | Icon and artwork |

## Safety

Every restore, revive and rewind first saves the current state as a "before" backup, then verifies the registry against the file value by value. Nothing is deleted without a confirmation dialog. The only registry key touched is `HKCU\Software\EtherGaming\Pocket Rogues`.

## License

MIT. Not affiliated with EtherGaming.
