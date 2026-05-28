#!/usr/bin/env python3
import curses, psutil, time, os, subprocess, threading, sys, glob, platform
from collections import deque
from datetime import datetime


REFRESH_RATE        = 0.8
HISTORY_SIZE        = 120
ALERT_DROP_PCT      = 5
ALERT_LOW_PCT       = 20
CRITICAL_LOW_PCT    = 10
OSCIL_WINDOW_SECS   = 300
OSCIL_EVENT_THRESH  = 3
OSCIL_RAPID_SECS    = 8
CURRENT_VAR_THRESH  = 0.15
SAVE_LOG            = True
LOG_FILE            = os.path.expanduser("~/.guardatensao.log")


C_AMBER  = 1
C_BRIGHT = 2
C_DIM    = 3
C_GOOD   = 4
C_WARN   = 5
C_CRIT   = 6
C_OSCIL  = 7
C_BORDER = 8
C_HDR    = 9
C_CYAN   = 10
C_WHITE  = 11
C_MAGENTA= 12


events          = deque(maxlen=200)
hist_pct        = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
hist_current    = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
hist_voltage    = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
hist_cpu        = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
hist_mem        = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
power_events    = deque()
alert_flash     = 0
oscil_detected  = False
oscil_score     = 0
beep_enabled    = True
running         = True
last_pct        = None
last_plugged    = None
last_current    = None
last_unplug_ts  = None
peak_current    = 0.0
min_voltage     = 9999.0
max_voltage     = 0.0
total_energy_mwh= 0.0
last_energy_ts  = None
charge_cycles   = 0
last_full_pct   = None
stats           = {
    "total_outages":  0,
    "total_oscil":    0,
    "total_warnings": 0,
    "longest_outage": 0,
    "session_start":  time.time(),
    "last_outage_ts": None,
}

def log_event(msg: str, level: str = "INFO"):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}][{level:5s}] {msg}"
    events.appendleft(entry)
    if SAVE_LOG:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{level}] {msg}\n")
        except Exception:
            pass

def notify(title, body, urgency="normal"):
    try:
        subprocess.Popen(["notify-send", "-u", urgency, "-t", "6000", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass

def beep(n=1):
    if beep_enabled:
        for _ in range(n):
            sys.stdout.write('\a'); sys.stdout.flush()
            if n > 1: time.sleep(0.15)

def secs_hms(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h: return f"{h}h{m:02d}m{sec:02d}s"
    return f"{m}m{sec:02d}s"

def uptime_str():
    return secs_hms(time.time() - stats["session_start"])

def get_temp():
    """Tenta ler temperatura da CPU."""
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp","k10temp","acpitz","cpu_thermal","zenpower"):
            if key in temps and temps[key]:
                vals = [t.current for t in temps[key] if t.label in ("","Package id 0","Tctl","cpu-thermal") or True]
                if vals:
                    return max(vals)
    except Exception:
        pass
    return None

def get_disk_io():
    try:
        d = psutil.disk_io_counters()
        return d.read_bytes, d.write_bytes
    except:
        return 0, 0

def get_net_io():
    try:
        n = psutil.net_io_counters()
        return n.bytes_sent, n.bytes_recv
    except:
        return 0, 0

def fmt_bytes(b):
    for u in ("B","K","M","G","T"):
        if b < 1024: return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}P"

def get_power():
    info = dict(battery_pct=None, plugged=None, time_left=None,
                status="—", voltage=None, current=None, ac_online=None,
                charge_status="unknown", power_w=None, capacity_mah=None,
                capacity_full=None, capacity_design=None, health_pct=None,
                cycle_count=None, manufacturer=None, model=None,
                technology=None, temp_battery=None)
    bat = psutil.sensors_battery()
    if bat:
        info["battery_pct"] = bat.percent
        info["plugged"]     = bat.power_plugged
        info["time_left"]   = bat.secsleft if bat.secsleft not in (-1,-2) else None
        info["status"]      = "Carregando" if bat.power_plugged else "Descarregando"
    try:
        for sp in glob.glob("/sys/class/power_supply/*"):
            tf = os.path.join(sp, "type")
            if not os.path.exists(tf): continue
            st = open(tf).read().strip()
            if st == "Mains":
                of = os.path.join(sp, "online")
                if os.path.exists(of):
                    info["ac_online"] = open(of).read().strip() == "1"
            elif st == "Battery":
                def rd(fn):
                    fp = os.path.join(sp, fn)
                    return open(fp).read().strip() if os.path.exists(fp) else None
                for k, fn, div in [
                    ("voltage","voltage_now",1_000_000),
                    ("current","current_now",1_000_000),
                    ("capacity_mah","charge_now",1_000),
                    ("capacity_full","charge_full",1_000),
                    ("capacity_design","charge_full_design",1_000),
                    ("temp_battery","temp",10),
                ]:
                    v = rd(fn)
                    if v:
                        try: info[k] = int(v)/div
                        except: pass
                # power in W
                if info["voltage"] and info["current"]:
                    info["power_w"] = info["voltage"] * abs(info["current"])
                # energia mWh alternativo
                for fn2, div2 in [("energy_now",1_000),("energy_full",1_000)]:
                    v = rd(fn2)
                    if v:
                        try:
                            k2 = "capacity_mah" if "now" in fn2 else "capacity_full"
                            info[k2] = float(v)/div2
                        except: pass
                # saúde
                if info.get("capacity_full") and info.get("capacity_design"):
                    info["health_pct"] = min(100, info["capacity_full"]/info["capacity_design"]*100)
                # metadados
                for k, fn in [("manufacturer","manufacturer"),("model","model_name"),
                               ("technology","technology"),("cycle_count","cycle_count")]:
                    v = rd(fn)
                    if v:
                        try: info[k] = int(v) if k=="cycle_count" else v.strip()
                        except: info[k] = v.strip()
                # status textual
                sf = os.path.join(sp, "status")
                if os.path.exists(sf):
                    raw = open(sf).read().strip()
                    info["charge_status"] = raw
                    m = {"Charging":"Carregando ⚡","Discharging":"Descarregando",
                         "Full":"Completa ✓","Not charging":"Sem carga","Unknown":"—"}
                    info["status"] = m.get(raw, raw)
    except: pass
    return info

_disk_r0, _disk_w0 = get_disk_io()
_net_s0,  _net_r0  = get_net_io()
_last_io_ts = time.time()
disk_r_rate = disk_w_rate = net_s_rate = net_r_rate = 0.0

def monitor_loop():
    global last_pct, last_plugged, last_current, last_unplug_ts
    global alert_flash, oscil_detected, oscil_score
    global peak_current, min_voltage, max_voltage, total_energy_mwh, last_energy_ts
    global charge_cycles, last_full_pct
    global _disk_r0, _disk_w0, _net_s0, _net_r0, _last_io_ts
    global disk_r_rate, disk_w_rate, net_s_rate, net_r_rate

    log_event("GuardaTensão v3 iniciado", "START")

    while running:
        info = get_power()
        now  = time.time()
        pct  = info["battery_pct"]
        cur  = info["current"]
        volt = info["voltage"]

        # — histórico —
        hist_pct.append(pct or 0.0)
        hist_current.append(abs(cur) if cur else 0.0)
        hist_voltage.append(volt or 0.0)
        cpu_now = psutil.cpu_percent(interval=None)
        mem_now = psutil.virtual_memory().percent
        hist_cpu.append(cpu_now)
        hist_mem.append(mem_now)

        # — picos —
        if cur:  peak_current = max(peak_current, abs(cur))
        if volt and volt > 1:
            if volt < min_voltage: min_voltage = volt
            if volt > max_voltage: max_voltage = volt

        # — taxa de I/O —
        dr, dw = get_disk_io()
        ns, nr = get_net_io()
        dt = now - _last_io_ts
        if dt > 0:
            disk_r_rate = (dr - _disk_r0) / dt
            disk_w_rate = (dw - _disk_w0) / dt
            net_s_rate  = (ns - _net_s0)  / dt
            net_r_rate  = (nr - _net_r0)  / dt
        _disk_r0, _disk_w0 = dr, dw
        _net_s0,  _net_r0  = ns, nr
        _last_io_ts = now

        # — energia acumulada —
        if info["power_w"] and last_energy_ts:
            total_energy_mwh += info["power_w"] * (now - last_energy_ts) / 3600 * 1000
        last_energy_ts = now

        # — ciclos de carga —
        if pct is not None:
            if last_full_pct is not None and last_full_pct < 95 and pct >= 95:
                charge_cycles += 1
            last_full_pct = pct

        # — plug/unplug —
        if last_plugged is not None and info["plugged"] != last_plugged:
            power_events.append(now)
            while power_events and power_events[0] < now - OSCIL_WINDOW_SECS:
                power_events.popleft()
            if info["plugged"]:
                delta = now - last_unplug_ts if last_unplug_ts else 999
                stats["longest_outage"] = max(stats["longest_outage"], delta)
                stats["last_outage_ts"] = now
                msg = f"Energia RETORNOU (fora {delta:.0f}s) — bat {pct:.0f}%"
                log_event(msg, "POWER")
                notify("⚡ Energia Voltou", f"Bat: {pct:.0f}%  |  fora {delta:.0f}s")
                beep(1); alert_flash = 2
                if delta <= OSCIL_RAPID_SECS:
                    stats["total_oscil"] += 1
                    log_event(f"OSCILAÇÃO RÁPIDA! ciclo {delta:.1f}s", "OSCIL")
                    notify("⚠ Oscilação Rápida!", f"Voltou em {delta:.1f}s", "critical")
                    beep(3); alert_flash = 8
            else:
                last_unplug_ts = now
                stats["total_outages"] += 1
                msg = f"QUEDA — bat {pct:.0f}%" if pct else "QUEDA DE ENERGIA"
                log_event(msg, "OUTAGE")
                notify("⚠ Queda!", f"Bat {pct:.0f}% — salve!", "critical")
                beep(2); alert_flash = 6

        recent = sum(1 for t in power_events if t > now - OSCIL_WINDOW_SECS)
        oscil_score = min(100, int(recent / OSCIL_EVENT_THRESH * 100))
        if recent >= OSCIL_EVENT_THRESH and not oscil_detected:
            oscil_detected = True
            stats["total_oscil"] += 1
            log_event(f"REDE INSTÁVEL: {recent} eventos/{OSCIL_WINDOW_SECS//60}min","OSCIL")
            notify("⚡ Rede Instável!", f"{recent} oscilações em {OSCIL_WINDOW_SECS//60}min","critical")
            beep(3); alert_flash = 10
        elif recent < OSCIL_EVENT_THRESH:
            oscil_detected = False

        if cur is not None and last_current is not None and info["plugged"]:
            delta_i = abs(abs(cur) - abs(last_current))
            if delta_i >= CURRENT_VAR_THRESH:
                log_event(f"Corrente instável: Δ{delta_i:.2f}A ({last_current:.2f}→{cur:.2f}A)","OSCIL")
                stats["total_warnings"] += 1
        last_current = cur

        if pct is not None and not info["plugged"]:
            if pct <= CRITICAL_LOW_PCT and (last_pct is None or last_pct > CRITICAL_LOW_PCT):
                log_event(f"CRÍTICO: bat {pct:.0f}%!", "CRIT")
                notify("🔴 Crítico!", f"{pct:.0f}% — conecte agora!", "critical")
                beep(3); alert_flash = 8; stats["total_warnings"] += 1
            elif pct <= ALERT_LOW_PCT and (last_pct is None or last_pct > ALERT_LOW_PCT):
                log_event(f"Bateria baixa: {pct:.0f}%", "WARN")
                notify("⚠ Baixa", f"{pct:.0f}% restantes")
                beep(1); stats["total_warnings"] += 1
            if last_pct is not None and (last_pct - pct) >= ALERT_DROP_PCT:
                log_event(f"Queda brusca: {last_pct:.0f}%→{pct:.0f}%", "WARN")
                beep(1)

        last_pct     = pct
        last_plugged = info["plugged"]
        time.sleep(REFRESH_RATE)

def safe(win, y, x, txt, attr=0):
    try:
        h,w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w and txt:
            win.addstr(y, x, str(txt)[:max(0,w-x-1)], attr)
    except curses.error: pass

def hline(win, y, x, ch, n, attr=0):
    try:
        h,w = win.getmaxyx()
        if 0 <= y < h:
            n = min(n, w-x-1)
            win.addstr(y, x, ch*n, attr)
    except curses.error: pass

def box(win, y, x, h, w, title="", color=C_BORDER):
    a = curses.color_pair(color) | curses.A_BOLD
    try:
        win.addstr(y,   x,   "╔" + "═"*(w-2) + "╗", a)
        for i in range(1,h-1):
            win.addstr(y+i, x,   "║", a)
            try: win.addstr(y+i, x+w-1, "║", a)
            except: pass
        win.addstr(y+h-1, x, "╚" + "═"*(w-2) + "╝", a)
        if title:
            t = f"┤ {title} ├"
            win.addstr(y, x+2, t, curses.color_pair(C_HDR) | curses.A_BOLD)
    except curses.error: pass

def hbar(win, y, x, w, pct, color):
    filled = max(0, min(w, int(w * pct / 100)))
    a_on   = curses.color_pair(color) | curses.A_BOLD
    a_off  = curses.color_pair(C_DIM)
    try:
        win.addstr(y, x,        "▰"*filled,    a_on)
        win.addstr(y, x+filled, "▱"*(w-filled), a_off)
    except curses.error: pass

BLOCKS = " ▁▂▃▄▅▆▇█"

def sparkline(win, y, x, w, data, h=4, color_high=C_AMBER, color_low=C_DIM, label_max=True):
    vals = list(data)[-w:]
    while len(vals) < w: vals.insert(0, 0)
    mx = max(vals) if max(vals) > 0 else 1
    mn = min(vals)
    try:
        for col, v in enumerate(vals):
            frac = v/mx
            for row in range(h):
                rt = row / h
                nt = (row+1) / h
                if frac >= nt:
                    ch = "█"
                    c  = color_high if row >= h//2 else color_low
                elif frac > rt:
                    sub = int((frac - rt) / (1/h) * 8)
                    ch  = BLOCKS[max(1, sub)]
                    c   = color_high
                else:
                    ch = "·" if row == 0 else " "
                    c  = C_DIM
                win.addstr(y + (h-1-row), x+col, ch, curses.color_pair(c))
    except curses.error: pass
    if label_max and w > 8:
        try:
            safe(win, y, x, f"{mx:.1f}", curses.color_pair(C_DIM))
            safe(win, y+h-1, x, f"{mn:.1f}", curses.color_pair(C_DIM))
        except: pass

def oscil_meter(win, y, x, w, score):
    filled = max(0, min(w, int(w * score / 100)))
    for i in range(filled):
        frac = i / max(w,1)
        c = C_GOOD if frac < 0.4 else (C_WARN if frac < 0.7 else C_CRIT)
        try: win.addstr(y, x+i, "█", curses.color_pair(c) | curses.A_BOLD)
        except curses.error: pass
    try: win.addstr(y, x+filled, "░"*(w-filled), curses.color_pair(C_DIM))
    except curses.error: pass

def label_val(win, y, x, label, val, lc=C_DIM, vc=C_AMBER, bold=False):
    """Imprime 'LABEL valor' com cores separadas."""
    safe(win, y, x, label, curses.color_pair(lc))
    attr = curses.color_pair(vc) | (curses.A_BOLD if bold else 0)
    safe(win, y, x+len(label), val, attr)

def divider(win, y, title="", color=C_DIM):
    SH, SW = win.getmaxyx()
    a = curses.color_pair(color)
    try:
        win.addstr(y, 0, "├" + "─"*(SW-2) + "┤", a)
        if title:
            t = f" {title} "
            cx = (SW - len(t)) // 2
            win.addstr(y, cx, t, curses.color_pair(C_HDR) | curses.A_BOLD)
    except: pass

LOGO = [
    " ▄▄  ▄  ▄  ▄▄  ▄▄  ▄▄  ▄▄     ▄▄  ▄▄  ▄▄  ▄▄ ▄  ▄  ▄▄  ▄▄ ",
    "█    █  █ █  █ █   █  █ █  █   █   █   █  █ █ █  █ █  █ █   ",
    "█ ▄▄ █  █ █▄▄█ █▄▄ █  █ █▄▄█   █▄▄ █▄▄ █  █ █  ▀▀  ██▄█ █▄▄",
    "█  █ █  █ █  █ █   █  █ █  █   █   █   █  █ █   █  █  █ █   ",
    " ▀▀   ▀▀  █  █ ▀▀   ▀▀  █  █   ▀▀  ▀▀   ▀▀  ▀   █  █  █ ▀▀ ",
]

def draw(stdscr, info, tick):
    global alert_flash
    SH, SW = stdscr.getmaxyx()
    stdscr.erase()

    if alert_flash > 0:
        if tick % 2 == 0:
            stdscr.bkgd(' ', curses.color_pair(C_CRIT) | curses.A_REVERSE)
        else:
            stdscr.bkgd(' ', 0)
        alert_flash -= 1
    else:
        stdscr.bkgd(' ', 0)

    pct     = info["battery_pct"]
    plugged = info["plugged"]
    now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    recent  = sum(1 for t in power_events if t > time.time() - OSCIL_WINDOW_SECS)

    # ── TOPO: LOGO + HEADER INLINE ────────────────────────────────────────────
    # Logo lado esquerdo, info lado direito
    logo_col = C_AMBER if tick%4<2 else C_BRIGHT
    for i, line in enumerate(LOGO):
        if i < SH:
            safe(stdscr, i, 1, line, curses.color_pair(logo_col) | curses.A_BOLD)

    # Painel direito do cabeçalho (ao lado do logo)
    lx = len(LOGO[0]) + 3
    if lx < SW - 30:
        # status principal
        if plugged is None:
            ps, pc = "SEM SENSOR", C_DIM
        elif plugged:
            ps, pc = "⚡ NA TOMADA", C_GOOD
        else:
            lv = ALERT_LOW_PCT
            ps, pc = "⚡ SEM ENERGIA", (C_CRIT if (pct or 100) <= lv else C_WARN)

        safe(stdscr, 0, lx, "STATUS  ", curses.color_pair(C_DIM))
        safe(stdscr, 0, lx+8, ps, curses.color_pair(pc) | curses.A_BOLD | curses.A_REVERSE)

        safe(stdscr, 1, lx, f"DATA    {now_str}", curses.color_pair(C_DIM))
        safe(stdscr, 2, lx, f"UPTIME  {uptime_str()}", curses.color_pair(C_CYAN))

        pct_col = C_GOOD if (pct or 100) > 60 else (C_WARN if (pct or 100) > ALERT_LOW_PCT else C_CRIT)
        pct_str = f"{pct:5.1f}%" if pct else "  N/D"
        safe(stdscr, 3, lx, f"BATERIA {pct_str}", curses.color_pair(pct_col) | curses.A_BOLD)
        if pct:
            bar_w = min(40, SW - lx - 18)
            safe(stdscr, 3, lx+14, "▕", curses.color_pair(C_DIM))
            hbar(stdscr, 3, lx+15, bar_w, pct, pct_col)
            safe(stdscr, 3, lx+15+bar_w, "▏", curses.color_pair(C_DIM))

        # oscilação no header
        osc_lbl = "REDE    "
        if oscil_score >= 60:
            safe(stdscr, 4, lx, osc_lbl + "◈ MUITO INSTÁVEL ◈", curses.color_pair(C_CRIT)|curses.A_BOLD)
        elif oscil_score >= 30:
            safe(stdscr, 4, lx, osc_lbl + "◇ OSCILANDO ◇", curses.color_pair(C_WARN)|curses.A_BOLD)
        else:
            safe(stdscr, 4, lx, osc_lbl + "✓ ESTÁVEL", curses.color_pair(C_GOOD)|curses.A_BOLD)

    row = len(LOGO) + 1

    # ── LINHA SEPARADORA TOTAL ────────────────────────────────────────────────
    hline(stdscr, row, 0, "═", SW, curses.color_pair(C_BORDER) | curses.A_BOLD)
    row += 1

    col_start = row
    cw = (SW - 4) // 3      # largura de cada coluna
    cA = 1                   # col A x
    cB = cA + cw + 1         # col B x
    cC = cB + cw + 1         # col C x

    # ── COLUNA A: BATERIA & ENERGIA ───────────────────────────────────────────
    r = col_start
    safe(stdscr, r, cA, "╔" + "═"*(cw-1), curses.color_pair(C_BORDER)|curses.A_BOLD)
    safe(stdscr, r, cA+2, "┤ BATERIA & ENERGIA ├", curses.color_pair(C_HDR)|curses.A_BOLD)
    r += 1

    if pct is not None:
        pct_col = C_GOOD if pct > 60 else (C_WARN if pct > ALERT_LOW_PCT else C_CRIT)
        label_val(stdscr, r, cA+1, "CARGA    ", f"{pct:5.1f}%", vc=pct_col, bold=True)
        bw = cw - 12
        hbar(stdscr, r, cA+11, bw, pct, pct_col)
        r += 1

        v = info.get("voltage")
        c = info.get("current")
        p = info.get("power_w")
        label_val(stdscr, r, cA+1, "TENSÃO   ", f"{v:.3f} V" if v else "  N/D   ", vc=C_CYAN)
        r += 1
        label_val(stdscr, r, cA+1, "CORRENTE ", f"{abs(c):.3f} A" if c else "  N/D   ", vc=C_CYAN)
        r += 1
        label_val(stdscr, r, cA+1, "POTÊNCIA ", f"{p:.2f} W" if p else "  N/D   ", vc=C_AMBER, bold=True)
        r += 1

        tl = info.get("time_left")
        label_val(stdscr, r, cA+1, "RESTANTE ", secs_hms(tl) if tl else "calcul…", vc=C_WHITE)
        r += 1
        label_val(stdscr, r, cA+1, "STATUS   ", info["status"], vc=C_GOOD if plugged else C_WARN)
        r += 1

        ac = info.get("ac_online")
        label_val(stdscr, r, cA+1, "AC REDE  ", ("ONLINE ✓" if ac else "OFFLINE ✗") if ac is not None else "N/D", vc=C_GOOD if ac else C_CRIT)
        r += 1

        # capacidade e saude
        cap = info.get("capacity_mah")
        full= info.get("capacity_full")
        des = info.get("capacity_design")
        hlth= info.get("health_pct")
        label_val(stdscr, r, cA+1, "CARGA mAh", f"{cap:.0f}" if cap else "N/D", vc=C_AMBER)
        if full: safe(stdscr, r, cA+20, f"/ {full:.0f} mAh", curses.color_pair(C_DIM))
        r += 1
        if hlth:
            hc = C_GOOD if hlth >= 80 else (C_WARN if hlth >= 60 else C_CRIT)
            label_val(stdscr, r, cA+1, "SAÚDE    ", f"{hlth:.1f}%", vc=hc, bold=True)
            hw = max(1, cw-14)
            hbar(stdscr, r, cA+14, hw, hlth, hc)
            r += 1

        # picas
        safe(stdscr, r, cA+1, "─── PICOS ───────────────────────", curses.color_pair(C_DIM))
        r += 1
        label_val(stdscr, r, cA+1, "I máx    ", f"{peak_current:.3f} A" if peak_current else "N/D", vc=C_WARN)
        r += 1
        if max_voltage > 0 and min_voltage < 9999:
            label_val(stdscr, r, cA+1, "V min/máx", f"{min_voltage:.3f} / {max_voltage:.3f} V", vc=C_CYAN)
            r += 1

        # energia acumulada na sessão
        label_val(stdscr, r, cA+1, "ENERGIA  ", f"{total_energy_mwh:.1f} mWh sessão", vc=C_DIM)
        r += 1
        label_val(stdscr, r, cA+1, "CICLOS   ", f"{charge_cycles} nesta sessão", vc=C_DIM)
        r += 1

        # metadados bateria
        safe(stdscr, r, cA+1, "─── HARDWARE ────────────────────", curses.color_pair(C_DIM))
        r += 1
        for k, lbl in [("manufacturer","FABRIC.  "),("model","MODELO   "),("technology","TECNOL.  ")]:
            v2 = info.get(k)
            if v2:
                label_val(stdscr, r, cA+1, lbl, str(v2)[:cw-12], vc=C_DIM)
                r += 1
        cc = info.get("cycle_count")
        if cc:
            label_val(stdscr, r, cA+1, "CICLOS HW", str(cc), vc=C_DIM)
            r += 1
        tb = info.get("temp_battery")
        if tb:
            tc = C_GOOD if tb < 40 else (C_WARN if tb < 55 else C_CRIT)
            label_val(stdscr, r, cA+1, "TEMP BAT ", f"{tb:.1f} °C", vc=tc, bold=True)
            r += 1
    else:
        safe(stdscr, r, cA+1, "Sem bateria — modo AC direto", curses.color_pair(C_DIM))
        r += 1

    col_A_end = r

    # ── COLUNA B: OSCILAÇÃO & HISTÓRICO ──────────────────────────────────────
    r = col_start
    safe(stdscr, r, cB, "╔" + "═"*(cw-1), curses.color_pair(C_BORDER)|curses.A_BOLD)
    safe(stdscr, r, cB+2, "┤ OSCILAÇÃO & HISTÓRICO ├", curses.color_pair(C_HDR)|curses.A_BOLD)
    r += 1

    # score de instabilidade
    safe(stdscr, r, cB+1, "INSTABILIDADE ", curses.color_pair(C_DIM))
    mw = cw - 20
    oscil_meter(stdscr, r, cB+15, mw, oscil_score)
    sc = C_CRIT if oscil_score >= 60 else (C_WARN if oscil_score >= 30 else C_GOOD)
    safe(stdscr, r, cB+16+mw, f"{oscil_score:3d}%", curses.color_pair(sc)|curses.A_BOLD)
    r += 1

    # indicadores
    ind1 = C_CRIT if oscil_detected else C_DIM
    ind2 = C_WARN if (last_unplug_ts and time.time()-last_unplug_ts < OSCIL_RAPID_SECS*3) else C_DIM
    ind3 = C_WARN if (info.get("current") and last_current and
                      abs(abs(info["current"])-abs(last_current)) >= CURRENT_VAR_THRESH*0.5) else C_DIM

    label_val(stdscr, r, cB+1, f"EVENTOS/{OSCIL_WINDOW_SECS//60}min ", str(recent), lc=C_DIM, vc=ind1, bold=True)
    r += 1
    label_val(stdscr, r, cB+1, "CICLO RÁPIDO  ", "SIM ◈" if ind2==C_WARN else "NÃO  ", vc=ind2, bold=True)
    r += 1
    label_val(stdscr, r, cB+1, "CORRENTE      ", "INSTÁVEL ◈" if ind3==C_WARN else "ESTÁVEL  ", vc=ind3, bold=True)
    r += 1
    label_val(stdscr, r, cB+1, "QUEDAS TOTAL  ", str(stats["total_outages"]), vc=C_CRIT if stats["total_outages"] else C_DIM)
    r += 1
    label_val(stdscr, r, cB+1, "OSCIL. TOTAL  ", str(stats["total_oscil"]), vc=C_WARN if stats["total_oscil"] else C_DIM)
    r += 1
    label_val(stdscr, r, cB+1, "AVISOS TOTAL  ", str(stats["total_warnings"]), vc=C_WARN if stats["total_warnings"] else C_DIM)
    r += 1
    if stats["longest_outage"] > 0:
        label_val(stdscr, r, cB+1, "MAIOR QUEDA   ", secs_hms(stats["longest_outage"]), vc=C_CRIT)
        r += 1

    # mensagem estado rede
    if oscil_score >= 60:
        msg = "◈ INSTÁVEL — considere UPS! ◈"
        mc  = C_CRIT
    elif oscil_score >= 30:
        msg = "◇ Oscilando — salve arquivos"
        mc  = C_WARN
    else:
        msg = "✓ Rede estável"
        mc  = C_GOOD
    safe(stdscr, r, cB+1, msg, curses.color_pair(mc)|curses.A_BOLD)
    r += 1

    safe(stdscr, r, cB+1, "─── GRÁFICO % BATERIA ───────────", curses.color_pair(C_DIM))
    r += 1
    gh = 5
    avail = cw - 3
    if r + gh + 1 < SH:
        sparkline(stdscr, r, cB+1, avail, hist_pct, h=gh, color_high=C_AMBER, color_low=C_DIM)
        safe(stdscr, r+gh, cB+1, "◂ hist", curses.color_pair(C_DIM))
        safe(stdscr, r+gh, cB+avail-5, "agora▸", curses.color_pair(C_DIM))
        r += gh + 1

    safe(stdscr, r, cB+1, "─── GRÁFICO CORRENTE (A) ────────", curses.color_pair(C_DIM))
    r += 1
    if r + gh + 1 < SH:
        sparkline(stdscr, r, cB+1, avail, hist_current, h=gh, color_high=C_CYAN, color_low=C_DIM)
        safe(stdscr, r+gh, cB+1, "◂ hist", curses.color_pair(C_DIM))
        safe(stdscr, r+gh, cB+avail-5, "agora▸", curses.color_pair(C_DIM))
        r += gh + 1

    safe(stdscr, r, cB+1, "─── GRÁFICO TENSÃO (V) ──────────", curses.color_pair(C_DIM))
    r += 1
    if r + gh + 1 < SH:
        sparkline(stdscr, r, cB+1, avail, hist_voltage, h=gh, color_high=C_OSCIL, color_low=C_DIM)
        safe(stdscr, r+gh, cB+1, "◂ hist", curses.color_pair(C_DIM))
        safe(stdscr, r+gh, cB+avail-5, "agora▸", curses.color_pair(C_DIM))
        r += gh + 1

    col_B_end = r

    # ── COLUNA C: SISTEMA ─────────────────────────────────────────────────────
    r = col_start
    safe(stdscr, r, cC, "╔" + "═"*(SW-cC-2), curses.color_pair(C_BORDER)|curses.A_BOLD)
    safe(stdscr, r, cC+2, "┤ SISTEMA ├", curses.color_pair(C_HDR)|curses.A_BOLD)
    r += 1

    # CPU
    cpu  = psutil.cpu_percent(interval=None)
    cpus = psutil.cpu_percent(interval=None, percpu=True)
    freq = psutil.cpu_freq()
    fmhz = f"{freq.current:.0f} MHz" if freq else "?"
    fmin = f"{freq.min:.0f}" if freq and freq.min else "?"
    fmax = f"{freq.max:.0f}" if freq and freq.max else "?"
    nc   = psutil.cpu_count(logical=True)
    nc_p = psutil.cpu_count(logical=False)
    temp = get_temp()

    safe(stdscr, r, cC+1, "─── CPU ─────────────────────────", curses.color_pair(C_DIM))
    r += 1
    label_val(stdscr, r, cC+1, "USO TOTAL ", f"{cpu:5.1f}%", vc=C_CRIT if cpu>85 else (C_WARN if cpu>60 else C_AMBER), bold=True)
    cw2 = SW - cC - 14
    hbar(stdscr, r, cC+13, cw2, cpu, C_CRIT if cpu>85 else (C_WARN if cpu>60 else C_GOOD))
    r += 1
    label_val(stdscr, r, cC+1, "FREQ      ", fmhz, vc=C_CYAN)
    safe(stdscr, r, cC+18, f"({fmin}~{fmax} MHz)", curses.color_pair(C_DIM))
    r += 1
    label_val(stdscr, r, cC+1, "NÚCLEOS   ", f"{nc_p} físicos / {nc} lógicos", vc=C_DIM)
    r += 1
    if temp:
        tc = C_GOOD if temp < 60 else (C_WARN if temp < 80 else C_CRIT)
        label_val(stdscr, r, cC+1, "TEMP CPU  ", f"{temp:.1f} °C", vc=tc, bold=True)
        r += 1

    # CPU por núcleo (max 8 mostrados)
    if cpus:
        safe(stdscr, r, cC+1, "POR NÚCLEO", curses.color_pair(C_DIM))
        r += 1
        cols_per_row = max(1,(SW-cC-3)//12)
        for i, p in enumerate(cpus[:16]):
            col_idx = i % cols_per_row
            row_idx = i // cols_per_row
            cx2 = cC+1 + col_idx*12
            ry  = r + row_idx
            if ry < SH-3:
                cc2 = C_CRIT if p>85 else (C_WARN if p>60 else C_GOOD)
                safe(stdscr, ry, cx2, f"C{i:<2}", curses.color_pair(C_DIM))
                safe(stdscr, ry, cx2+3, f"{p:5.1f}%", curses.color_pair(cc2))
        r += (len(cpus[:16])-1)//cols_per_row + 1

    # gráfico cpu
    safe(stdscr, r, cC+1, "─── HIST CPU % ──────────────────", curses.color_pair(C_DIM))
    r += 1
    gh2 = 4
    aw  = SW - cC - 3
    if r + gh2 + 1 < SH:
        sparkline(stdscr, r, cC+1, aw, hist_cpu, h=gh2, color_high=C_AMBER, color_low=C_DIM)
        r += gh2 + 1

    # RAM
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    safe(stdscr, r, cC+1, "─── MEMÓRIA ─────────────────────", curses.color_pair(C_DIM))
    r += 1
    mc2 = C_CRIT if mem.percent>90 else (C_WARN if mem.percent>75 else C_GOOD)
    label_val(stdscr, r, cC+1, "RAM       ", f"{mem.percent:5.1f}%  {mem.used//1024//1024}MB/{mem.total//1024//1024}MB", vc=mc2, bold=True)
    hbar(stdscr, r, cC+30, max(1,SW-cC-32), mem.percent, mc2)
    r += 1
    sc2 = C_WARN if swap.percent > 20 else C_DIM
    label_val(stdscr, r, cC+1, "SWAP      ", f"{swap.percent:5.1f}%  {swap.used//1024//1024}MB/{swap.total//1024//1024}MB", vc=sc2)
    r += 1

    # gráfico mem
    safe(stdscr, r, cC+1, "─── HIST RAM % ──────────────────", curses.color_pair(C_DIM))
    r += 1
    if r + gh2 + 1 < SH:
        sparkline(stdscr, r, cC+1, aw, hist_mem, h=gh2, color_high=C_CYAN, color_low=C_DIM)
        r += gh2 + 1

    # Disco & rede
    safe(stdscr, r, cC+1, "─── I/O DISCO & REDE ────────────", curses.color_pair(C_DIM))
    r += 1
    label_val(stdscr, r, cC+1, "DISCO R↓  ", fmt_bytes(disk_r_rate)+"/s", vc=C_AMBER)
    safe(stdscr, r, cC+20, "  W↑ "+fmt_bytes(disk_w_rate)+"/s", curses.color_pair(C_WARN))
    r += 1
    label_val(stdscr, r, cC+1, "REDE  R↓  ", fmt_bytes(net_r_rate)+"/s", vc=C_CYAN)
    safe(stdscr, r, cC+20, "  S↑ "+fmt_bytes(net_s_rate)+"/s", curses.color_pair(C_GOOD))
    r += 1

    # partições
    try:
        parts = psutil.disk_partitions(all=False)
        safe(stdscr, r, cC+1, "─── PARTIÇÕES ───────────────────", curses.color_pair(C_DIM))
        r += 1
        for p in parts[:4]:
            try:
                u = psutil.disk_usage(p.mountpoint)
                dc = C_CRIT if u.percent>90 else (C_WARN if u.percent>75 else C_GOOD)
                lbl = f"{p.mountpoint[:10]:<10}"
                val = f"{u.percent:5.1f}% {fmt_bytes(u.used)}/{fmt_bytes(u.total)}"
                label_val(stdscr, r, cC+1, lbl+" ", val, vc=dc)
                r += 1
                if r >= SH-3: break
            except: pass
    except: pass

    # processos top
    try:
        procs = sorted(psutil.process_iter(['pid','name','cpu_percent','memory_percent']),
                       key=lambda p: p.info.get('cpu_percent') or 0, reverse=True)
        safe(stdscr, r, cC+1, "─── TOP PROCESSOS (CPU) ─────────", curses.color_pair(C_DIM))
        r += 1
        for p in procs[:5]:
            if r >= SH-3: break
            try:
                nm = (p.info['name'] or '?')[:14]
                cp = p.info.get('cpu_percent') or 0
                mp = p.info.get('memory_percent') or 0
                pc_c = C_CRIT if cp>50 else (C_WARN if cp>20 else C_DIM)
                s = f"{p.info['pid']:6d} {nm:<14} CPU:{cp:5.1f}% MEM:{mp:4.1f}%"
                safe(stdscr, r, cC+1, s[:SW-cC-2], curses.color_pair(pc_c))
                r += 1
            except: pass
    except: pass

    col_C_end = r

    # ── DIVISORES VERTICAIS ───────────────────────────────────────────────────
    max_row = max(col_A_end, col_B_end, col_C_end)
    for ry in range(col_start, min(max_row, SH-2)):
        safe(stdscr, ry, cB-1, "║", curses.color_pair(C_BORDER)|curses.A_BOLD)
        safe(stdscr, ry, cC-1, "║", curses.color_pair(C_BORDER)|curses.A_BOLD)

    # ── LOG NA PARTE INFERIOR ─────────────────────────────────────────────────
    log_row = min(max_row, SH - 8)
    if log_row < SH - 4:
        hline(stdscr, log_row, 0, "═", SW, curses.color_pair(C_BORDER)|curses.A_BOLD)
        safe(stdscr, log_row, 2, "┤ LOG DE EVENTOS ├", curses.color_pair(C_HDR)|curses.A_BOLD)
        log_row += 1
        ev_list = list(events)[:SH - log_row - 2]
        for i, ev in enumerate(ev_list):
            if log_row + i >= SH - 2: break
            if "OUTAGE" in ev or "QUEDA" in ev:         ec = C_CRIT
            elif "OSCIL" in ev or "instável" in ev.lower(): ec = C_WARN
            elif "POWER" in ev or "RETORNOU" in ev:     ec = C_GOOD
            elif "CRIT" in ev:                           ec = C_CRIT
            elif "WARN" in ev:                           ec = C_WARN
            elif "START" in ev or "STOP" in ev:         ec = C_CYAN
            else:                                        ec = C_DIM
            safe(stdscr, log_row+i, 1, ev[:SW-2], curses.color_pair(ec))

    # ── RODAPÉ ────────────────────────────────────────────────────────────────
    hline(stdscr, SH-2, 0, "─", SW, curses.color_pair(C_DIM))
    beep_lbl = f"B:beep={'ON ' if beep_enabled else 'OFF'}"
    footer = (f"  Q:sair  {beep_lbl}  L:limpar-log  R:reset-stats  "
              f"  QUEDAS:{stats['total_outages']}  OSCIL:{stats['total_oscil']}  "
              f"AVISOS:{stats['total_warnings']}   log→{LOG_FILE}  ")
    safe(stdscr, SH-1, 0, footer[:SW-1], curses.color_pair(C_DIM))

def main(stdscr):
    global alert_flash, beep_enabled, running

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(700)
    curses.start_color()
    curses.use_default_colors()

    try:
        curses.init_color(20, 850, 450,   0)
        curses.init_color(21, 1000, 650, 100)
        curses.init_color(22, 300, 300, 300)
        AMB  = 20; ABRI = 21; DGRY = 22
    except Exception:
        AMB  = curses.COLOR_YELLOW
        ABRI = curses.COLOR_WHITE
        DGRY = 8

    curses.init_pair(C_AMBER,   AMB,                 -1)
    curses.init_pair(C_BRIGHT,  ABRI,                -1)
    curses.init_pair(C_DIM,     DGRY,                -1)
    curses.init_pair(C_GOOD,    curses.COLOR_GREEN,  -1)
    curses.init_pair(C_WARN,    curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CRIT,    curses.COLOR_RED,    -1)
    curses.init_pair(C_OSCIL,   curses.COLOR_MAGENTA,-1)
    curses.init_pair(C_BORDER,  AMB,                 -1)
    curses.init_pair(C_HDR,     curses.COLOR_BLACK,   AMB)
    curses.init_pair(C_CYAN,    curses.COLOR_CYAN,   -1)
    curses.init_pair(C_WHITE,   curses.COLOR_WHITE,  -1)
    curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA,-1)

    tick = 0
    while True:
        k = stdscr.getch()
        if k in (ord('q'), ord('Q'), 27): break
        if k in (ord('b'), ord('B')):
            beep_enabled = not beep_enabled
            log_event(f"Beep {'ativado' if beep_enabled else 'desativado'}")
        if k in (ord('l'), ord('L')):
            events.clear(); log_event("Log limpo")
        if k in (ord('r'), ord('R')):
            stats["total_outages"] = 0
            stats["total_oscil"]   = 0
            stats["total_warnings"]= 0
            stats["longest_outage"]= 0
            stats["session_start"] = time.time()
            log_event("Estatísticas resetadas", "INFO")

        info = get_power()
        draw(stdscr, info, tick)
        stdscr.refresh()
        tick += 1

def run():
    global running
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        log_event("GuardaTensão v3 encerrado", "STOP")
        print(f"\n✓ Encerrado. Log: {LOG_FILE}")

if __name__ == "__main__":
    try: import psutil
    except ImportError:
        print("Instale psutil:  pip install psutil  ou  sudo dnf install python3-psutil")
        sys.exit(1)
    run()
