#!/usr/bin/env python3
"""Back up and restore the Pocket Rogues save that lives in the Windows registry.

    python save_manager.py backup [label]     export the key to Backups\\<timestamp>[_label].reg
    python save_manager.py list               show the backups, newest first
    python save_manager.py restore [what]     import a backup (default: latest; or a number
                                              from `list`, a label, or a path)
    python save_manager.py revive             undo the newest death: last floor layout, pre-death
                                              gold/XP/counters, and the gear worn at death
    python save_manager.py rewind CP [PROG]   load backup CP's floor and position, keeping PROG's
                                              gold, gear, XP and counters (PROG defaults to newest)
    python save_manager.py watch              poll the key, snapshot every change, and after a
                                              death + game exit restore the last pre-death snapshot
                                              (--no-revive turns the restore off)

Backups are regedit-format .reg files (the same thing `reg export` produces), so
prefs_convert.py can turn any of them into Android XML and back.

restore refuses to run while Pocket Rogues.exe is open, because Unity rewrites the
key when the game exits and would wipe the restore. Pass --force to override.
Before every restore the current key is saved as a "pre-restore" backup.
"""
import argparse
import codecs
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

REG_KEY = r"HKCU\Software\EtherGaming\Pocket Rogues"
REG_KEY_FULL = r"HKEY_CURRENT_USER\Software\EtherGaming\Pocket Rogues"   # as written inside .reg files
GAME_EXE = "Pocket Rogues.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "Backups")
POLL_SECONDS = 1
KEEP = 500          # plain "auto" snapshots beyond this count are deleted, oldest first
STALE_EXPORT_SECONDS = 120   # a .live-* temp export older than this was abandoned by a crash
KEEP_MARKED = 300   # labelled ones (save-exit, checkpoint, pre-*, revive, rewind, your own labels) get their own cap
EXIT_GRACE = 8      # seconds after the game exits before an auto-restore may run
REVIVE_WINDOW = 180 # only auto-restore if the game exits within this many seconds of a death
# Slot order of the character screen (curPoints<slot> matches the skill points shown on
# each class's diamond in the game).
CLASS_NAMES = ("Warrior", "Archer", "Wizard", "Hunter", "Berserker", "Necromancer")
DEATH_RE = re.compile(r'^"statsMain_char\d+_heroesDied_h\d+"=dword:([0-9a-f]{8})', re.M)

sys.path.insert(0, HERE)
import prefs_convert


class SaveError(Exception):
    """A problem the caller should show to the user (the CLI exits with it)."""


# ---------------------------------------------------------------- registry helpers

# Without this flag a windowless caller (pythonw, the desktop window) gets a console
# window flashed open for every reg / tasklist call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_tool(args):
    """Run a console tool quietly and return its CompletedProcess."""
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)


_export_lock = threading.Lock()


def export_key() -> bytes:
    """Return the current key as regedit .reg bytes (UTF-16 with BOM).

    Each call gets its own temp file, so the watcher and the window can export at
    the same moment without stepping on each other."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".live-", suffix=".reg", dir=BACKUP_DIR)
    os.close(fd)
    try:
        with _export_lock:                  # reg.exe fails now and then if two exports overlap
            for attempt in range(4):
                r = run_tool(["reg", "export", REG_KEY, tmp, "/y"])
                data = b""
                if r.returncode == 0:
                    with open(tmp, "rb") as fh:
                        data = fh.read()
                    # reg.exe has been seen returning success with an empty file when
                    # another process exported the same key in the same instant
                    if data.startswith(codecs.BOM_UTF16_LE) and "[".encode("utf-16-le") in data:
                        return data
                time.sleep(0.2 * (attempt + 1))   # another process (a CLI watch) may be exporting
            raise SaveError("reg export failed: " + (r.stderr.strip() or r.stdout.strip() or "empty export"))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def import_reg(path: str):
    r = run_tool(["reg", "import", path])
    if r.returncode != 0:
        raise SaveError(f"reg import failed: {r.stderr.strip() or r.stdout.strip()}")


def delete_key():
    r = run_tool(["reg", "delete", REG_KEY, "/f"])
    if r.returncode != 0 and "unable to find" not in (r.stderr + r.stdout).lower():
        raise SaveError(f"reg delete failed: {r.stderr.strip() or r.stdout.strip()}")


def game_running() -> bool:
    r = run_tool(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH"])
    return GAME_EXE.lower() in r.stdout.lower()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def count_values(data: bytes) -> int:
    return len(re.findall(r'^"', data.decode("utf-16", errors="replace"), re.M))


def deaths(data: bytes) -> int:
    """Total hero deaths across all characters, straight from the export text."""
    return sum(int(h, 16) for h in DEATH_RE.findall(data.decode("utf-16", errors="replace")))


def snapshot_info(path: str) -> dict:
    """What a snapshot says about the game: the fields that decide whether it can put the
    player back in a dungeon, plus the hero and lifetime stats worth showing."""
    return describe(prefs_convert.read_reg(path))


def describe(entries) -> dict:
    """Summarise PlayerPrefs entries (from read_reg) into one flat dict of plain values."""
    vals = {k: v for k, _, v in entries}
    hero = vals.get("curChar", 0)
    hero_name = CLASS_NAMES[hero] if isinstance(hero, int) and 0 <= hero < len(CLASS_NAMES) else f"hero {hero}"
    cur = f"statsCur_char{hero}_"      # this hero's current life
    main = f"statsMain_char{hero}_"    # this slot, all time

    def n(key, default=0):
        v = vals.get(key, default)
        return v if isinstance(v, (int, float)) else default

    areas = ("Tower", "Catacombs", "Crypt", "Prison", "Garden", "TombHill", "Borderlands", "Grotto")
    total_deaths = sum(n(f"statsMain_char{c}_heroesDied") for c in range(6))
    return {
        # resume logic (used by revive)
        "deaths": total_deaths,
        "scene": vals.get("sceneName", ""),
        "resumable": n("NeedLoading") == 1 or n("NeedLoadingAutosaveReserve") == 1 or n("NeedLoadingAutosave") == 1,
        "has_layout": bool(vals.get("dungData_data") or vals.get("reserve_dungData_data")),
        # a load point: the game will start straight into this floor at this position. Save and Exit
        # makes one; so do the manager's revive and rewind. Side-room entries set the flag too but
        # with loc pointing at the side room, so they are excluded.
        "load_point": n("NeedLoading") == 1 and bool(vals.get("dungData_data"))
                      and str(vals.get("sceneName", "")).startswith("floor_")
                      and vals.get("loc") == vals.get("sceneName"),
        "depth": n("curDeep"),
        # the hero being played right now
        "hero": hero,
        "hero_name": hero_name,
        "skill_points_by_class": {CLASS_NAMES[c]: n(f"curPoints{c}") for c in range(len(CLASS_NAMES))},
        "level_by_class": {CLASS_NAMES[c]: n(f"realLvl_{c}") for c in range(len(CLASS_NAMES))},
        "level": n(f"realLvl_{hero}"),
        "endless": n("Endless") == 1,
        "gold": n("money"),                       # the number on screen
        "gems": n("curGems"),
        "run_coins": n(cur + "coins"),             # coins collected this life (a counter, not a wallet)
        "run_kills": n(cur + "killedCreatures"),
        "run_floors": n(cur + "floorsCleared"),
        "run_minutes": n(cur + "timeInGame"),
        "run_best_floor": n(cur + "maxFloor"),
        "run_score": n(cur + "maxScore"),
        "skill_points": n(cur + "skillPoints"),
        # this slot, all time
        "guild_level": n("guildLvl"),
        "all_raids": n(main + "allRaids"),
        "raids_no_death": n(main + "allRaids_withoutDeath"),
        "all_deaths": n(main + "heroesDied"),
        "all_kills": n(main + "killedCreatures"),
        "all_coins": n(main + "coins"),
        "all_minutes": n(main + "timeInGame"),
        "all_floors": n(main + "floorsCleared"),
        "best_floor": n(main + "maxFloor"),
        "best_score": n(main + "maxScore"),
        "secrets": n(main + "foundedSecrets"),
        "quests": n(main + "quests"),
        "kills_by_tier": {t: n(main + f"killed_{t}") for t in ("normal", "upper", "elite", "champions", "bosses")},
        "deaths_by_cause": {c: n(main + f"heroesDied_{c}") for c in ("monster", "bosses", "traps", "effects", "leaved")},
        "best_floor_by_area": {a: n(main + f"maxFloor_{a}") for a in areas},
    }


def has_live_layout(path: str) -> bool:
    vals = {k: v for k, _, v in prefs_convert.read_reg(path)}
    return bool(vals.get("dungData_data"))


def equip_death_gear(entries, death_path: str):
    """Put the gear recorded at death (DeadCharacterData_<slot>) back on the hero record.

    The game writes the equipped items into the death record even though it never wrote
    them to the dungeon layout, so gear found after the last layout write survives this
    way. Whatever the hero record had in a replaced slot goes into the bag.
    Returns (entries, list of item names equipped)."""
    import json
    death = {k: v for k, _, v in prefs_convert.read_reg(death_path)}
    vals = {k: (t, v) for k, t, v in entries}
    hero_slot = vals.get("curChar", ("int", 0))[1]
    record = death.get(f"DeadCharacterData_{hero_slot}")
    if not record or not vals.get("dungData_data", ("", ""))[1]:
        return entries, []
    gear = [json.loads(e) if isinstance(e, str) else e for e in json.loads(record).get("equipment", [])]
    hero = json.loads(vals["dungData_data"][1])
    bag = json.loads(vals["dungData_invData"][1]) if vals.get("dungData_invData", ("", ""))[1] else {"Items": []}
    slot_of = {"Helmets": "head", "Armor": "body", "SecondWeapon": "shield", "Weapons": "weapon"}
    worn = []
    for item in gear:
        kind = item.get("path", "").split("/")[1:2]
        slot = slot_of.get(kind[0]) if kind else None
        if not slot:
            continue
        old = hero.get(slot)
        if old and old.get("path") and old.get("path") != item.get("path"):
            bag.setdefault("Items", []).append(old)
        hero[slot] = item
        worn.append(item["path"].split("/")[-1])
    compact = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False)  # noqa: E731
    vals["dungData_data"] = ("string", compact(hero))
    vals["dungData_invData"] = ("string", compact(bag))
    return [(k, vals[k][0], vals[k][1]) for k, _, _ in entries], worn


def build_revive(files=None):
    """Build the save that undoes the newest death in the backups.

    files: backup paths, oldest first (default: everything in Backups).
    progress   = the last backup before the death counter rose (gold, XP, counters);
    checkpoint = the last backup at or before it that holds a live dungeon layout
                 (falls back to the reserve copy inside `progress`);
    gear       = the death record in the first post-death backup.
    Returns (path, description)."""
    files = list(files) if files is not None else list(reversed(backups()))
    if len(files) < 2:
        raise SaveError("need at least two snapshots to find a death")
    counts = [deaths(open(f, "rb").read()) for f in files]
    death_at = None
    for i in range(1, len(files)):
        if counts[i] > counts[i - 1]:
            death_at = i
    if death_at is None:
        raise SaveError("no death found in the snapshots")
    death, progress = files[death_at], files[death_at - 1]
    checkpoint = next((f for f in reversed(files[:death_at]) if has_live_layout(f)), progress)
    entries = rewind_keep_progress(checkpoint, progress)
    entries, worn = equip_death_gear(entries, death)
    out = write_entries(entries, "revive")
    info = snapshot_info(out)
    why = (f"death after {os.path.basename(progress)}; floor from {os.path.basename(checkpoint)} "
           f"({info['scene']}, floor {info['depth']}), gold {info['gold']:,}, level {info['level']}"
           + (f", wearing {', '.join(worn)}" if worn else ""))
    return out, why


# ---------------------------------------------------------------- rewind, keep progress

# Keys that describe WHERE the hero is and what the floor looks like. They come from the
# checkpoint; everything else (stats, gold, gear, inventory) comes from the progress snapshot.
PLACE_KEYS = ("loc", "locationStringBckp", "helloFromGates", "prevLocation", "prevLoc_temp", "curDeep", "realDeep",
              "clearsList", "thisIsClear", "newDungeon", "FirstAwake", "dungData_objData", "dungData_mobsData",
              "savedMinimap")
# Fields inside dungData_data (the hero record) that belong to the place rather than the hero.
PLACE_FIELDS = ("position", "rotation", "deep", "killedMobs", "fullSaving")


def rewind_keep_progress(checkpoint: str, progress: str) -> list:
    """Entries for a save that puts the hero back at `checkpoint`'s place with `progress`'s progress.

    Verified against the game: it loads a dungeon from the dungData_* keys whenever
    NeedLoading is 1, placing the hero at dungData_data.position. So a save built from the
    newest stats plus an older floor layout loads at the older position with the newer gear,
    gold, XP and counters. Monsters alive in the older layout come back.
    """
    import json
    old = {k: (t, v) for k, t, v in prefs_convert.read_reg(checkpoint)}
    new_entries = prefs_convert.read_reg(progress)
    new = {k: (t, v) for k, t, v in new_entries}
    # The game clears the live layout (dungData_*) on every floor change and rewrites it a few
    # seconds later; between those moments only the reserve copy (reserve_dungData_*) exists.
    layout = "dungData_" if old.get("dungData_data", ("", ""))[1] else "reserve_dungData_"
    for key in ("data", "objData", "mobsData"):
        if not old.get(layout + key, ("", ""))[1]:
            raise SaveError(f"{os.path.basename(checkpoint)} holds no dungeon layout; pick a snapshot taken inside a dungeon")
    for key in ("objData", "mobsData", "invData"):
        old["dungData_" + key] = old[layout + key]
    old["dungData_data"] = old[layout + "data"]
    hero_old = json.loads(old["dungData_data"][1])
    hero_new = json.loads(new["dungData_data"][1]) if new.get("dungData_data", ("", ""))[1] else dict(hero_old)
    for f in PLACE_FIELDS:
        if f in hero_old:
            hero_new[f] = hero_old[f]
    if not isinstance(hero_new.get("hp_cur"), (int, float)) or hero_new.get("hp_cur", 0) <= 0:
        hero_new["hp_cur"] = hero_old.get("hp_cur", 1)   # never load a dead hero
    merged = dict(new)
    for k in PLACE_KEYS:
        if k in old:
            merged[k] = old[k]
    if not merged.get("dungData_invData", ("", ""))[1]:      # progress taken after the game cleared its layout
        merged["dungData_invData"] = old["dungData_invData"]
    merged["dungData_data"] = ("string", json.dumps(hero_new, separators=(",", ":"), ensure_ascii=False))
    merged["NeedLoading"] = ("int", 1)
    merged["NeedLoadingAutosave"] = ("int", 1)
    merged["sceneName"] = old.get("sceneName", merged.get("sceneName", ("string", "")))
    # keep the progress file's order, then anything only the checkpoint had
    order = [k for k, _, _ in new_entries] + [k for k in merged if k not in new]
    return [(k, merged[k][0], merged[k][1]) for k in order]


def write_entries(entries, label: str) -> str:
    """Write PlayerPrefs entries as a labelled backup, named like every other backup."""
    fd, tmp = tempfile.mkstemp(prefix=".build-", suffix=".reg", dir=BACKUP_DIR if os.path.isdir(BACKUP_DIR) else None)
    os.close(fd)
    try:
        prefs_convert.write_reg(tmp, entries)
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return write_backup(data, label)


def write_rewind(checkpoint: str, progress: str, label: str = "rewind") -> str:
    """Build the rewind save as a labelled backup file and return its path."""
    return write_entries(rewind_keep_progress(checkpoint, progress), label)


# ---------------------------------------------------------------- backups

def backups():
    """Backup files, newest first."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = [f for f in os.listdir(BACKUP_DIR) if f.lower().endswith(".reg") and not f.startswith(".")]
    for stale in os.listdir(BACKUP_DIR):        # temp exports left by a crash are never backups
        if stale.startswith(".live-"):
            full = os.path.join(BACKUP_DIR, stale)
            try:
                if time.time() - os.path.getmtime(full) > STALE_EXPORT_SECONDS:   # an export in progress is younger
                    os.remove(full)
            except OSError:
                pass
    files.sort(reverse=True)
    return [os.path.join(BACKUP_DIR, f) for f in files]


_name_lock = threading.Lock()
_last_stamp_time = None


def write_backup(data: bytes, label: str = "") -> str:
    """Write a backup named by its time to the millisecond, e.g. 2026-01-02_12-08-40-347_auto.reg.

    Names sort in creation order, so `latest` and pruning see the right file. The file is
    created exclusively; if two writers land on the same millisecond, the loser moves on
    to the next one instead of overwriting."""
    global _last_stamp_time
    os.makedirs(BACKUP_DIR, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with _name_lock:
        now = datetime.now()
        if _last_stamp_time is not None and now <= _last_stamp_time:
            now = _last_stamp_time + timedelta(milliseconds=1)   # never reuse a stamp, whatever the label
        taken = {f[:23] for f in os.listdir(BACKUP_DIR) if STAMP_RE.match(f)}   # stamps other processes used
        for _ in range(10000):
            stamp = f"{now:%Y-%m-%d_%H-%M-%S}-{now.microsecond // 1000:03d}"
            if stamp in taken:
                now += timedelta(milliseconds=1)
                continue
            path = os.path.join(BACKUP_DIR, f"{stamp}_{label}.reg" if label else f"{stamp}.reg")
            try:
                fd = os.open(path, flags)
            except FileExistsError:
                now += timedelta(milliseconds=1)
                continue
            _last_stamp_time = now
            break
        else:
            raise SaveError("could not find a free backup file name")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    prune()
    return path


STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:-\d{3})?(?:_(.*))?\.reg$", re.I)


def backup_label(path: str) -> str:
    """The label part of a backup file name ('' for a plain or unlabelled one)."""
    m = STAMP_RE.match(os.path.basename(path))
    return (m.group(3) or "") if m else os.path.basename(path)[:-4]


def prune():
    files = backups()
    plain = [f for f in files if os.path.basename(f).endswith("_auto.reg") or not backup_label(f)]
    marked = [f for f in files if f not in plain]
    for old in plain[KEEP:] + marked[KEEP_MARKED:]:
        os.remove(old)


def snapshot_label(data: bytes, last_layout: str = None):
    """What to call a watcher snapshot: 'load-point', 'checkpoint' (a new dungeon layout), or 'auto'.
    Returns (label, layout digest or None)."""
    vals = {k: v for k, _, v in prefs_convert.read_reg_bytes(data)}
    layout = vals.get("dungData_data") or ""
    layout_id = hashlib.sha256((layout + (vals.get("dungData_objData") or "")).encode("utf-8")).hexdigest() if layout else None
    if (vals.get("NeedLoading") == 1 and layout and str(vals.get("sceneName", "")).startswith("floor_")
            and vals.get("loc") == vals.get("sceneName")):
        return "load-point", layout_id
    if layout_id and layout_id != last_layout:
        return "checkpoint", layout_id
    return "auto", layout_id


def latest_digest():
    files = backups()
    return digest(open(files[0], "rb").read()) if files else None


def pick_backup(what: str) -> str:
    files = backups()
    if not files:
        raise SaveError("no backups yet; run `backup` first")
    if not what or what == "latest":
        return files[0]
    if what.isdigit():
        idx = int(what)
        if 1 <= idx <= len(files):
            return files[idx - 1]
        raise SaveError(f"there are {len(files)} backups; {idx} is out of range")
    if os.path.isfile(what):
        return what
    hits = [f for f in files if what.lower() in os.path.basename(f).lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SaveError(f"no backup matches {what!r}")
    raise SaveError(f"{what!r} matches {len(hits)} backups; be more specific:\n  " + "\n  ".join(os.path.basename(h) for h in hits))


# ---------------------------------------------------------------- commands

def cmd_backup(label: str = "", quiet: bool = False, log=print) -> str:
    data = export_key()
    path = write_backup(data, label)
    if not quiet:
        log(f"saved {count_values(data)} values -> {path}")
    return path


def cmd_list():
    files = backups()
    if not files:
        print("no backups yet")
        return
    for i, f in enumerate(files, 1):
        size = os.path.getsize(f)
        print(f"{i:4d}  {os.path.basename(f):45s} {size // 1024:5d} KB")
    print(f"{len(files)} backups in {BACKUP_DIR}")


def cmd_restore(what: str, force: bool, log=print):
    path = pick_backup(what)
    if game_running() and not force:
        raise SaveError(f"{GAME_EXE} is running. Close the game first, or use --force (the game will overwrite the restore on exit).")
    data = open(path, "rb").read()
    check_backup(data, os.path.basename(path))
    safety = cmd_backup("pre-restore", quiet=True)
    ok = restore_bytes(data)
    log(f"restored {count_values(data)} values from {os.path.basename(path)}")
    log(f"previous state kept as {os.path.basename(safety)}")
    log("verified: registry now matches the backup value for value" if ok
          else "WARNING: registry differs from the backup after import; check the file")


SECTION_RE = re.compile(r"^\[(-?)([^\]]*)\]\s*$", re.M)


def check_backup(data: bytes, name: str):
    """Refuse anything that would touch a registry key other than the game's."""
    text = data.decode("utf-16", errors="replace")
    sections = SECTION_RE.findall(text)
    if not sections:
        raise SaveError(f"{name} holds no registry key; refusing to import it")
    for minus, key in sections:
        if minus or key.strip().lower() != REG_KEY_FULL.lower():
            raise SaveError(f"{name} touches another registry key ({minus}{key.strip()}); refusing to import it")


def same_values(a: bytes, b: bytes) -> bool:
    """True when two .reg exports carry the same names, types and values (layout may differ)."""
    return sorted(prefs_convert.read_reg_bytes(a)) == sorted(prefs_convert.read_reg_bytes(b))


def restore_file(path: str) -> bool:
    """Import a backup over the live key; returns True if the registry then matches it."""
    data = open(path, "rb").read()
    check_backup(data, os.path.basename(path))
    return restore_bytes(data)


def restore_bytes(data: bytes) -> bool:
    """Import .reg content held in memory, via a temp file that pruning never sees."""
    fd, tmp = tempfile.mkstemp(prefix=".restore-", suffix=".reg", dir=BACKUP_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        delete_key()
        import_reg(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return same_values(export_key(), data)


def cmd_revive(force: bool, log=print):
    """Find the newest death in the snapshots and restore the best point before it."""
    if game_running() and not force:
        raise SaveError(f"{GAME_EXE} is running. Close the game first, or use --force.")
    safe, why = build_revive()
    log(why)
    keep = cmd_backup("pre-revive", quiet=True)
    ok = restore_file(safe)
    log(f"loaded {os.path.basename(safe)}; previous state kept as {os.path.basename(keep)}")
    log("verified: registry matches the snapshot value for value" if ok
          else "WARNING: registry does not match the snapshot; check it")


def cmd_rewind(checkpoint: str, progress: str = "latest", force: bool = False, log=print):
    """Load `checkpoint`'s floor with `progress`'s gold, gear, XP and counters (default: newest backup)."""
    if game_running() and not force:
        raise SaveError(f"{GAME_EXE} is running. Close the game first, or use --force.")
    cp = pick_backup(checkpoint)
    pr = pick_backup(progress)
    built = write_rewind(cp, pr)
    info = snapshot_info(built)
    log(f"built {os.path.basename(built)}: {info['scene']} floor {info['depth']} from {os.path.basename(cp)}, "
        f"gold {info['gold']:,} and level {info['level']} from {os.path.basename(pr)}")
    keep = cmd_backup("pre-rewind", quiet=True)
    ok = restore_file(built)
    log(f"loaded it; previous state kept as {os.path.basename(keep)}")
    log("verified: registry matches the rewind save value for value" if ok
        else "WARNING: registry does not match the rewind save; check it")


def cmd_watch(revive: bool = True, log=print, stop=None):
    """Poll until Ctrl+C or, when given, until `stop` (a threading.Event) is set."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    log(f"watching {REG_KEY}")
    log(f"snapshots go to {BACKUP_DIR}")
    log("auto-revive is " + ("ON: after a death, the last pre-death snapshot is restored when the game exits"
                               if revive else "off"))
    files = backups()
    last_path = files[0] if files else None
    last_saved = digest(open(last_path, "rb").read()) if last_path else None
    last_deaths = deaths(open(last_path, "rb").read()) if last_path else None
    last_layout = snapshot_label(open(last_path, "rb").read())[1] if last_path else None
    safe_path = None        # newest snapshot taken before the most recent death
    death_time = None
    exit_time = None
    was_running = game_running()
    log(f"game is {'running' if was_running else 'not running'}")
    try:
        while stop is None or not stop.is_set():
            running = game_running()
            if running != was_running:
                log(f"{datetime.now():%H:%M:%S}  game {'started' if running else 'exited'}")
                was_running = running
                if running:
                    safe_path = death_time = exit_time = None
                else:
                    exit_time = time.time()
            data = export_key()
            d = digest(data)
            if d != last_saved and count_values(data) > 0:
                label, last_layout = snapshot_label(data, last_layout)
                path = write_backup(data, label)
                now_deaths = deaths(data)
                if last_deaths is not None and now_deaths > last_deaths and last_path:
                    safe_path = path        # the post-death snapshot; the revive is built at exit time
                    death_time = time.time()
                    log(f"{datetime.now():%H:%M:%S}  DEATH detected (deaths {last_deaths} -> {now_deaths}); "
                          f"will revive from the last layout when the game exits")
                last_saved, last_path, last_deaths = d, path, now_deaths
                log(f"{datetime.now():%H:%M:%S}  change detected, saved {os.path.basename(path)} ({count_values(data)} values)")

            if (revive and exit_time is not None and not running
                    and time.time() - exit_time >= EXIT_GRACE):
                if safe_path and exit_time - death_time <= REVIVE_WINDOW:
                    if game_running():
                        time.sleep(POLL_SECONDS)
                        continue
                    keep = write_backup(data, "pre-revive")
                    try:
                        safe_path, why = build_revive()
                    except SaveError as exc:
                        log(f"{datetime.now():%H:%M:%S}  could not build a revive: {exc}")
                        safe_path = death_time = exit_time = None
                        continue
                    ok = restore_file(safe_path)
                    log(f"{datetime.now():%H:%M:%S}  REVIVED: {why}; post-death state kept as {os.path.basename(keep)}")
                    log("           " + ("verified: registry matches the snapshot value for value" if ok
                                          else "WARNING: registry does not match the snapshot; check it"))
                    last_path = safe_path
                    live = open(safe_path, "rb").read()
                    last_saved, last_deaths = digest(live), deaths(live)
                elif safe_path:
                    log(f"{datetime.now():%H:%M:%S}  death was {int(exit_time - death_time)}s before exit, "
                          f"longer than {REVIVE_WINDOW}s: not restoring. Safe point: {os.path.basename(safe_path)}")
                safe_path = death_time = exit_time = None
            if stop is not None:
                stop.wait(POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    log("stopped watching")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="export the key now")
    b.add_argument("label", nargs="?", default="")
    sub.add_parser("list", help="show backups")
    r = sub.add_parser("restore", help="import a backup")
    r.add_argument("what", nargs="?", default="latest")
    r.add_argument("--force", action="store_true", help="restore even if the game is running")
    v = sub.add_parser("revive", help="restore the best point before the newest death in the snapshots")
    v.add_argument("--force", action="store_true")
    rw = sub.add_parser("rewind", help="load a checkpoint's floor but keep a newer backup's progress")
    rw.add_argument("checkpoint", help="backup to take the floor and position from (number, label or path)")
    rw.add_argument("progress", nargs="?", default="latest", help="backup to take gold, gear and stats from (default: newest)")
    rw.add_argument("--force", action="store_true")
    w = sub.add_parser("watch", help="snapshot every change and auto-revive after a death")
    w.add_argument("--no-revive", action="store_true", help="only snapshot, never restore")
    ns = p.parse_args(argv)
    try:
        run(ns)
    except SaveError as exc:
        sys.exit(str(exc))


def run(ns):
    if ns.cmd == "backup":
        cmd_backup(ns.label)
    elif ns.cmd == "list":
        cmd_list()
    elif ns.cmd == "restore":
        cmd_restore(ns.what, ns.force)
    elif ns.cmd == "revive":
        cmd_revive(ns.force)
    elif ns.cmd == "rewind":
        cmd_rewind(ns.checkpoint, ns.progress, ns.force)
    else:
        cmd_watch(revive=not ns.no_revive)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("this script talks to the Windows registry; run it on Windows")
    main()
