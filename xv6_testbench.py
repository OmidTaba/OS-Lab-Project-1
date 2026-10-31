#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xv6 OS-Lab-1 (Fall 1405) -- black-box test bench
================================================

What it does
------------
* BUILD tests  : run `make clean`, `make -n`, `make`; check kernel link command,
                 bootblock (512 bytes + 0x55AA), kernel ELF, rle in UPROGS ...
* VM tests     : boot the freshly built xv6 in QEMU (headless, -snapshot so the
                 disk is never modified), press real keys through the QEMU monitor
                 (`sendkey`) and read the VGA text buffer (0xB8000) to see exactly
                 what the console shows.  Covers:
                   - boot message with the group's names
                   - cursor movement / Backspace / Ctrl+Q / Ctrl+E
                   - select / copy / paste (Ctrl+I, Ctrl+C, Ctrl+V) + highlight
                   - alias
                   - history + !! + !n + !prefix
                   - rle encode / decode (+ error handling)
                   - combined scenarios and a randomized "fuzz" test that compares
                     the console against a reference model of the spec

Usage (from the xv6 repo root, or pass --repo):
    python3 xv6_testbench.py --members "Ali,Sara,Omid"
    python3 xv6_testbench.py --only CUR,SEL           # groups or ids (regex)
    python3 xv6_testbench.py --list
    python3 xv6_testbench.py --report TESTCASES.md    # table for the lab report
    python3 xv6_testbench.py --selftest               # tests the tests (no QEMU)

Results: PASS / FAIL / WARN (soft: spec is ambiguous or optional) / SKIP / ERROR
"""
import argparse
import math
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback

COLS, ROWS = 80, 25
BASE_ATTR = 0x07


class Skip(Exception):
    pass


REGISTRY = []


def test(tid, group, desc, reason, soft=False, vm=True):
    def deco(fn):
        REGISTRY.append(dict(id=tid, group=group, desc=desc, reason=reason,
                             soft=soft, vm=vm, fn=fn))
        return fn
    return deco


class Cfg:
    repo = "."
    qemu = None
    members = ""
    keydelay = 0.08
    quiet = 0.15
    boot_timeout = 90
    fake = False
    make_n = ""
    make_ok = None
    make_log = ""
    no_build = False
    outdir = "."


CFG = Cfg()

# --------------------------------------------------------------------------
# key naming (QEMU `sendkey` names)
# --------------------------------------------------------------------------
KEYMAP = {" ": "spc", "!": "shift-1", "=": "equal", "-": "minus", ".": "dot",
          ",": "comma", "/": "slash", ">": "shift-dot"}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    KEYMAP[_c] = _c
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEYMAP[_c] = "shift-" + _c.lower()


def clean_ansi(b):
    s = b.decode("latin-1")
    s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)
    return s.replace("\r", "")


# --------------------------------------------------------------------------
# Real machine: QEMU + HMP monitor
# --------------------------------------------------------------------------
class QemuMachine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tmp = tempfile.mkdtemp(prefix="xv6tb_")
        self.sock_path = os.path.join(self.tmp, "mon.sock")
        self.errlog = open(os.path.join(self.tmp, "qemu.err"), "w+")
        repo = os.path.abspath(cfg.repo)
        for f in ("xv6.img", "fs.img"):
            if not os.path.exists(os.path.join(repo, f)):
                raise RuntimeError("%s not found in %s (build failed?)" % (f, repo))
        cmd = [cfg.qemu,
               "-drive", "file=%s/fs.img,index=1,media=disk,format=raw" % repo,
               "-drive", "file=%s/xv6.img,index=0,media=disk,format=raw" % repo,
               "-smp", "1", "-m", "512", "-display", "none",
               "-serial", "file:" + os.path.join(self.tmp, "serial.log"),
               "-monitor", "unix:%s,server,nowait" % self.sock_path,
               "-snapshot", "-no-reboot"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self.errlog)
        self.sock = None
        t0 = time.time()
        while time.time() - t0 < 15:
            if self.proc.poll() is not None:
                self.errlog.seek(0)
                raise RuntimeError("qemu exited: " + self.errlog.read())
            if os.path.exists(self.sock_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.sock_path)
                    s.settimeout(0.5)
                    self.sock = s
                    break
                except OSError:
                    pass
            time.sleep(0.1)
        if self.sock is None:
            raise RuntimeError("cannot connect to QEMU monitor")
        self._read_prompt()
        self._cursor_ok = True

    def _read_prompt(self, timeout=15):
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                chunk = None
            if chunk == b"":
                raise RuntimeError("monitor closed")
            if chunk:
                buf += chunk
            if clean_ansi(buf).rstrip().endswith("(qemu)"):
                return clean_ansi(buf)
        raise RuntimeError("monitor timeout")

    def cmd(self, c):
        self.sock.sendall((c + "\n").encode())
        return self._read_prompt()

    def key(self, name):
        self.cmd("sendkey %s 30" % name)
        time.sleep(self.cfg.keydelay)

    def screen(self):
        out = self.cmd("xp /2000xh 0xb8000")
        words = []
        for line in out.split("\n"):
            m = re.match(r"^\s*[0-9a-fA-F]+:\s*(.*)$", line)
            if m:
                words += [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", m.group(1))]
        if len(words) < COLS * ROWS:
            raise RuntimeError("could not read VGA memory (got %d words)" % len(words))
        rows, attrs = [], []
        for r in range(ROWS):
            ws = words[r * COLS:(r + 1) * COLS]
            rows.append("".join(chr(w & 0xFF) if 32 <= (w & 0xFF) < 127 else " " for w in ws))
            attrs.append([(w >> 8) & 0xFF for w in ws])
        return rows, attrs

    def _inb(self, port):
        out = self.cmd("i /b 0x%x" % port)
        if "unknown" in out.lower() or "error" in out.lower():
            raise RuntimeError("ioport read unsupported")
        vals = re.findall(r"0x[0-9a-fA-F]+", out.split(":", 1)[-1])
        return int(vals[-1], 16)

    def cursor(self):
        if not self._cursor_ok:
            return None
        try:
            self.cmd("o /b 0x3d4 0xe")
            hi = self._inb(0x3d5)
            self.cmd("o /b 0x3d4 0xf")
            lo = self._inb(0x3d5)
            return (hi << 8) | lo
        except Exception:
            self._cursor_ok = False
            return None

    def close(self):
        try:
            if self.sock:
                self.sock.sendall(b"quit\n")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Fake machine = reference implementation of the spec (ONLY for --selftest)
# --------------------------------------------------------------------------
class FakeMachine:
    CH = {"spc": " ", "equal": "=", "minus": "-", "dot": ".", "comma": ",",
          "slash": "/", "shift-1": "!", "shift-dot": ">"}

    def __init__(self, cfg):
        self.ch = [[" "] * COLS for _ in range(ROWS)]
        self.at = [[BASE_ATTR] * COLS for _ in range(ROWS)]
        self.r = self.c = 0
        self.files = {"README": "xv6 is a re-implementation of Dennis Ritchie\nxv6 loosely follows\n"}
        self.alias, self.hist, self.hn = {}, [], 0
        self.buf, self.cur, self.state = [], 0, "n"
        self.start, self.sel, self.clip = 0, (0, 0), []
        self.prow = 0
        self.pcol = 0
        self.in_input = False
        team = (cfg.members or "Alice,Bob,Carol").replace(",", " ")
        for ln in ["cpu0: starting 0", "sb: size 1000 nblocks 941", "Team: " + team,
                   "init: starting sh"]:
            self.puts(ln + "\n")
        self.prompt()

    # --- screen ---
    def _scroll(self):
        self.ch = self.ch[1:] + [[" "] * COLS]
        self.at = self.at[1:] + [[BASE_ATTR] * COLS]
        self.prow -= 1

    def _nl(self):
        self.c = 0
        self.r += 1
        if self.r >= ROWS:
            self._scroll()
            self.r = ROWS - 1

    def puts(self, t):
        for x in t:
            if x == "\n":
                self._nl()
            else:
                self.ch[self.r][self.c] = x
                self.at[self.r][self.c] = BASE_ATTR
                self.c += 1
                if self.c >= COLS:
                    self._nl()

    def prompt(self):
        self.pcol = self.c
        self.puts("$ ")
        self.prow = self.r
        self.in_input = True

    def redraw(self):
        text = "".join(self.buf)
        pc = self.pcol
        need = max(1, (pc + 2 + len(text) + COLS - 1) // COLS)
        while self.prow + need > ROWS:
            self._scroll()
        for rr in range(self.prow, min(ROWS, self.prow + need + 1)):
            lo = pc if rr == self.prow else 0
            for cc in range(lo, COLS):
                self.ch[rr][cc] = " "
                self.at[rr][cc] = BASE_ATTR
        self.ch[self.prow][pc], self.ch[self.prow][pc + 1] = "$", " "
        for i, x in enumerate(text):
            pos = pc + 2 + i
            rr, cc = self.prow + pos // COLS, pos % COLS
            self.ch[rr][cc] = x
            self.at[rr][cc] = 0x70 if (self.state == "s" and self.sel[0] <= i < self.sel[1]) else BASE_ATTR

    # --- keys ---
    def _rng(self):
        return self.sel

    def key(self, name):
        n = name
        ch = self.CH.get(n)
        if ch is None and len(n) == 1:
            ch = n
        if ch is None and n.startswith("shift-") and len(n) == 7:
            ch = n[-1].upper()
        b = self.buf
        if ch is not None:
            if self.state == "s":
                a, e = self.sel
                b[a:e] = [ch]
                self.cur, self.state = a + 1, "n"
            elif self.state == "n" and len(b) < 127:
                b.insert(self.cur, ch)
                self.cur += 1
        elif n == "left":
            if self.state in "nm" and self.cur > 0:
                self.cur -= 1
        elif n == "right":
            if self.state in "nm" and self.cur < len(b):
                self.cur += 1
        elif n == "ctrl-q":
            if self.state in "nm":
                self.cur = 0
        elif n == "ctrl-e":
            if self.state in "nm":
                self.cur = len(b)
        elif n == "backspace":
            if self.state == "s":
                a, e = self.sel
                del b[a:e]
                self.cur, self.state = a, "n"
            elif self.state == "n" and self.cur > 0:
                del b[self.cur - 1]
                self.cur -= 1
        elif n == "ctrl-i":
            if self.state == "n":
                self.state, self.start = "m", self.cur
            elif self.state == "m":
                a, e = sorted((self.start, self.cur))
                if a == e:
                    self.state = "n"
                else:
                    self.state, self.sel = "s", (a, e)
        elif n == "ctrl-c":
            if self.state == "s":
                self.clip = b[self.sel[0]:self.sel[1]]
        elif n == "ctrl-v":
            if self.state == "s":
                a, e = self.sel
                b[a:e] = list(self.clip)
                self.cur, self.state = a + len(self.clip), "n"
            elif self.state == "n":
                b[self.cur:self.cur] = list(self.clip)
                self.cur += len(self.clip)
        elif n == "ret":
            self._enter()
            return
        self.redraw()

    def _enter(self):
        text = "".join(self.buf)
        self.buf, self.cur, self.state = [], 0, "n"
        self.redraw_text = text
        pos = self.pcol + 2 + len(text)
        self.r, self.c = self.prow + pos // COLS, pos % COLS
        self.in_input = False
        self._nl()
        self.execute(text)
        self.prompt()

    # --- shell ---
    def hist_add(self, line):
        self.hn += 1
        self.hist.append((self.hn, line))
        self.hist = self.hist[-10:]

    def execute(self, line):
        if not line.strip():
            return
        if line.startswith("!"):
            key = line[1:].strip()
            exp = None
            if key == "!" and self.hist:
                exp = self.hist[-1][1]
            elif key.isdigit():
                exp = next((c for n, c in self.hist if n == int(key)), None)
            elif key and key != "!":
                exp = next((c for n, c in reversed(self.hist) if c.startswith(key)), None)
            if exp is None:
                self.puts("history: event not found\n")
                return
            line = exp
            self.puts(line + "\n")
        self.hist_add(line)
        words = line.split()
        if words and words[0] in self.alias:
            line = self.alias[words[0]] + line.lstrip()[len(words[0]):]
        out_file = None
        if ">" in line:
            line, out_file = line.split(">", 1)
            out_file = out_file.strip()
        w = line.split()
        out = self.run_cmd(w, line)
        if out_file:
            self.files[out_file] = out
        else:
            self.puts(out)

    def run_cmd(self, w, line):
        if not w:
            return ""
        c, a = w[0], w[1:]
        if c == "echo":
            return " ".join(a) + "\n"
        if c == "cat":
            o = ""
            for f in a:
                o += self.files[f] if f in self.files else "cat: cannot open %s\n" % f
            return o
        if c == "ls":
            return "".join("%s 2 %d\n" % (n, len(v)) for n, v in
                           sorted(self.files.items())) + "".join(
                "%s 2 99 9999\n" % p for p in ("cat", "echo", "ls", "grep", "rle"))
        if c == "history":
            return "".join("%d %s\n" % (n, cm) for n, cm in self.hist)
        if c == "alias":
            rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            if "=" in rest:
                k, v = rest.split("=", 1)
                self.alias[k.strip()] = v.strip()
            return ""
        if c == "grep":
            pat, f = a[0], a[1]
            return "".join(l + "\n" for l in self.files.get(f, "").split("\n") if l and pat in l)
        if c == "rle":
            if len(a) != 2 or a[0] not in ("encode", "decode"):
                return "usage: rle encode|decode <file>\n"
            if a[1] not in self.files:
                return "rle: cannot open %s\n" % a[1]
            d = self.files[a[1]]
            if a[0] == "encode":
                enc, i = "", 0
                while i < len(d):
                    j = i
                    while j < len(d) and d[j] == d[i]:
                        j += 1
                    enc += "%s,%d;" % (d[i], j - i)
                    i = j
                self.files["encoded.txt"] = enc
            else:
                self.files["decoded.txt"] = "".join(
                    ch * int(n) for ch, n in re.findall(r"(.),(\d+);", d, re.S))
            return ""
        return "exec %s failed\n" % c

    # --- inspection ---
    def screen(self):
        return ["".join(r) for r in self.ch], [list(r) for r in self.at]

    def cursor(self):
        if self.in_input:
            return self.prow * COLS + self.pcol + 2 + self.cur
        return self.r * COLS + self.c

    def close(self):
        pass


# --------------------------------------------------------------------------
# Session: helpers used by tests
# --------------------------------------------------------------------------
def dump(rows):
    return "\n".join("|" + r.rstrip() for r in rows if r.strip())


def is_prompt_row(r):
    return r.startswith("$ ") or r.rstrip() == "$"


class Session:
    def __init__(self, cfg):
        self.cfg = cfg
        self.m = FakeMachine(cfg) if cfg.fake else QemuMachine(cfg)
        self.boot_rows = None
        self.pcol = 0   # screen column where the current "$ " prompt starts
        self._wait_boot()

    def close(self):
        self.m.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- keys
    def key(self, name):
        self.m.key(name)

    def type(self, text):
        for ch in text:
            self.m.key(KEYMAP[ch])

    def left(self, n=1):
        for _ in range(n):
            self.key("left")

    def right(self, n=1):
        for _ in range(n):
            self.key("right")

    def home(self):
        self.key("ctrl-q")

    def end(self):
        self.key("ctrl-e")

    def bs(self, n=1):
        for _ in range(n):
            self.key("backspace")

    def mark(self):
        self.key("ctrl-i")

    def copy(self):
        self.key("ctrl-c")

    def paste(self):
        self.key("ctrl-v")

    # --- screen
    def snapshot(self, timeout=6):
        prev, since, t0 = None, None, time.time()
        while True:
            cur = self.m.screen()
            if prev is not None and cur == prev:
                if time.time() - since >= self.cfg.quiet:
                    return cur
            else:
                prev, since = cur, time.time()
            if time.time() - t0 > timeout:
                return cur
            if self.cfg.fake:
                time.sleep(0)

    def _wait_boot(self):
        t0 = time.time()
        while time.time() - t0 < self.cfg.boot_timeout:
            rows, _ = self.snapshot()
            if any(is_prompt_row(r) for r in rows):
                self.boot_rows = rows
                return
            time.sleep(0.3)
        raise AssertionError("xv6 did not reach the shell prompt in %ds. Screen:\n%s"
                             % (self.cfg.boot_timeout, dump(self.snapshot()[0])))

    def last_prompt(self, rows):
        """row index of the active prompt (it can start in the middle of a row when the previous
        output had no trailing newline, e.g. `cat encoded.txt`)"""
        c = self.pcol
        idx = [i for i, r in enumerate(rows) if r[c:c + 2] == "$ " or r[c:].rstrip() == "$"]
        return idx[-1] if idx else None

    def line(self):
        rows, _ = self.snapshot()
        p = self.last_prompt(rows)
        if p is None:
            raise AssertionError("no prompt on screen:\n" + dump(rows))
        q = p
        while q < ROWS and rows[q].strip():
            q += 1
        return "".join([rows[p][self.pcol:]] + rows[p + 1:q])[2:].rstrip()

    def line_attrs(self):
        rows, attrs = self.snapshot()
        p = self.last_prompt(rows)
        return attrs[p], rows[p]

    def cursor_index(self):
        """cursor position inside the input line (0 = right after '$ '), or None"""
        pos = self.m.cursor()
        if pos is None:
            return None
        rows, _ = self.snapshot()
        p = self.last_prompt(rows)
        return pos - p * COLS - self.pcol - 2

    # --- running commands
    def collect(self, shown, timeout=40):
        """wait for the next prompt, return output rows of the command that was just entered.
        `shown` = text that is displayed on the command row (what was in the buffer)."""
        deadline = time.time() + timeout
        old_pcol = self.pcol
        while True:
            rows, _ = self.snapshot()
            ne = [i for i, r in enumerate(rows) if r.strip()]
            if ne and rows[ne[-1]].rstrip().endswith("$"):
                last = ne[-1]
                break
            if time.time() > deadline:
                raise AssertionError("timeout waiting for prompt. Screen:\n" + dump(rows))
            time.sleep(0.2)
        new_pcol = len(rows[last].rstrip()) - 1
        tail = rows[last][:new_pcol].rstrip()          # output that shares the row with the new prompt
        before = [i for i in range(last) if rows[i][old_pcol:old_pcol + 2] == "$ "
                  or rows[i][old_pcol:].rstrip() == "$"]
        if not before:
            raise AssertionError("command scrolled off screen:\n" + dump(rows))
        cmd_row = before[-1]
        if shown is not None and rows[cmd_row][old_pcol + 2:].rstrip() != shown[:COLS - old_pcol - 2].rstrip():
            raise AssertionError("cannot find command row for %r (output too long?). Screen:\n%s"
                                 % (shown, dump(rows)))
        start = cmd_row + max(1, math.ceil((old_pcol + 2 + len(shown or "")) / COLS))
        out = [r.rstrip() for r in rows[start:last]]
        if tail:
            out.append(tail)
        while out and not out[-1]:
            out.pop()
        self.pcol = new_pcol
        return out

    def run(self, cmd):
        self.type(cmd)
        self.key("ret")
        return self.collect(cmd)

    def enter(self, shown):
        self.key("ret")
        return self.collect(shown)

    def history(self):
        out = self.run("history")
        res = []
        for r in out:
            m = re.match(r"^\s*(\d+)\s*[:.)\]\-]*\s+(\S.*)$", r)
            if m:
                res.append((int(m.group(1)), m.group(2).strip()))
        if not res:
            raise AssertionError("`history` printed nothing parsable: %r" % out)
        return res

    def select_fwd(self, a, b):
        """select [a,b) walking forward (assumes normal state)"""
        self.home()
        self.right(a)
        self.mark()
        self.right(b - a)
        self.mark()


# --------------------------------------------------------------------------
# assertion helpers
# --------------------------------------------------------------------------
def expect_line(s, want, ctx=""):
    got = s.line()
    if got.rstrip() != want.rstrip():
        raise AssertionError("%sinput line is %r, expected %r" % (ctx + ": " if ctx else "", got, want))


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def no_crash(rows):
    txt = "\n".join(rows).lower()
    expect("trap" not in txt and "panic" not in txt, "kernel trap/panic on screen:\n" + "\n".join(rows))


# ==========================================================================
#                                BUILD TESTS
# ==========================================================================
def _read(path):
    with open(os.path.join(CFG.repo, path), "rb") as f:
        return f.read()


def read_elf32(path):
    d = open(path, "rb").read()
    expect(d[:4] == b"\x7fELF" and d[4] == 1, "%s is not a 32-bit ELF" % path)
    (e_type, e_machine, _v, e_entry, e_phoff, _sh, _fl, _eh, e_phentsize,
     e_phnum) = struct.unpack_from("<HHIIIIIHHH", d, 16)
    phs = []
    for i in range(e_phnum):
        t, off, va, pa, fsz, msz, fl, al = struct.unpack_from("<IIIIIIII", d, e_phoff + i * e_phentsize)
        phs.append(dict(type=t, off=off, vaddr=va, paddr=pa, filesz=fsz, memsz=msz))
    return dict(machine=e_machine, entry=e_entry, ph=phs)






@test("BLD-3", "build", "bootblock is exactly 512 bytes, ends with 0x55AA, starts with `cli`",
      "Q12-Q13: BIOS only boots a sector whose last two bytes are 0x55 0xAA; bootasm.S starts with cli (0xFA).",
      vm=False)
def t_bootblock(_s=None):
    d = _read("bootblock")
    expect(len(d) == 512, "bootblock is %d bytes, expected 512" % len(d))
    expect(d[510:512] == b"\x55\xaa", "boot signature missing: %r" % d[510:512])
    expect(d[0] == 0xFA, "first byte is 0x%02x, bootasm.S should start with cli (0xFA)" % d[0])


@test("BLD-4", "build", "first sector of xv6.img == bootblock",
      "Q12: the file placed in the first disk sector is bootblock.", vm=False)
def t_img_first_sector(_s=None):
    expect(_read("xv6.img")[:512] == _read("bootblock"), "xv6.img[0:512] differs from bootblock")


@test("BLD-5", "build", "kernel ELF: i386, entry ~0x10000c, first PT_LOAD paddr 0x100000",
      "Q17: kernel is loaded at physical 0x100000 and `entry` lives there; boot loader jumps to e_entry.",
      vm=False)
def t_kernel_elf(_s=None):
    e = read_elf32(os.path.join(CFG.repo, "kernel"))
    expect(e["machine"] == 3, "e_machine=%d, expected 3 (i386)" % e["machine"])
    expect(0x100000 <= e["entry"] < 0x101000, "entry=0x%x not near 0x100000" % e["entry"])
    loads = [p for p in e["ph"] if p["type"] == 1]
    expect(loads and loads[0]["paddr"] == 0x100000, "first PT_LOAD paddr is not 0x100000")


@test("BLD-6", "build", "Makefile UPROGS contains _rle and rle.c exists",
      "Spec: rle must be built like ls/cat, i.e. registered in the Makefile (Q10).", vm=False)
def t_makefile_rle(_s=None):
    mk = _read("Makefile").decode("utf-8", "replace")
    expect(re.search(r"^\s*_rle\b", mk, re.M) or re.search(r"UPROGS[^\n]*_rle\b", mk) or "_rle\\" in mk
           or re.search(r"_rle\s*\\?\s*$", mk, re.M), "_rle not found in Makefile")
    expect(os.path.exists(os.path.join(CFG.repo, "rle.c")), "rle.c missing")


@test("BLD-7", "build", "rle.c uses open/read/write/close and the file names encoded.txt/decoded.txt",
      "Spec: rle must be implemented with those four syscalls and the exact output file names.", vm=False)
def t_rle_static(_s=None):
    src = _read("rle.c").decode("utf-8", "replace")
    for fn in ("open", "read", "write", "close"):
        expect(re.search(r"\b%s\s*\(" % fn, src), "rle.c never calls %s()" % fn)
    for nm in ("encoded.txt", "decoded.txt"):
        expect(nm in src, "%s not mentioned in rle.c" % nm)


# ==========================================================================
#                                BOOT MESSAGE
# ==========================================================================
@test("BOOT-1", "boot", "Group member names are printed on the console before the shell prompt",
      "Spec: names must appear among the boot messages, before `$ `. Screen is also saved for the report screenshot.")
def t_boot(s):
    rows = s.boot_rows
    path = os.path.join(CFG.outdir, "boot_screen.txt")
    with open(path, "w") as f:
        f.write(dump(rows) + "\n")
    names = [n.strip() for n in CFG.members.split(",") if n.strip()]
    if not names:
        raise Skip("pass --members \"A,B,C\" to check names automatically. Screen saved to %s" % path)
    p = s.last_prompt(rows)
    head = "\n".join(rows[:p])
    for n in names:
        expect(n.lower() in head.lower(), "name %r not printed before the prompt. Boot screen:\n%s" % (n, dump(rows)))
    expect("init: starting sh" in head, "original boot message 'init: starting sh' disappeared")


# ==========================================================================
#                          CURSOR MOVEMENT (feature 1)
# ==========================================================================
@test("CUR-1", "CUR", "Typing characters shows them in the input line",
      "Baseline: console input/echo still works after the rewrite.")
def t_cur1(s):
    s.type("echo hello")
    expect_line(s, "echo hello")


@test("CUR-2", "CUR", "Left arrow x2, then insert 'X' in the middle",
      "Spec: Left moves one char; typing must INSERT at the cursor (not overwrite / not append).")
def t_cur2(s):
    s.type("echo hello")
    s.left(2)
    s.type("X")
    expect_line(s, "echo helXlo")


@test("CUR-3", "CUR", "Right arrow moves back right after Left",
      "Spec: Right moves one char; combination Left*3 + Right*1 must land on the correct slot.")
def t_cur3(s):
    s.type("abc")
    s.left(3)
    s.right(1)
    s.type("X")
    expect_line(s, "aXbc")


@test("CUR-4", "CUR", "Ctrl+Q goes to start, Ctrl+E goes to end",
      "Spec: Home/End equivalents. Checked by inserting a character after each jump.")
def t_cur4(s):
    s.type("world")
    s.home()
    s.type("X")
    expect_line(s, "Xworld")
    s.end()
    s.type("Y")
    expect_line(s, "XworldY")


@test("CUR-5", "CUR", "Backspace in the middle deletes the char BEFORE the cursor and keeps the rest",
      "Spec: Backspace must follow the cursor; chars after cursor shift left and cursor stays consistent.")
def t_cur5(s):
    s.type("abcd")
    s.left(2)
    s.bs()
    expect_line(s, "acd")
    s.type("X")
    expect_line(s, "aXcd")


@test("CUR-6", "CUR", "Boundaries: Left/Backspace at start and Right at end do nothing",
      "Spec explicitly lists these edge cases (no change, no underflow, prompt not erased).")
def t_cur6(s):
    s.type("ab")
    s.home()
    s.left(2)
    s.bs(2)
    expect_line(s, "ab")
    s.end()
    s.right(3)
    s.type("c")
    expect_line(s, "abc")
    s.home()
    s.type("X")
    expect_line(s, "Xabc")


@test("CUR-7", "CUR", "Enter in the middle of the line executes the WHOLE buffer",
      "Spec: displayed text, cursor and real input buffer must agree; the shell must receive all of it.")
def t_cur7(s):
    s.type("echo abc")
    s.left(1)
    s.type("Z")
    out = s.enter("echo abZc")
    expect(out == ["abZc"], "shell output was %r, expected ['abZc']" % out)


@test("CUR-8", "CUR", "Backspace on an empty prompt does not erase the '$ ' prompt",
      "Classic bug when rewriting consoleintr: erasing past the start of the input.")
def t_cur8(s):
    s.bs(4)
    rows, _ = s.snapshot()
    expect(rows[s.last_prompt(rows)].startswith("$ "), "prompt damaged:\n" + dump(rows))
    s.type("echo x")
    expect_line(s, "echo x")
    out = s.enter("echo x")
    expect(out == ["x"], "got %r" % out)


@test("CUR-9", "CUR", "Hardware cursor position matches the logical cursor (VGA CRT registers)",
      "Spec: 'text, cursor position and real buffer must be consistent'. Skipped if QEMU can't read I/O ports.",
      soft=True)
def t_cur9(s):
    s.type("abcdef")
    s.left(2)
    idx = s.cursor_index()
    if idx is None:
        raise Skip("QEMU monitor could not read VGA CRT ports")
    expect(idx == 4, "cursor is at input index %d, expected 4" % idx)
    s.home()
    expect(s.cursor_index() == 0, "after Ctrl+Q cursor index = %r, expected 0" % s.cursor_index())
    s.end()
    expect(s.cursor_index() == 6, "after Ctrl+E cursor index = %r, expected 6" % s.cursor_index())


@test("CUR-10", "CUR", "Long input (130 chars) is capped, no overflow/crash",
      "Buffer-end off-by-one is the typical memory bug when adding insertion support (xv6 INPUT_BUF=128).",
      soft=True)
def t_cur10(s):
    s.type("x" * 130)
    n = len(s.line())
    expect(n <= 128, "input line shows %d chars (> buffer size 128)" % n)
    s.key("ret")
    time.sleep(1.5)
    s.snapshot()
    out = s.run("echo alive")
    expect("alive" in out, "shell not responsive after overflow: %r" % out)


# ==========================================================================
#                      SELECT / COPY / PASTE (feature 2)
# ==========================================================================
@test("SEL-1", "SEL", "Select forward (Ctrl+I, Right*5, Ctrl+I) then Backspace deletes the selection",
      "Spec: select between two marks; Backspace deletes the whole selection.")
def t_sel1(s):
    s.type("echo hello")
    s.select_fwd(5, 10)
    s.bs()
    expect_line(s, "echo")


@test("SEL-2", "SEL", "Select BACKWARD (second mark before first) selects the same range",
      "Spec: order of marks must not matter.")
def t_sel2(s):
    s.type("echo hello")
    s.mark()
    s.left(5)
    s.mark()
    s.bs()
    expect_line(s, "echo")


@test("SEL-3", "SEL", "Same start and end => nothing selected (Backspace deletes just 1 char)",
      "Spec: equal start/end positions select nothing.")
def t_sel3(s):
    s.type("abc")
    s.mark()
    s.mark()
    s.bs()
    expect_line(s, "ab")


@test("SEL-4", "SEL", "Ctrl+C keeps the selection active (Backspace afterwards deletes it)",
      "Spec: after Ctrl+C selection remains; all other ops end the selection mode.")
def t_sel4(s):
    s.type("echo hello")
    s.select_fwd(5, 10)
    s.copy()
    s.bs()
    expect_line(s, "echo")








@test("SEL-8", "SEL", "After deleting a selection the console is back to normal mode",
      "Spec: every op except Ctrl+C ends selection; the next Backspace must delete a single char.")
def t_sel8(s):
    s.type("echo hello")
    s.select_fwd(4, 10)
    s.bs()
    expect_line(s, "echo")
    s.bs()
    expect_line(s, "ech")


@test("SEL-9", "SEL", "Selected characters are highlighted (different VGA attribute); others are not",
      "Spec: selection must be visibly marked (colour/highlight). Read directly from VGA memory.")
def t_sel9(s):
    s.type("echo hello")
    s.select_fwd(5, 10)
    attrs, row = s.line_attrs()
    base = attrs[0]
    for i in range(5, 10):
        expect(attrs[2 + i] != base, "char %d (%r) is not highlighted (attr 0x%02x == prompt attr)"
               % (i, row[2 + i], attrs[2 + i]))
    for i in range(0, 5):
        expect(attrs[2 + i] == base, "char %d outside selection is highlighted (attr 0x%02x)" % (i, attrs[2 + i]))


@test("SEL-10", "SEL", "Highlight disappears after the selection operation finishes",
      "Spec: after any op (except Ctrl+C) normal mode returns, so no stale highlight may remain.")
def t_sel10(s):
    s.type("echo hello")
    s.select_fwd(5, 10)
    s.copy()
    s.paste()
    attrs, row = s.line_attrs()
    base = attrs[0]
    cur = s.cursor_index()
    for i in range(10):
        if cur is not None and i == cur:
            continue
        expect(attrs[2 + i] == base, "stale highlight on char %d (attr 0x%02x)" % (i, attrs[2 + i]))






@test("SEL-13", "SEL", "Moving the cursor between the two marks works with Ctrl+Q / Ctrl+E too",
      "Spec: only cursor-movement keys are allowed between the two marks; Ctrl+Q/E count as movement.")
def t_sel13(s):
    s.type("abcdef")
    s.left(2)
    s.mark()
    s.end()
    s.mark()
    s.bs()
    expect_line(s, "abcd")
    s.type("1")
    s.home()
    s.mark()
    s.right(2)
    s.mark()
    s.bs()
    expect_line(s, "cd1")


@test("SEL-14", "SEL", "Clipboard persists to the next prompt (copy on one line, paste on the next)",
      "Spec doesn't say the clipboard resets on Enter; typical shells keep it.", soft=True)
def t_sel14(s):
    s.type("echo qq")
    s.select_fwd(5, 7)
    s.copy()
    s.paste()
    out = s.enter("echo qq")
    expect(out == ["qq"], "got %r" % out)
    s.type("echo ")
    s.paste()
    expect_line(s, "echo qq")


# ==========================================================================
#                              ALIAS (feature 3)
# ==========================================================================
@test("ALIAS-1", "ALIAS", "alias hi=echo hello world ; `hi there` -> 'hello world there'",
      "Spec: everything after '=' is the command; user args are appended unchanged.")
def t_al1(s):
    expect(s.run("alias hi=echo hello world") == [], "alias definition printed something")
    out = s.run("hi there")
    expect(out == ["hello world there"], "got %r" % out)


@test("ALIAS-2", "ALIAS", "Redefining an alias replaces the old value",
      "Spec: a second alias with the same name overrides the first.")
def t_al2(s):
    s.run("alias hi=echo one")
    s.run("alias hi=echo two")
    out = s.run("hi x")
    expect(out == ["two x"], "got %r" % out)


@test("ALIAS-3", "ALIAS", "Only the FIRST word is replaced (alias name used later in the line is untouched)",
      "Spec: only the first word of the command is compared with alias names.")
def t_al3(s):
    s.run("alias hi=echo HELLO")
    out = s.run("echo hi")
    expect(out == ["hi"], "got %r (alias must not apply to non-first words)" % out)


@test("ALIAS-4", "ALIAS", "Alias name must match the whole word (prefix/longer word does not match)",
      "Spec: 'exactly equal' first word.")
def t_al4(s):
    s.run("alias ec=echo Z")
    out = s.run("ecx hi")
    expect(out and "Z" not in out, "ecx wrongly matched alias 'ec': %r" % out)
    out = s.run("ec")
    expect(out == ["Z"], "got %r" % out)


@test("ALIAS-5", "ALIAS", "Everything after the first '=' belongs to the command (alias x=echo a=b)",
      "Spec: all text after '=' is the command, even if it contains another '='.")
def t_al5(s):
    s.run("alias x=echo a=b")
    out = s.run("x")
    expect(out == ["a=b"], "got %r" % out)


@test("ALIAS-6", "ALIAS", "Alias expansion is NOT recursive (one step only)",
      "Spec: 'only one alias expansion per command, no recursion'.")
def t_al6(s):
    s.run("alias aa=bb")
    s.run("alias bb=echo BEE")
    out = s.run("aa")
    expect("BEE" not in out, "alias was expanded recursively: %r" % out)
    expect(out, "expected an 'exec bb failed'-style message, got nothing")


@test("ALIAS-7", "ALIAS", "At least 10 aliases can coexist",
      "Spec: 'at least 10 aliases at the same time'. We define 12 and call all.")
def t_al7(s):
    for i in range(1, 13):
        s.run("alias q%d=echo v%d" % (i, i))
    for i in range(1, 13):
        out = s.run("q%d" % i)
        expect(out == ["v%d" % i], "alias q%d -> %r" % (i, out))


@test("ALIAS-8", "ALIAS", "Aliases are per shell session: fresh boot has none",
      "Spec: aliases are not persisted.")
def t_al8(s):
    out = s.run("hi")
    expect(out and "exec" in "".join(out).lower() or out, "expected an exec failure, got %r" % out)
    expect(out != ["hello"], "unexpected alias present")


# ==========================================================================
#                          HISTORY EXPANSION (feature 4)
# ==========================================================================
@test("HIST-1", "HIST", "`history` lists recent commands in order with increasing numbers",
      "Spec: history shows commands with numbers, chronological order.")
def t_h1(s):
    for c in ("echo hello", "echo two", "echo three"):
        s.run(c)
    h = s.history()
    cmds = [c for _, c in h]
    i = cmds.index("echo hello")
    expect(cmds[i:i + 3] == ["echo hello", "echo two", "echo three"], "order wrong: %r" % h)
    nums = [n for n, _ in h]
    expect(nums == sorted(nums) and len(set(nums)) == len(nums), "numbers not strictly increasing: %r" % nums)
    expect(nums[i + 1] == nums[i] + 1 and nums[i + 2] == nums[i] + 2, "numbers not consecutive: %r" % h)


@test("HIST-2", "HIST", "`!!` re-runs the last command and shows the expanded command first",
      "Spec: !! = last command; expanded line must be printed before execution.")
def t_h2(s):
    s.run("echo one")
    s.run("echo two")
    out = s.run("!!")
    expect(any(r.strip() == "echo two" for r in out), "expanded command not displayed: %r" % out)
    expect(out and out[-1] == "two", "command output missing: %r" % out)


@test("HIST-3", "HIST", "`!n` runs the command with history number n",
      "Spec: n = number shown by `history`.")
def t_h3(s):
    s.run("echo alpha")
    s.run("echo beta")
    h = s.history()
    n = [n for n, c in h if c == "echo alpha"][0]
    out = s.run("!%d" % n)
    expect(any(r.strip() == "echo alpha" for r in out) and out[-1] == "alpha", "got %r" % out)






@test("HIST-6", "HIST", "`!!`, `!n`, `!prefix` are never stored; only the expanded command is",
      "Spec: history must contain final commands, not the bang forms.")
def t_h6(s):
    s.run("echo q1")
    s.run("!!")
    s.run("!ec")
    h = s.history()
    expect(not any(c.startswith("!") for _, c in h), "bang command stored: %r" % h)
    expect([c for _, c in h].count("echo q1") == 3, "expected 3x 'echo q1' (original + 2 expansions): %r" % h)


@test("HIST-7", "HIST", "Keeps >=10 recent commands, numbers keep increasing and never get renumbered",
      "Spec: min 10 entries; deleting old ones must not change the numbers of the others.")
def t_h7(s):
    for k in range(1, 15):
        s.run("echo n%d" % k)
    h1 = s.history()
    expect(len(h1) >= 10, "only %d entries kept" % len(h1))
    nums = [n for n, _ in h1]
    expect(nums == list(range(nums[0], nums[0] + len(nums))), "numbers not consecutive: %r" % h1)
    for k in range(3):
        s.run("echo m%d" % k)
    h2 = dict((c, n) for n, c in s.history())
    for n, c in h1:
        if c in h2 and c.startswith("echo n"):
            expect(h2[c] == n, "number of %r changed from %d to %d" % (c, n, h2[c]))
    expect(max(h2.values()) > max(nums), "numbers did not keep growing")


@test("HIST-8", "HIST", "`!n` with a number that was evicted from the window is an error",
      "Spec: only commands still in history can be re-run.", soft=True)
def t_h8(s):
    for k in range(1, 16):
        s.run("echo e%d" % k)
    h = s.history()
    low = min(n for n, _ in h)
    if low <= 1:
        raise Skip("history window >= 15, nothing was evicted")
    out = s.run("!%d" % (low - 1))
    expect(out and not any(r.startswith("e") and r[1:].isdigit() for r in out),
           "evicted entry still executed: %r" % out)


# ==========================================================================
#                              RLE USER PROGRAM
# ==========================================================================
def mkfile(s, name, text):
    out = s.run("echo %s > %s" % (text, name))
    expect(out == [], "creating %s printed %r" % (name, out))


@test("RLE-1", "RLE", "`ls` lists rle (program was added to the build)",
      "Spec: rle must be runnable like ls/cat => UPROGS/fs.img integration (Q10).")
def t_rle1(s):
    out = s.run("ls")
    expect(any(r.split()[:1] == ["rle"] for r in out), "rle not in ls output:\n" + "\n".join(out))






@test("RLE-4", "RLE", "Round trip with digits, commas and spaces in the data",
      "Spec: separator ',' and ';' format must be unambiguous even when data contains digits/commas.")
def t_rle4(s):
    txt = "a1,,22 zz  q-9"
    mkfile(s, "t.txt", txt)
    s.run("rle encode t.txt")
    s.run("rle decode encoded.txt")
    out = s.run("cat decoded.txt")
    expect(out == ["a1,,22 zz q-9"], "decoded.txt = %r (echo collapses double space)" % out)


@test("RLE-5", "RLE", "All-distinct and single chars: 'abc' -> a,1;b,1;c,1;",
      "Boundary: runs of length 1.")
def t_rle5(s):
    mkfile(s, "t.txt", "abc")
    s.run("rle encode t.txt")
    out = s.run("cat encoded.txt")
    expect(out and out[0].startswith("a,1;b,1;c,1;"), "got %r" % out)


@test("RLE-6", "RLE", "Two-digit run lengths: 12 x 'a' -> a,12;  and decode restores 12",
      "Boundary: multi-digit counts must be parsed/printed correctly.")
def t_rle6(s):
    mkfile(s, "t.txt", "a" * 12)
    s.run("rle encode t.txt")
    out = s.run("cat encoded.txt")
    expect(out and out[0].startswith("a,12;"), "got %r" % out)
    s.run("rle decode encoded.txt")
    out = s.run("cat decoded.txt")
    expect(out == ["a" * 12], "decoded = %r" % out)


@test("RLE-7", "RLE", "Multi-line file (newline is just a character) round-trips",
      "Spec: any byte can appear in the file; newline runs must survive encode/decode.")
def t_rle7(s):
    mkfile(s, "a.txt", "xxy")
    mkfile(s, "b.txt", "zz")
    s.run("cat a.txt b.txt > c.txt")
    s.run("rle encode c.txt")
    s.run("rle decode encoded.txt")
    out = s.run("cat decoded.txt")
    expect(out == ["xxy", "zz"], "decoded = %r" % out)




@test("RLE-9", "RLE", "Invalid usage prints an error (no args, bad mode, missing file, extra arg, nonexistent file)",
      "Spec: wrong mode / wrong argument count / unopenable file => message and exit, no crash.")
def t_rle9(s):
    for c in ("rle", "rle encode", "rle compress input.txt", "rle encode a b", "rle decode nosuchfile.txt",
              "rle encode nosuchfile.txt"):
        out = s.run(c)
        expect(out, "`%s` printed no error message" % c)
        no_crash(out)
    out = s.run("echo alive")
    expect(out == ["alive"], "shell broken afterwards: %r" % out)


@test("RLE-10", "RLE", "Failed commands do not create output files",
      "Housekeeping: an invalid call should not leave encoded.txt/decoded.txt behind.", soft=True)
def t_rle10(s):
    s.run("rle encode nosuchfile.txt")
    s.run("rle decode nosuchfile.txt")
    out = s.run("ls")
    names = [r.split()[0] for r in out if r.split()]
    expect("encoded.txt" not in names and "decoded.txt" not in names, "stray output files: %r" % names)


@test("RLE-11", "RLE", "Re-running encode with a SHORTER result leaves no leftover bytes from the older file",
      "xv6 open(O_CREATE) does not truncate: programs must unlink/overwrite properly or old bytes remain.",
      soft=True)
def t_rle11(s):
    mkfile(s, "long.txt", "a" * 20)
    mkfile(s, "short.txt", "b")
    s.run("rle encode long.txt")
    s.run("rle encode short.txt")
    out = s.run("cat encoded.txt")
    expect(out == ["b,1;", ",1;"], "stale bytes in encoded.txt: %r" % out)
    s.run("rle decode encoded.txt")
    out = s.run("cat decoded.txt")
    expect(out == ["b"], "stale bytes in decoded.txt: %r" % out)


# ==========================================================================
#                              COMBINED SCENARIOS
# ==========================================================================
@test("COMB-1", "COMB", "Edit with cursor keys, then run (typo fixed in the middle) ; history stores the FINAL text",
      "Spec: history/alias/console must interplay; history gets the edited command, not raw keystrokes.")
def t_c1(s):
    s.type("echo hllo")
    s.left(3)
    s.type("e")
    expect_line(s, "echo hello")
    out = s.enter("echo hello")
    expect(out == ["hello"], "got %r" % out)
    out = s.run("!!")
    expect(out and out[-1] == "hello", "!! did not re-run the edited command: %r" % out)
    h = s.history()
    expect([c for _, c in h].count("echo hello") == 2, "history = %r" % h)


@test("COMB-2", "COMB", "Alias + cursor editing: fix alias name by moving cursor",
      "Spec: aliases must work on commands that were produced by cursor editing.")
def t_c2(s):
    s.run("alias zz=echo Z")
    s.type("z hello")
    s.home()
    s.right(1)
    s.type("z")
    expect_line(s, "zz hello")
    out = s.enter("zz hello")
    expect(out == ["Z hello"], "got %r" % out)






@test("COMB-5", "COMB", "Edit a history command while typing it: '!ecc' -> Left, Backspace -> '!ec'",
      "History bang forms must go through the same editable buffer.")
def t_c5(s):
    s.run("echo hi")
    s.type("!ecc")
    s.left(1)
    s.bs()
    expect_line(s, "!ec")
    out = s.enter("!ec")
    expect(out and out[-1] == "hi", "got %r" % out)


@test("COMB-6", "COMB", "Order of expansion: history FIRST, alias SECOND",
      "Spec: `!!` expands to `zz one` (stored form) and then the alias turns it into echo Z one.")
def t_c6(s):
    s.run("alias zz=echo Z")
    out = s.run("zz one")
    expect(out == ["Z one"], "got %r" % out)
    out = s.run("!!")
    expect(out and out[-1] == "Z one", "history->alias chain failed: %r" % out)
    out = s.run("!z")
    expect(out and out[-1] == "Z one", "!prefix->alias chain failed: %r" % out)




@test("COMB-8", "COMB", "A pending selection mark does not leak into the next command line",
      "Spec doesn't define it; a clean reset on Enter is the expected behaviour.", soft=True)
def t_c8(s):
    s.type("abc")
    s.mark()
    s.left(1)
    s.enter("abc")
    s.type("echo ok")
    s.left(2)
    s.type("X")
    expect_line(s, "echo Xok")
    s.right(1)
    s.type("Y")
    expect_line(s, "echo XoYk")




# ==========================================================================
#                 RANDOMIZED MODEL-BASED CONSOLE TEST ("fuzz")
# ==========================================================================
class EditModel:
    """Reference model of the console editing spec (only unambiguous behaviours are generated)."""

    def __init__(self):
        self.buf, self.cur, self.state = [], 0, "n"
        self.start, self.sel, self.clip = 0, (0, 0), []

    def allowed(self):
        n = len(self.buf)
        if self.state == "n":
            ops = ["ch", "ch", "ch", "left", "right", "home", "end", "bs", "bs", "mark", "mark"]
            if self.clip:
                ops.append("paste")
            if n > 50:
                ops = [o for o in ops if o not in ("ch", "paste")]
            return ops
        if self.state == "m":
            return ["left", "right", "left", "right", "home", "end", "mark", "mark"]
        ops = ["ch", "bs", "copy", "paste_sel", "ch", "bs"]
        if not self.clip:
            ops.remove("paste_sel")
        return ops

    def apply(self, op, ch=None):
        b = self.buf
        if op == "ch":
            if self.state == "s":
                a, e = self.sel
                b[a:e] = [ch]
                self.cur, self.state = a + 1, "n"
            else:
                b.insert(self.cur, ch)
                self.cur += 1
        elif op == "left":
            self.cur = max(0, self.cur - 1)
        elif op == "right":
            self.cur = min(len(b), self.cur + 1)
        elif op == "home":
            self.cur = 0
        elif op == "end":
            self.cur = len(b)
        elif op == "bs":
            if self.state == "s":
                a, e = self.sel
                del b[a:e]
                self.cur, self.state = a, "n"
            elif self.cur > 0:
                del b[self.cur - 1]
                self.cur -= 1
        elif op == "mark":
            if self.state == "n":
                self.state, self.start = "m", self.cur
            else:
                a, e = sorted((self.start, self.cur))
                self.state, self.sel = ("n", (0, 0)) if a == e else ("s", (a, e))
        elif op == "copy":
            self.clip = b[self.sel[0]:self.sel[1]]
        elif op in ("paste", "paste_sel"):
            if self.state == "s":
                a, e = self.sel
                b[a:e] = list(self.clip)
                self.cur, self.state = a + len(self.clip), "n"
            else:
                b[self.cur:self.cur] = list(self.clip)
                self.cur += len(self.clip)

    def text(self):
        return "".join(self.buf)


KEYOF = {"left": "left", "right": "right", "home": "ctrl-q", "end": "ctrl-e", "bs": "backspace",
         "mark": "ctrl-i", "copy": "ctrl-c", "paste": "ctrl-v", "paste_sel": "ctrl-v"}


def fuzz(s, seed, steps):
    rnd = random.Random(seed)
    m = EditModel()
    trace, cursor_bad = [], None
    for i in range(steps):
        op = rnd.choice(m.allowed())
        ch = rnd.choice("abcdefgh0123 ") if op == "ch" else None
        if op == "paste" and len(m.clip) > 12:
            continue
        m.apply(op, ch)
        trace.append(op if ch is None else "ch(%r)" % ch)
        if op == "ch":
            s.type(ch)
        else:
            s.key(KEYOF[op])
        got = s.line()
        want = m.text().rstrip()
        if got.rstrip() != want:
            raise AssertionError("seed %d step %d after %s\n  console : %r\n  expected: %r"
                                 % (seed, i, trace[-8:], got, m.text()))
        if m.state != "s" and cursor_bad is None:
            idx = s.cursor_index()
            if idx is not None and idx != m.cur:
                cursor_bad = "seed %d step %d: hardware cursor index %d, expected %d (ops %s)" % (
                    seed, i, idx, m.cur, trace[-6:])
    return cursor_bad


def _mk_fuzz(seed):
    @test("FUZZ-%d" % seed, "FUZZ", "Random mix of 70 edit operations (seed %d) vs. reference model" % seed,
          "Catches index/off-by-one bugs and feature interactions that hand-written cases miss. "
          "Failure message prints the seed and last operations so it can be replayed.")
    def _t(s):
        bad = fuzz(s, seed, 70)
        if bad:
            raise AssertionError("text OK but " + bad)
    return _t


for _seed in (2, 3):
    _mk_fuzz(_seed)


# ==========================================================================
#                                  RUNNER
# ==========================================================================
def find_qemu():
    for c in (os.environ.get("QEMU"), "qemu-system-i386", "qemu-system-x86_64", "qemu"):
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def prepare_build(cfg):
    if cfg.no_build:
        return
    def mk(*args):
        return subprocess.run(["make", "-C", cfg.repo] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    print("[build] make clean ...")
    mk("clean")
    r = mk("-n")
    cfg.make_n = r.stdout
    print("[build] make ...")
    r = mk()
    cfg.make_ok = (r.returncode == 0)
    cfg.make_log = r.stdout
    print("[build] %s" % ("ok" if cfg.make_ok else "FAILED"))


COL = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m", "SKIP": "\033[36m", "ERROR": "\033[35m"}


def paint(tag):
    return (COL[tag] + tag + "\033[0m") if sys.stdout.isatty() else tag


def run_tests(selected, cfg):
    results = []
    for t in selected:
        tag, msg = "PASS", ""
        t0 = time.time()
        try:
            if not t["vm"]:
                if cfg.fake:
                    raise Skip("build tests need a real repo")
                t["fn"](None)
            else:
                with Session(cfg) as s:
                    t["fn"](s)
        except Skip as e:
            tag, msg = "SKIP", str(e)
        except AssertionError as e:
            tag, msg = ("WARN" if t["soft"] else "FAIL"), str(e)
        except Exception as e:  # harness / environment problem
            tag = "ERROR"
            msg = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc(limit=3))
        dt = time.time() - t0
        print("%-9s %-7s %s  (%.1fs)" % ("[" + paint(tag) + "]" if False else paint(tag), t["id"], t["desc"], dt))
        if msg and tag != "PASS":
            for ln in msg.splitlines():
                print("            " + ln)
        sys.stdout.flush()
        results.append((t, tag, msg))
    return results


def write_report(path, results):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Test cases and why they were chosen\n\n")
        f.write("| ID | Test case | Why this case | Result |\n|---|---|---|---|\n")
        for t, tag, _ in results:
            f.write("| %s | %s | %s | %s |\n" % (t["id"], t["desc"].replace("|", "/"),
                                                t["reason"].replace("|", "/"), tag))
    print("report written to", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="path of the xv6 repo (default: .)")
    ap.add_argument("--members", default=os.environ.get("OSLAB_MEMBERS", ""),
                    help='comma separated names expected in the boot message, e.g. "Ali,Sara,Omid"')
    ap.add_argument("--qemu", help="qemu binary (default: auto-detect, or $QEMU)")
    ap.add_argument("--only", help="comma separated regexes matched against test id / group")
    ap.add_argument("--skip", help="comma separated regexes of tests to skip")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-build", action="store_true", help="do not run make clean / make")
    ap.add_argument("--keydelay", type=float, default=0.08, help="seconds between key presses (raise if flaky)")
    ap.add_argument("--boot-timeout", type=int, default=90)
    ap.add_argument("--report", help="write markdown table of test cases + results")
    ap.add_argument("--selftest", action="store_true", help="run VM tests against a built-in simulated xv6")
    a = ap.parse_args()

    CFG.repo, CFG.members, CFG.keydelay = a.repo, a.members, a.keydelay
    CFG.boot_timeout, CFG.no_build, CFG.fake = a.boot_timeout, a.no_build, a.selftest
    CFG.outdir = os.getcwd()
    if a.selftest:
        CFG.keydelay, CFG.quiet, CFG.no_build = 0, 0, True
        CFG.members = CFG.members or "Alice,Bob,Carol"

    sel = list(REGISTRY)
    if a.only:
        pats = [re.compile(p, re.I) for p in a.only.split(",")]
        sel = [t for t in sel if any(p.search(t["id"]) or p.search(t["group"]) for p in pats)]
    if a.skip:
        pats = [re.compile(p, re.I) for p in a.skip.split(",")]
        sel = [t for t in sel if not any(p.search(t["id"]) or p.search(t["group"]) for p in pats)]
    if a.list:
        for t in sel:
            print("%-8s %-6s %s" % (t["id"], t["group"], t["desc"]))
        return 0

    if not a.selftest:
        CFG.qemu = a.qemu or find_qemu()
        if not os.path.isdir(CFG.repo) or not os.path.exists(os.path.join(CFG.repo, "Makefile")):
            print("ERROR: %s is not an xv6 repo (no Makefile). Use --repo." % CFG.repo)
            return 2
        if any(t["vm"] for t in sel) and not CFG.qemu:
            print("ERROR: qemu not found. Install it (sudo apt install qemu-system-x86) or use --qemu.")
            return 2
        if any(t["id"].startswith("BLD") for t in sel) or not a.no_build:
            prepare_build(CFG)
        elif not CFG.make_n:
            CFG.make_ok = os.path.exists(os.path.join(CFG.repo, "xv6.img"))
    results = run_tests(sel, CFG)
    cnt = {}
    for _, tag, _ in results:
        cnt[tag] = cnt.get(tag, 0) + 1
    print("\nSummary: " + ", ".join("%d %s" % (v, k) for k, v in sorted(cnt.items())))
    if a.report:
        write_report(a.report, results)
    return 1 if cnt.get("FAIL") or cnt.get("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
