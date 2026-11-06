"""Huawei desktop dashboard. Run directly or with `diag.py gui`."""
import argparse
import configparser
import csv
import json
import datetime as dt
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections import deque

from common import ROOT, load_config
from modem import FIELDS, Modem, parse
from dashboard_data import (MODE_LABELS, SessionStats, band_request, execute_control,
                            mode_request, read_hosts, read_network, selected_bands)

BG, PANEL, BORDER = "#101820", "#18232f", "#2a3949"
TEXT, MUTED, GREEN, BLUE, AMBER = "#edf3fa", "#9cacc0", "#68dfb2", "#75b9ff", "#ffc875"
HISTORY_RANGES = {"5 min": 300, "15 min": 900, "30 min": 1800, "1 hour": 3600}


def display(value, unit=""):
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        value = f"{value:.1f}"
    return f"{value}{(' ' + unit) if unit else ''}"


def uptime(seconds):
    if seconds in (None, ""):
        return "—"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return (f"{days}d " if days else "") + f"{hours}h {minutes:02d}m"


def sample(client):
    signal = client.call(lambda c: c.device.signal())
    status = client.call(lambda c: c.monitoring.status())
    row = parse(signal, status)
    traffic, warning = {}, ""
    try:
        traffic = client.call(lambda c: c.monitoring.traffic_statistics())
        row.update(dl_mbps=int(traffic["CurrentDownloadRate"]) * 8 / 1e6,
                   ul_mbps=int(traffic["CurrentUploadRate"]) * 8 / 1e6,
                   connect_s=int(traffic["CurrentConnectTime"]))
    except Exception as exc:
        warning = f"Traffic unavailable ({type(exc).__name__})"
    row.update(t=time.time(), time=dt.datetime.now().isoformat(timespec="seconds"))
    return row, {"Signal": signal, "Status": status, "Traffic": traffic}, warning


def demo_sample():
    resources = Path(getattr(sys, "_MEIPASS", ROOT))
    signal = __import__("json").loads((resources / "fixtures/signal_nsa_real.json").read_text())
    phase = time.monotonic()
    signal.update(sinr=f"{3 + 8 * math.sin(phase / 12):.1f}dB", nrsinr="22dB")
    row = parse(signal, {"CurrentNetworkTypeEx": "101", "EndcStatus": "1"})
    row.update(t=time.time(), time=dt.datetime.now().isoformat(timespec="seconds"),
               dl_mbps=40 + 35 * math.sin(phase / 9), ul_mbps=2.1, connect_s=8400)
    return row, {"Signal": signal}, "SIMULATED DATA · modem not contacted"


def poll_worker(messages, commands, stop, cfg, demo=False):
    client = Modem(cfg)
    interval, failures, next_aux = 1.0, 0, 0
    try:
        while not stop.is_set():
            while True:
                try:
                    action, value = commands.get_nowait()
                except queue.Empty:
                    break
                if action == "config":
                    client.close()
                    client = Modem(value)
                    failures, next_aux = 0, 0
                elif action == "reconnect":
                    client.drop()
                    next_aux = 0
                elif action == "interval":
                    interval = value
                elif action == "refresh_aux":
                    next_aux = 0
                elif action in ("mobile_reconnect", "set_mode", "set_bands"):
                    try:
                        if demo:
                            raise ValueError("Modem controls are disabled in demo mode.")
                        result = execute_control(client, action, value)
                        messages.put(("control_result", (True, result)))
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        messages.put(("control_result", (False, reason + ". Outcome may be unconfirmed; refresh before retrying. No automatic resubmission.")))
                    next_aux = 0
            started = time.monotonic()
            try:
                row, raw, warning = demo_sample() if demo else sample(client)
                messages.put(("sample", (row, raw, warning)))
                failures = 0
                if not demo and time.monotonic() >= next_aux:
                    for kind, reader in [("network", read_network), ("hosts", read_hosts)]:
                        try:
                            messages.put((kind, reader(client)))
                        except Exception as exc:
                            messages.put(("aux_error", (kind, type(exc).__name__)))
                    next_aux = time.monotonic() + 15
            except Exception as exc:
                failures += 1
                # Never show exception messages: HTTP errors can contain credential URLs.
                messages.put(("error", f"Poll failed ({type(exc).__name__}). Retrying automatically."))
            delay = min(15, interval * 2 ** min(failures, 4)) if failures else interval
            stop.wait(max(0, delay - (time.monotonic() - started)))
    finally:
        client.close()


class Graph(tk.Canvas):
    def __init__(self, parent, history, series, floor=None):
        super().__init__(parent, background=PANEL, highlightthickness=0, height=205)
        self.history, self.series, self.floor = history, series, floor
        self.window_s = 300
        self.plot = None
        self.hover_position = None
        self.bind("<Configure>", lambda _: self.redraw())
        self.bind("<Motion>", self.on_motion)
        self.bind("<Leave>", self.on_leave)

    def on_motion(self, event):
        self.hover_position = (event.x, event.y)
        self.draw_hover()

    def on_leave(self, _event=None):
        self.hover_position = None
        self.delete("hover")

    def draw_hover(self):
        self.delete("hover")
        if not self.plot or not self.hover_position:
            return
        rows, now, bounds, limits = self.plot
        left, right, top, bottom = bounds
        lo, hi = limits
        x, y = self.hover_position
        if not rows or not (left <= x <= right and top <= y <= bottom):
            return
        target = now - (right-x)/(right-left)*self.window_s
        row = min(rows, key=lambda r: abs(r["t"]-target))
        # Do not invent values in an outage or the empty part before startup.
        if abs(row["t"]-target) > 3:
            return
        sample_x = right-(now-row["t"])/self.window_s*(right-left)
        self.create_line(sample_x, top, sample_x, bottom, fill=MUTED, dash=(3, 3), tags="hover")
        lines = [dt.datetime.fromtimestamp(row["t"]).strftime("%Y-%m-%d %H:%M:%S") + "  (local)"]
        for key, name, color in self.series:
            value = row.get(key)
            label, _, unit = name.partition(" · ")
            lines.append(f"{label}: {display(value, unit)}")
            if value not in (None, ""):
                sample_y = bottom-(float(value)-lo)/(hi-lo)*(bottom-top)
                self.create_oval(sample_x-4, sample_y-4, sample_x+4, sample_y+4,
                                 fill=color, outline=TEXT, tags="hover")
        text = self.create_text(0, 0, text="\n".join(lines), fill=TEXT, anchor="nw",
                                font=("Segoe UI", 10), tags=("hover", "tooltip-text"))
        box = self.bbox(text)
        width, height = box[2]-box[0]+16, box[3]-box[1]+14
        bx = max(left, min(x+15, right-width))
        by = max(top, min(y+15, bottom-height))
        self.coords(text, bx+8, by+7)
        background = self.create_rectangle(bx, by, bx+width, by+height, fill=BG,
                                           outline=BORDER, tags="hover")
        self.tag_lower(background, text)

    def redraw(self):
        self.delete("all")
        self.plot = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 100:
            return
        left, right, top, bottom = 55, w - 18, 38, h - 30
        now = time.time()
        rows = [r for r in self.history if now - self.window_s <= r["t"] <= now]
        values = [float(r[k]) for r in rows for k, _, _ in self.series if r.get(k) not in (None, "")]
        lo, hi = (min(values), max(values)) if values else (0, 10)
        if self.floor is not None:
            lo = min(self.floor, lo)
        padding = max(2, (hi - lo) * .15)
        lo, hi = lo - padding, hi + padding
        for n in range(5):
            y = top + (bottom - top) * n / 4
            self.create_line(left, y, right, y, fill=BORDER)
            self.create_text(left - 9, y, text=f"{hi - (hi-lo)*n/4:.0f}", fill=MUTED, anchor="e", font=("Segoe UI", 9))
        minutes = self.window_s / 60
        for fraction, label in [(1, f"{minutes:g} min ago"), (.5, f"{minutes/2:g} min ago"), (0, "Now")]:
            x = right - fraction * (right - left)
            self.create_text(x, bottom + 17, text=label, fill=MUTED, font=("Segoe UI", 9))
        for i, (key, name, color) in enumerate(self.series):
            self.create_text(left + i * 190, 15, text="●  " + name, fill=color, anchor="w", font=("Segoe UI", 10))
            points, previous = [], None
            def draw():
                if len(points) >= 4:
                    self.create_line(*points, fill=color, width=2, tags="series")
                elif points:
                    x, y = points
                    self.create_oval(x-2, y-2, x+2, y+2, fill=color, outline="", tags="series")
            for row in rows:
                value = row.get(key)
                if value in (None, "") or (previous is not None and row["t"] - previous > 6):
                    draw()
                    points = []
                if value not in (None, ""):
                    points.extend((right - (now-row["t"])/self.window_s*(right-left),
                                   bottom - (float(value)-lo)/(hi-lo)*(bottom-top)))
                previous = row["t"]
            draw()
        if not values:
            self.create_text(w/2, h/2, text="Waiting for samples…", fill=MUTED)
        self.plot = (rows, now, (left, right, top, bottom), (lo, hi))
        self.draw_hover()


class Dashboard:
    def __init__(self, root, demo=False):
        self.root, self.demo, self.cfg = root, demo, load_config()
        self.history = deque()
        self.messages, self.commands, self.stop = queue.Queue(), queue.Queue(), threading.Event()
        self.latest, self.raw, self.last_success, self.connected = None, {}, 0, False
        self.record_file, self.writer = None, None
        self.previous_cell = None
        self.stats = SessionStats()
        self.network_state, self.network_time, self.control_busy = None, 0, False
        self.band_vars, self.mode_codes, self.editing_network = {}, {}, False
        self.host_signature = None
        root.title("Huawei · Radio Monitor" + (" [DEMO]" if demo else ""))
        root.geometry("1120x850")
        root.minsize(920, 740)
        root.configure(background=BG)
        self.style()
        outer = ttk.Frame(root, padding=22)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="HUAWEI  /  RADIO MONITOR", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Settings", command=self.settings).pack(side="right")
        ttk.Button(header, text="Reconnect API", command=lambda: self.commands.put(("reconnect", None))).pack(side="right", padx=8)
        self.connection = tk.StringVar(value="Connecting…")
        ttk.Label(outer, textvariable=self.connection, foreground=GREEN).pack(anchor="w", pady=(7, 14))

        summary = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        summary.pack(fill="x")
        self.rat, self.traffic, self.connection_age = tk.StringVar(value="—"), tk.StringVar(value="—"), tk.StringVar(value="—")
        for title, var in [("CONNECTION", self.rat), ("WHOLE-LINE TRAFFIC", self.traffic), ("DATA CONNECTION UPTIME", self.connection_age)]:
            f = ttk.Frame(summary, style="Panel.TFrame")
            f.pack(side="left", fill="x", expand=True)
            ttk.Label(f, text=title, style="MutedPanel.TLabel").pack(anchor="w")
            ttk.Label(f, textvariable=var, style="Value.TLabel").pack(anchor="w", pady=(5, 0))
        self.band = tk.StringVar(value="Waiting for modem…")
        ttk.Label(outer, textvariable=self.band, foreground=MUTED, wraplength=1020).pack(anchor="w", pady=10)

        cards = ttk.Frame(outer)
        cards.pack(fill="x")
        self.metrics = {}
        groups = [
            ("LTE ANCHOR", BLUE, [("pci", "PCI", ""), ("sinr", "SINR", "dB"), ("rsrp", "RSRP", "dBm"),
             ("rsrq", "RSRQ", "dB"), ("lte_pusch", "PUSCH", "dBm"), ("lte_pucch", "PUCCH", "dBm"), ("lte_ul_mcs", "UL MCS", "")]),
            ("5G / NR", GREEN, [("scc_pci", "PCI", ""), ("nrsinr", "SINR", "dB"), ("nrrsrp", "RSRP", "dBm"),
             ("nrrsrq", "RSRQ", "dB"), ("nr_pusch", "PUSCH", "dBm"), ("nr_pucch", "PUCCH", "dBm"), ("nr_ul_mcs", "UL MCS", "")])]
        for col, (title, color, fields) in enumerate(groups):
            cards.columnconfigure(col, weight=1)
            card = ttk.Frame(cards, style="Panel.TFrame", padding=14)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
            ttk.Label(card, text=title, foreground=color, style="Panel.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
            for i, (key, label, unit) in enumerate(fields):
                r, c = divmod(i, 4)
                card.columnconfigure(c, weight=1)
                ttk.Label(card, text=label, style="MutedPanel.TLabel").grid(row=r*2+1, column=c, sticky="w")
                widget = ttk.Label(card, text="—", style="Metric.TLabel")
                widget.grid(row=r*2+2, column=c, sticky="w", padx=(0, 12), pady=(2, 12))
                self.metrics[key] = (widget, unit)

        self.note = tk.StringVar(value="SINR is received signal quality. Transmit power alone does not diagnose jitter.")
        ttk.Label(outer, textvariable=self.note, foreground=AMBER, wraplength=1020).pack(anchor="w", pady=10)
        history_toolbar = ttk.Frame(outer)
        history_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(history_toolbar, text="History range", foreground=MUTED).pack(side="left", padx=(0, 8))
        self.history_range = ttk.Combobox(history_toolbar, values=list(HISTORY_RANGES), width=10, state="readonly")
        self.history_range.set("5 min")
        self.history_range.pack(side="left")
        self.history_range.bind("<<ComboboxSelected>>", self.set_history_range)
        ttk.Label(history_toolbar, text="Keeps the last hour while open · no history before startup", foreground=MUTED).pack(side="left", padx=12)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        self.graphs = []
        for title, series, floor in [
            ("Signal quality", [("sinr", "LTE SINR · dB", BLUE), ("nrsinr", "NR SINR · dB", GREEN)], 0),
            ("RSRP", [("rsrp", "LTE RSRP · dBm", BLUE), ("nrrsrp", "NR RSRP · dBm", GREEN)], None),
            ("Transmit power", [("lte_pusch", "LTE PUSCH · dBm", BLUE), ("nr_pusch", "NR PUSCH · dBm", GREEN), ("nr_pucch", "NR PUCCH · dBm", AMBER)], None),
            ("Traffic", [("dl_mbps", "Download · Mbps", BLUE), ("ul_mbps", "Upload · Mbps", GREEN)], 0)]:
            g = Graph(tabs, self.history, series, floor)
            tabs.add(g, text=title)
            self.graphs.append(g)
        self.add_controls_tab(tabs)
        self.add_devices_tab(tabs)
        self.add_stats_tab(tabs)
        raw_frame = ttk.Frame(tabs)
        tabs.add(raw_frame, text="All fields")
        self.tree = ttk.Treeview(raw_frame, columns=("value",), show="tree headings")
        self.tree.heading("#0", text="API / Field")
        self.tree.heading("value", text="Value")
        self.tree.column("#0", width=260)
        self.tree.column("value", width=650)
        scroll = ttk.Scrollbar(raw_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.events = tk.Listbox(outer, height=3, background=PANEL, foreground=MUTED, borderwidth=0,
                                 highlightthickness=0, font=("Segoe UI", 9), activestyle="none")
        self.events.pack(fill="x", pady=10)
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.record_button = ttk.Button(footer, text="● Record CSV", command=self.record)
        self.record_button.pack(side="left")
        ttk.Button(footer, text="Save snapshot", command=self.snapshot).pack(side="left", padx=8)
        ttk.Label(footer, text="Refresh", foreground=MUTED).pack(side="left", padx=(12, 5))
        self.interval = ttk.Combobox(footer, values=["1 second", "2 seconds", "5 seconds"], width=11, state="readonly")
        self.interval.current(0)
        self.interval.pack(side="left")
        self.interval.bind("<<ComboboxSelected>>", lambda _: self.commands.put(("interval", float(self.interval.get().split()[0]))))
        self.footer = tk.StringVar(value="Monitoring · automatic session recovery")
        ttk.Label(footer, textvariable=self.footer, foreground=MUTED).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.thread = threading.Thread(target=poll_worker, args=(self.messages, self.commands, self.stop, self.cfg, demo), daemon=True)
        self.thread.start()
        root.after(100, self.tick)

    def style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TButton", background=BORDER, padding=(12, 7), borderwidth=0)
        s.map("TButton", background=[("active", "#35495d")])
        s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=6)
        s.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT, arrowcolor=TEXT)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Panel.TLabel", background=PANEL)
        s.configure("MutedPanel.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Value.TLabel", background=PANEL, font=("Segoe UI Semibold", 17))
        s.configure("Metric.TLabel", background=PANEL, font=("Segoe UI Semibold", 15))
        s.configure("Title.TLabel", font=("Segoe UI Semibold", 20))
        s.configure("TNotebook", borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL, padding=(15, 9))
        s.map("TNotebook.Tab", background=[("selected", BORDER)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, rowheight=25)
        s.configure("Treeview.Heading", background=BORDER)

    def event(self, text):
        self.events.insert(0, dt.datetime.now().strftime("%H:%M:%S") + "   " + text)
        if self.events.size() > 100:
            self.events.delete(100, "end")

    def set_history_range(self, _event=None):
        seconds = HISTORY_RANGES[self.history_range.get()]
        for graph in self.graphs:
            graph.window_s = seconds
            if graph.winfo_viewable():
                graph.redraw()

    def add_controls_tab(self, tabs):
        page = ttk.Frame(tabs, padding=16)
        tabs.add(page, text="Network controls")
        self.network_summary = tk.StringVar(value="Reading current restrictions…" if not self.demo else "Demo mode · modem controls disabled")
        ttk.Label(page, textvariable=self.network_summary, wraplength=1000).pack(anchor="w")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=10)
        ttk.Button(toolbar, text="Refresh settings", command=lambda: self.commands.put(("refresh_aux", None))).pack(side="left")
        self.mobile_button = ttk.Button(toolbar, text="Reconnect mobile network…", command=self.mobile_reconnect)
        self.mobile_button.pack(side="left", padx=8)
        self.mobile_button.state(["disabled"])
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="Network mode").pack(side="left", padx=(0, 12))
        self.mode_choice = ttk.Combobox(row, state="readonly", width=28)
        self.mode_choice.pack(side="left")
        self.mode_choice.bind("<<ComboboxSelected>>", lambda _: self.mark_network_edit())
        self.mode_button = ttk.Button(row, text="Preview mode change…", command=self.preview_mode)
        self.mode_button.pack(side="left", padx=8)
        self.mode_button.state(["disabled"])
        ttk.Label(page, text="Allowed LTE bands  ·  NR restrictions are preserved", foreground=MUTED).pack(anchor="w")
        self.bands_frame = ttk.Frame(page)
        self.bands_frame.pack(fill="x", pady=6)
        actions = ttk.Frame(page)
        actions.pack(fill="x")
        self.bands_button = ttk.Button(actions, text="Preview LTE band lock…", command=self.preview_bands)
        self.bands_button.pack(side="left")
        self.unlock_button = ttk.Button(actions, text="Remove LTE restrictions…", command=lambda: self.preview_bands(True))
        self.unlock_button.pack(side="left", padx=8)
        self.bands_button.state(["disabled"])
        self.unlock_button.state(["disabled"])
        ttk.Label(page, text="Applying a mode or band restriction can interrupt internet access. A band lock replaces LTE frequency/PCI restrictions; it does not force a specific cell.",
                  foreground=AMBER, wraplength=1000).pack(anchor="w", pady=10)

    def add_devices_tab(self, tabs):
        page = ttk.Frame(tabs, padding=12)
        tabs.add(page, text="Devices")
        self.host_status = tk.StringVar(value="Waiting for connected-device list…")
        ttk.Label(page, textvariable=self.host_status, foreground=MUTED).pack(anchor="w", pady=(0, 6))
        self.hosts_tree = ttk.Treeview(page, columns=("name", "ip", "mac", "link", "active", "speed"), show="headings", height=5)
        for key, label, width in [("name", "Device", 150), ("ip", "IP address", 260), ("mac", "MAC", 150),
                                  ("link", "Connection", 100), ("active", "Active", 60), ("speed", "Link Mbps", 85)]:
            self.hosts_tree.heading(key, text=label)
            self.hosts_tree.column(key, width=width, minwidth=50)
        scroll = ttk.Scrollbar(page, orient="vertical", command=self.hosts_tree.yview)
        scroll.pack(side="right", fill="y")
        self.hosts_tree.configure(yscrollcommand=scroll.set)
        self.hosts_tree.pack(fill="both", expand=True)
        ttk.Label(page, text="Refreshed every 15 seconds. Devices behind OpenWrt usually appear as the router; this API cannot show its individual LAN clients.",
                  foreground=MUTED, wraplength=1000).pack(anchor="w", pady=6)

    def add_stats_tab(self, tabs):
        page = ttk.Frame(tabs, padding=16)
        tabs.add(page, text="Session stats")
        self.stats_info = tk.StringVar(value="Waiting for samples…")
        ttk.Label(page, textvariable=self.stats_info, foreground=MUTED).pack(anchor="w")
        self.stats_tree = ttk.Treeview(page, columns=("min", "avg", "max", "negative", "share"), show="tree headings", height=2)
        self.stats_tree.heading("#0", text="Received SINR")
        self.stats_tree.column("#0", width=170)
        for key, label in [("min", "Min dB"), ("avg", "Mean dB"), ("max", "Max dB"), ("negative", "Time < 0 dB"), ("share", "% observed time")]:
            self.stats_tree.heading(key, text=label)
            self.stats_tree.column(key, width=130)
        self.stats_tree.pack(fill="both", expand=True, pady=10)
        for key, label in [("sinr", "LTE"), ("nrsinr", "NR")]:
            self.stats_tree.insert("", "end", iid=key, text=label)
        ttk.Button(page, text="Reset session stats", command=self.reset_stats).pack(anchor="w")

    def mark_network_edit(self):
        self.editing_network = True

    def show_network(self, state):
        changed = self.network_state is not None and state["current"] != self.network_state["current"]
        self.network_state, self.network_time = state, time.time()
        current, lock = state["current"], state.get("lock") or {}
        self.mode_codes = {MODE_LABELS.get(c, "Mode " + c): c for c in state["modes"]}
        self.mode_choice.configure(values=list(self.mode_codes))
        label = MODE_LABELS.get(current.get("NetworkMode"), "Mode " + str(current.get("NetworkMode")))
        lte, nr = lock.get("lte_info") or {}, lock.get("nr_info") or {}
        lock_names = {"0": "unrestricted", "1": "frequency", "2": "frequency + PCI", "3": "bands"}
        self.network_summary.set(f"Current: {label}  ·  LTE band mask: {current.get('LTEBand', '—')}\n"
                                 f"LTE restriction: {lock_names.get(lte.get('lock_mode'), 'unavailable')} / bands: {lte.get('all_bands') or 'automatic'}  ·  "
                                 f"NR restriction: {lock_names.get(nr.get('lock_mode'), 'unavailable')}  ·  updated {time.strftime('%H:%M:%S')}")
        if set(self.band_vars) != set(state["bands"]):
            for widget in self.bands_frame.winfo_children():
                widget.destroy()
            self.band_vars = {}
            for i, b in enumerate(state["bands"]):
                var = tk.BooleanVar()
                self.band_vars[b] = var
                ttk.Checkbutton(self.bands_frame, text=f"B{b}", variable=var, command=self.mark_network_edit).grid(row=i//9, column=i%9, sticky="w", padx=(0, 14))
            self.editing_network = False
        if not self.editing_network:
            self.mode_choice.set(label)
            selected = selected_bands(state)
            for b, var in self.band_vars.items():
                var.set(b in selected)
        if changed:
            self.event("Network restrictions changed; current settings refreshed")

    def show_hosts(self, hosts):
        signature = json.dumps(hosts, sort_keys=True)
        self.host_status.set(f"{sum(h.get('Active') == '1' for h in hosts)} active / {len(hosts)} known devices · updated {time.strftime('%H:%M:%S')}")
        if signature == self.host_signature:
            return
        self.host_signature = signature
        for iid in self.hosts_tree.get_children():
            self.hosts_tree.delete(iid)
        for h in hosts:
            self.hosts_tree.insert("", "end", values=(h.get("ActualName") or h.get("HostName") or "Unknown",
                h.get("IpAddress") or "—", h.get("MacAddress") or "—", h.get("InterfaceType") or "—",
                "Yes" if h.get("Active") == "1" else "No", h.get("EthernetSpeed") or "—"))

    def update_stats(self):
        s = self.stats
        self.stats_info.set(f"Since reset: {s.samples} samples · LTE anchor changes: {s.cell_changes} · NR additions: {s.nr_adds} / releases: {s.nr_releases}\n"
                            "Time below zero is estimated between successful samples; gaps longer than 15 seconds are excluded.")
        for key, v in s.values.items():
            if v["n"]:
                share = f"{100*v['negative_s']/v['observed_s']:.1f}%" if v["observed_s"] else "—"
                self.stats_tree.item(key, values=(f"{v['min']:.1f}", f"{v['sum']/v['n']:.1f}", f"{v['max']:.1f}", f"{v['negative_s']:.0f} s", share))
            else:
                self.stats_tree.item(key, values=("—",)*5)

    def reset_stats(self):
        self.stats = SessionStats()
        self.update_stats()

    def controls_ready(self):
        return not self.demo and self.connected and not self.control_busy and time.time()-self.last_success < 6

    def mobile_reconnect(self):
        if not self.controls_ready():
            return
        if messagebox.askyesno("Reconnect mobile network?", "This sends a mobile-network reconnect request and can interrupt internet for everyone. It does not guarantee a different cell.\n\nSend once now?", parent=self.root):
            self.control_busy = True
            self.event("Sending mobile reconnect request…")
            self.commands.put(("mobile_reconnect", None))

    def preview_mode(self):
        if self.network_state:
            self.preview_request("set_mode", lambda: mode_request(self.network_state, self.mode_codes.get(self.mode_choice.get())))

    def preview_bands(self, unrestricted=False):
        if self.network_state:
            self.preview_request("set_bands", lambda: band_request(self.network_state, [b for b, v in self.band_vars.items() if v.get()], unrestricted))

    def preview_request(self, action, builder):
        if not self.controls_ready() or time.time()-self.network_time > 30:
            messagebox.showinfo("Refresh needed", "Wait for a fresh connection and settings, then preview again.", parent=self.root)
            self.commands.put(("refresh_aux", None))
            return
        try:
            expected = self.network_state
            endpoint, body = builder()
        except ValueError as exc:
            messagebox.showerror("Cannot apply", str(exc), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Preview modem change")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="This may interrupt internet access for everyone.", foreground=AMBER).pack(anchor="w", pady=(0, 10))
        area = ttk.Frame(frame)
        area.pack(fill="both", expand=True)
        text = tk.Text(area, width=76, height=20, background=PANEL, foreground=TEXT, wrap="none", font=("Consolas", 10))
        scroll = ttk.Scrollbar(area, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", f"POST /api/{endpoint}\n\n{json.dumps(body, indent=2)}")
        text.configure(state="disabled")
        confirmed = []
        def apply():
            confirmed.append(True)
            dialog.destroy()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply once", command=apply).pack(side="right", padx=8)
        self.root.wait_window(dialog)
        if confirmed and self.controls_ready():
            self.control_busy = True
            self.commands.put((action, (expected, endpoint, body)))

    def tick(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "error":
                    if self.connected:
                        self.event(value)
                    self.connected = False
                    self.connection.set(value)
                    self.note.set("Displayed values are stale. Check modem address/password in Settings if this persists.")
                elif kind == "sample":
                    self.update(*value)
                elif kind == "network":
                    self.show_network(value)
                elif kind == "hosts":
                    self.show_hosts(value)
                elif kind == "aux_error":
                    name, reason = value
                    if name == "network":
                        self.network_time = 0
                        self.network_summary.set(f"Settings unavailable ({reason}); controls disabled until refreshed.")
                    else:
                        self.host_status.set(f"Device refresh failed ({reason}); listed devices may be stale.")
                elif kind == "control_result":
                    ok, text = value
                    self.control_busy = False
                    self.editing_network = False
                    self.network_time = 0
                    self.event(text)
                    messagebox.showinfo("Modem control result" if ok else "Modem control not confirmed", text, parent=self.root)
        except queue.Empty:
            pass
        if self.last_success and time.time() - self.last_success > 6:
            self.footer.set(f"Stale · last sample {int(time.time()-self.last_success)}s ago")
        ready = self.controls_ready()
        self.mobile_button.state(["!disabled"] if ready else ["disabled"])
        fresh = ready and self.network_state is not None and time.time()-self.network_time <= 30
        self.mode_button.state(["!disabled"] if fresh else ["disabled"])
        bands_ready = fresh and bool((self.network_state.get("lock") or {}).get("lte_info")) and self.network_state["current"].get("NetworkMode") != "08"
        for button in (self.bands_button, self.unlock_button):
            button.state(["!disabled"] if bands_ready else ["disabled"])
        for graph in self.graphs:
            if graph.winfo_viewable():
                graph.redraw()
        self.root.after(500, self.tick)

    def update(self, row, raw, warning):
        if not self.connected:
            self.event("Connected" if not self.demo else "Demo mode · simulated readings")
        self.connected, self.last_success = True, row["t"]
        self.latest, self.raw = row, raw
        self.history.append(row)
        while self.history and self.history[0]["t"] < row["t"] - 3600:
            self.history.popleft()
        self.stats.add(row)
        self.update_stats()
        identity = (row["rat"], row["lte_band"], row["pci"], row["nr_band"], row["scc_pci"], row["cell_id"], row["earfcn"])
        if identity != self.previous_cell:
            self.event(f"{row['rat']} · LTE {row['lte_band']} / PCI {row['pci']} · NR {row['nr_band'] or 'absent'} / PCI {row['scc_pci'] or '—'}")
            self.previous_cell = identity
        self.connection.set(("DEMO · " if self.demo else "Connected · ") + self.cfg["modem"]["url"] + " · " + row["time"].split("T")[-1])
        self.rat.set(row["rat"] + "  ·  " + row["lte_band"] + (" + " + row["nr_band"] if row["nr_band"] else ""))
        self.traffic.set(f"↓ {display(row.get('dl_mbps'))}   ↑ {display(row.get('ul_mbps'))} Mbps")
        self.connection_age.set(uptime(row.get("connect_s")))
        self.band.set(row["band"] + f"  ·  LTE EARFCN {row['earfcn']}  ·  Cell {row['cell_id']}")
        for key, (widget, unit) in self.metrics.items():
            widget.configure(text=display(row.get(key), unit), foreground=TEXT)
            if key in ("sinr", "nrsinr") and row.get(key) not in (None, ""):
                widget.configure(foreground=AMBER if row[key] < 10 else GREEN)
        value = row.get("sinr")
        note = "SINR is received signal quality. Transmit power alone does not diagnose jitter."
        if value not in (None, "") and value < 0:
            note = "LTE SINR below 0 dB: interference + noise exceed the wanted signal. This can affect performance."
        elif value not in (None, "") and value < 10:
            note = "LTE SINR is low. Compare trends and game timing; a single reading does not prove the cause."
        self.note.set(warning or note)
        self.footer.set("Recording CSV" if self.writer else "Monitoring · automatic session recovery")
        for group, fields in raw.items():
            if not self.tree.exists(group):
                self.tree.insert("", "end", iid=group, text=group, open=True)
            for key, value in fields.items():
                iid = group + "/" + key
                if self.tree.exists(iid):
                    self.tree.item(iid, values=(str(value) if value is not None else "—",))
                else:
                    self.tree.insert(group, "end", iid=iid, text=key, values=(str(value) if value is not None else "—",))
        if self.writer:
            try:
                self.writer.writerow({k: row.get(k, "") for k in FIELDS})
                self.record_file.flush()
            except OSError as exc:
                self.end_record()
                messagebox.showerror("Recording stopped", str(exc), parent=self.root)

    def record(self):
        if self.writer:
            self.end_record()
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title="Record modem samples", defaultextension=".csv",
                    initialfile=dt.datetime.now().strftime("modem-%Y%m%d-%H%M%S.csv"), filetypes=[("CSV", "*.csv")])
        if filename:
            try:
                self.record_file = open(filename, "w", newline="", encoding="utf-8")
                self.writer = csv.DictWriter(self.record_file, fieldnames=FIELDS)
                self.writer.writeheader()
                self.record_button.configure(text="■ Stop recording")
                self.event("CSV recording started")
            except OSError as exc:
                self.end_record()
                messagebox.showerror("Cannot record", str(exc), parent=self.root)

    def end_record(self):
        if self.record_file:
            self.record_file.close()
        self.record_file = self.writer = None
        self.record_button.configure(text="● Record CSV")

    def snapshot(self):
        if not self.latest:
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title="Save current readings", defaultextension=".csv",
                    initialfile="modem-snapshot.csv", filetypes=[("CSV", "*.csv")])
        if filename:
            try:
                with open(filename, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerow({k: self.latest.get(k, "") for k in FIELDS})
            except OSError as exc:
                messagebox.showerror("Cannot save", str(exc), parent=self.root)

    def settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Modem connection")
        dialog.configure(background=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        entries = {}
        for i, (key, label) in enumerate([("url", "Modem URL"), ("username", "Username"), ("password", "Password")]):
            ttk.Label(frame, text=label).grid(row=i*2, column=0, sticky="w", pady=(8, 4))
            var = tk.StringVar(value=self.cfg["modem"][key])
            ttk.Entry(frame, textvariable=var, width=45, show="•" if key == "password" else "").grid(row=i*2+1, column=0)
            entries[key] = var
        remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Save credentials locally (plain text, git-ignored)", variable=remember).grid(row=6, column=0, sticky="w", pady=14)
        def connect():
            from urllib.parse import urlsplit
            url = entries["url"].get().strip()
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                messagebox.showerror("Invalid URL", "Use http://192.168.100.1/ without credentials in the URL.", parent=dialog)
                return
            if not entries["username"].get().strip() or not entries["password"].get():
                messagebox.showerror("Missing credentials", "Enter a username and password.", parent=dialog)
                return
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read_dict({section: dict(self.cfg[section]) for section in self.cfg.sections()})
            for key, var in entries.items():
                cfg["modem"][key] = var.get().strip() if key != "password" else var.get()
            if remember.get():
                local = configparser.ConfigParser(interpolation=None)
                target = ROOT / "config.local.ini"
                local.read(target, encoding="utf-8")
                if not local.has_section("modem"):
                    local.add_section("modem")
                for key in entries:
                    local["modem"][key] = cfg["modem"][key]
                try:
                    temporary = target.with_suffix(".ini.tmp")
                    with temporary.open("w", encoding="utf-8") as f:
                        local.write(f)
                    os.replace(temporary, target)
                except OSError as exc:
                    messagebox.showerror("Cannot save settings", str(exc), parent=dialog)
                    return
            self.cfg = cfg
            self.network_state, self.network_time, self.connected = None, 0, False
            self.editing_network = False
            self.commands.put(("config", cfg))
            self.connection.set("Connecting…")
            dialog.destroy()
        ttk.Button(frame, text="Connect", command=connect).grid(row=7, column=0, sticky="e")

    def close(self):
        self.stop.set()
        self.end_record()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="show simulated data without contacting the modem")
    parser.add_argument("--smoke-test", action="store_true", help="exit after a short GUI startup test")
    args = parser.parse_args(argv)
    root = tk.Tk()
    app = Dashboard(root, args.demo)
    if args.smoke_test:
        root.after(3500, app.close)
    root.mainloop()
    if args.smoke_test and app.latest is None:
        raise SystemExit(1)
    if args.smoke_test and not args.demo and (app.network_state is None or app.host_signature is None):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
