#!/usr/bin/env python3
"""Convert Pocket Rogues save data between its three on-disk shapes.

    Android shared_prefs XML  <->  Windows registry .reg   (Unity PlayerPrefs)
    PRogues.prs               <->  editable .json          (the game's "Save (Local)" file)

Usage:
    python prefs_convert.py xml2reg   in.xml   out.reg   [--base existing.reg]
    python prefs_convert.py reg2xml   in.reg   out.xml   [--base existing.xml]
    python prefs_convert.py prs2json  in.prs   out.json
    python prefs_convert.py json2prs  in.json  out.prs
    python prefs_convert.py in.xml out.reg               (mode picked from the input extension)

--base keeps every entry of an existing file that the input does not mention, so
Windows-only keys (Screenmanager, Rewired bindings, ...) survive an xml2reg run.
Base entries come first, in their original order; input entries follow.

PlayerPrefs facts this script encodes (verified against real files):
  * Windows value name = key + "_h" + DJB2-xor hash of the key's UTF-8 bytes.
  * Android XML key names AND string values are percent-encoded (space -> %20,
    | -> %7C, { -> %7B); alphanumerics and -_.*~ stay raw. Windows stores them decoded.
  * int    <-> REG_DWORD (dword:), negatives wrap to unsigned 32-bit.
  * float  <-> hex(4): 8-byte little-endian double, widened from float32.
  * string <-> hex: UTF-8 bytes followed by a single 00 terminator.

PRogues.prs facts (key confirmed in Assembly-CSharp-firstpass.dll):
  * The file is compact JSON XOR-ed with the repeating key "pumpkinheadBossLvl".
    No header, no checksum: the file is exactly as long as the JSON.
  * Some values are JSON stored inside a string (dead characters, their equipment).
    prs2json expands those into real objects so they can be edited, and records
    where it did so under "_nestedJson". json2prs folds them back using that list,
    or the built-in defaults when the list is missing (files from saveeditonline.com).
"""

import argparse
import itertools
import json
import os
import re
import struct
import sys
from decimal import Decimal
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

class ConvertError(Exception):
    """A problem the caller should show to the user (the CLI exits with it)."""


REG_KEY = r"HKEY_CURRENT_USER\Software\EtherGaming\Pocket Rogues"
REG_HEADER = "Windows Registry Editor Version 5.00"
NAME_RE = re.compile(r"^(.*)_h(\d+)$", re.S)
# Characters Unity leaves raw when it percent-encodes Android keys and string values.
URL_SAFE = "-_.*~"
PRS_KEY = b"pumpkinheadBossLvl"
# Paths (one "[*]" per list level) whose values the game stores as JSON-in-a-string.
PRS_NESTED_DEFAULT = ["deadCharacters[*]", "deadCharacters[*].equipment[*]"]
NESTED_KEY = "_nestedJson"


# ---------------------------------------------------------------- helpers

def unity_hash(key: str) -> int:
    h = 5381
    for b in key.encode("utf-8"):
        h = ((h * 33) ^ b) & 0xFFFFFFFF
    return h


def float32_to_text(value: float) -> str:
    """Shortest decimal that round-trips as float32, formatted like Java's Float.toString."""
    f32 = struct.unpack("<f", struct.pack("<f", value))[0]
    if f32 != f32:
        return "NaN"
    if f32 in (float("inf"), float("-inf")):
        return "Infinity" if f32 > 0 else "-Infinity"
    if f32 == 0:
        return "-0.0" if str(f32).startswith("-") else "0.0"
    s = repr(f32)
    for digits in range(1, 10):
        cand = f"{f32:.{digits}g}"
        try:
            if struct.unpack("<f", struct.pack("<f", float(cand)))[0] == f32:
                s = cand
                break
        except OverflowError:
            continue
    d = Decimal(s)
    mag = abs(f32)
    if 1e-3 <= mag < 1e7:
        s = format(d, "f")
        if "." not in s:
            s += ".0"
        return s
    # Java style: one digit before the point, at least one after, 'E' exponent.
    sign, digits_tuple, exp = d.as_tuple()
    digits_str = "".join(map(str, digits_tuple)).rstrip("0") or "0"
    exponent = exp + len(digits_tuple) - 1
    mant = digits_str[0] + "." + (digits_str[1:] or "0")
    return f"{'-' if sign else ''}{mant}E{exponent}"


# ---------------------------------------------------------------- XML side

def read_xml(path: str):
    """Return list of (key, type, value). Key is the decoded (real) name."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ConvertError(f"{path} is not well-formed XML: {exc}")
    entries = []
    for el in root.iter():
        if el.tag not in ("int", "float", "long", "boolean", "string") or "name" not in el.attrib:
            continue
        key = unquote(el.attrib["name"])
        value = el.attrib.get("value")
        if el.tag == "string":
            entries.append((key, "string", unquote(el.text or "")))
        elif value is None:
            raise ConvertError(f"{path}: <{el.tag} name={key!r}> has no value attribute")
        elif el.tag == "float":
            entries.append((key, "float", float(value)))
        elif el.tag == "boolean":
            entries.append((key, "int", 1 if value == "true" else 0))
        else:  # int, long
            entries.append((key, "int", int(value)))
    return entries


def write_xml(path: str, entries):
    lines = ["<?xml version='1.0' encoding='utf-8' standalone='yes' ?>", "<map>"]
    for key, typ, value in entries:
        name = escape(quote(key, safe=URL_SAFE), {'"': "&quot;"})
        if typ == "int":
            lines.append(f'    <int name="{name}" value="{value}" />')
        elif typ == "float":
            lines.append(f'    <float name="{name}" value="{float32_to_text(value)}" />')
        else:
            lines.append(f'    <string name="{name}">{escape(quote(value, safe=URL_SAFE))}</string>')
    lines.append("</map>")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- REG side

def read_reg(path: str):
    """Return list of (key, type, value) from the first [HKEY...] section of a .reg file."""
    with open(path, "rb") as fh:
        return read_reg_bytes(fh.read())


def read_reg_bytes(raw: bytes):
    """Same as read_reg, for .reg content already in memory."""
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
    text = text.replace("\r\n", "\n")

    # Join continuation lines (trailing backslash).
    joined = []
    buf = None
    for line in text.split("\n"):
        stripped = line.rstrip()
        if buf is not None:
            buf += stripped.lstrip()
        else:
            buf = stripped
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        joined.append(buf)
        buf = None
    if buf is not None:
        joined.append(buf)

    entries = []
    in_key = False
    for line in joined:
        if line.startswith("["):
            if in_key:
                break
            in_key = True
            continue
        if not in_key or not line.startswith('"'):
            continue
        # Split "name"=value, honouring \" and \\ escapes inside the name.
        i = 1
        name_chars = []
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                name_chars.append(line[i + 1])
                i += 2
                continue
            if c == '"':
                break
            name_chars.append(c)
            i += 1
        full_name = "".join(name_chars)
        rest = line[i + 1:]
        if not rest.startswith("="):
            continue
        rest = rest[1:]
        m = NAME_RE.match(full_name)
        key = m.group(1) if m else full_name

        if rest.startswith("dword:"):
            raw_int = int(rest[6:], 16)
            value = raw_int - 0x100000000 if raw_int >= 0x80000000 else raw_int
            entries.append((key, "int", value))
        elif rest.startswith("hex(4):"):
            data = bytes(int(x, 16) for x in rest[7:].split(",") if x)
            if len(data) == 8:
                entries.append((key, "float", struct.unpack("<d", data)[0]))
            elif len(data) == 4:
                entries.append((key, "int", struct.unpack("<i", data)[0]))
            else:
                print(f"warning: skipping {full_name!r}, hex(4) with {len(data)} bytes", file=sys.stderr)
        elif rest.startswith("hex:"):
            data = bytes(int(x, 16) for x in rest[4:].split(",") if x)
            if data.endswith(b"\x00"):
                data = data[:-1]
            entries.append((key, "string", data.decode("utf-8", errors="replace")))
        elif rest.startswith('"'):
            s = rest[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            entries.append((key, "string", s))
        else:
            print(f"warning: skipping {full_name!r}, unknown value kind {rest[:12]!r}", file=sys.stderr)
    return entries


def reg_hex_lines(prefix: str, data: bytes, width: int = 80):
    """Emit hex bytes the way regedit does: lines capped at `width`, continuation via backslash."""
    tokens = [f"{b:02x}" for b in data]
    lines = []
    line = prefix
    for idx, tok in enumerate(tokens):
        last = idx == len(tokens) - 1
        piece = tok if last else tok + ","
        # Non-final lines end with a backslash, so leave room for it.
        limit = width if last else width - 1
        if len(line) + len(piece) > limit and line not in (prefix, "  "):
            lines.append(line + "\\")
            line = "  "
        line += piece
    lines.append(line)
    return lines


def write_reg(path: str, entries):
    out = [REG_HEADER, "", f"[{REG_KEY}]"]
    for key, typ, value in entries:
        name = f"{key}_h{unity_hash(key)}".replace("\\", "\\\\").replace('"', '\\"')
        prefix = f'"{name}"='
        if typ == "int":
            out.append(f"{prefix}dword:{value & 0xFFFFFFFF:08x}")
        elif typ == "float":
            f32 = struct.unpack("<f", struct.pack("<f", value))[0]
            out.extend(reg_hex_lines(prefix + "hex(4):", struct.pack("<d", f32)))
        else:
            out.extend(reg_hex_lines(prefix + "hex:", value.encode("utf-8") + b"\x00"))
    out.append("")
    with open(path, "wb") as fh:
        fh.write("﻿".encode("utf-16-le"))
        fh.write(("\r\n".join(out) + "\r\n").encode("utf-16-le"))


# ---------------------------------------------------------------- PRS side

def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _expand_nested(value, path, found):
    """Parse JSON-in-a-string values recursively; record the path pattern of each."""
    if isinstance(value, dict):
        return {k: _expand_nested(v, f"{path}.{k}" if path else k, found) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_nested(v, f"{path}[*]", found) for v in value]
    if isinstance(value, str) and value[:1] in "{[":
        try:
            inner = json.loads(value)
        except ValueError:
            return value
        if isinstance(inner, (dict, list)) and _compact(inner) == value:
            if path not in found:
                found.append(path)
            return _expand_nested(inner, path, found)
    return value


def _fold_nested(value, path, patterns):
    """Inverse of _expand_nested: re-stringify objects sitting at recorded paths."""
    if isinstance(value, dict):
        value = {k: _fold_nested(v, f"{path}.{k}" if path else k, patterns) for k, v in value.items()}
    elif isinstance(value, list):
        value = [_fold_nested(v, f"{path}[*]", patterns) for v in value]
    if path in patterns and isinstance(value, (dict, list)):
        return _compact(value)
    return value


def prs_to_json(src: str, dst: str):
    raw = open(src, "rb").read()
    plain = bytes(a ^ b for a, b in zip(raw, itertools.cycle(PRS_KEY)))
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConvertError(f"{src} did not decrypt to JSON with the known key ({exc}); not a Pocket Rogues save?")
    found = []
    expanded = _expand_nested(data, "", found)
    expanded[NESTED_KEY] = found
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(expanded, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return len(data)


def json_to_prs(src: str, dst: str):
    data = json.loads(open(src, encoding="utf-8-sig").read())
    patterns = data.pop(NESTED_KEY, None)
    if patterns is None:
        patterns = PRS_NESTED_DEFAULT
    folded = _fold_nested(data, "", set(patterns))
    plain = _compact(folded).encode("utf-8")
    with open(dst, "wb") as fh:
        fh.write(bytes(a ^ b for a, b in zip(plain, itertools.cycle(PRS_KEY))))
    return len(folded)


# ---------------------------------------------------------------- driver

def merge(base_entries, new_entries):
    new_keys = {k for k, _, _ in new_entries}
    kept = [e for e in base_entries if e[0] not in new_keys]
    return kept + new_entries


MODES = ("xml2reg", "reg2xml", "prs2json", "json2prs")
EXT_MODES = {".xml": "xml2reg", ".reg": "reg2xml", ".prs": "prs2json", ".json": "json2prs", ".txt": "json2prs"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("args", nargs="+", help="[mode] input output   (mode: " + "|".join(MODES) + ")")
    p.add_argument("--base", help="xml2reg/reg2xml only: existing file whose unmentioned entries are kept")
    ns = p.parse_args(argv)

    args = list(ns.args)
    mode = args.pop(0) if args and args[0] in MODES else None
    if len(args) != 2:
        p.error("need input and output paths")
    src, dst = args
    if mode is None:
        mode = mode_for(src)
        if mode is None:
            p.error(f"cannot pick a mode from {src!r}; name one of {', '.join(MODES)}")

    try:
        count = convert(mode, src, dst, ns.base)
    except ConvertError as exc:
        sys.exit(str(exc))
    print(f"{mode}: wrote {count} entries to {dst}")


def mode_for(path: str):
    """Conversion mode implied by a file's extension, or None."""
    ext = path[path.rfind("."):].lower() if "." in os.path.basename(path) else ""
    return EXT_MODES.get(ext)


def convert(mode: str, src: str, dst: str, base: str = None) -> int:
    """Run one conversion; returns the number of entries written."""
    if mode not in MODES:
        raise ConvertError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    if not os.path.isfile(src):
        raise ConvertError(f"input file not found: {src}")
    if mode == "xml2reg":
        entries = read_xml(src)
        if base:
            entries = merge(read_reg(base), entries)
        write_reg(dst, entries)
        return len(entries)
    if mode == "reg2xml":
        entries = read_reg(src)
        if base:
            entries = merge(read_xml(base), entries)
        write_xml(dst, entries)
        return len(entries)
    if mode == "prs2json":
        return prs_to_json(src, dst)
    return json_to_prs(src, dst)


if __name__ == "__main__":
    main()
