"""Modem poller for /api/device/signal and /api/monitoring/status (huawei-lte-api)."""
import re
import time

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection

from common import CsvLog, now

FIELDS = [
    "t", "time", "ok", "error", "rat", "net_type", "net_type_ex", "endc", "nr_icon",
    "band", "lte_band", "nr_band", "pci", "scc_pci", "cell_id", "earfcn",
    "rsrp", "rsrq", "sinr", "lte_pusch", "lte_pucch", "lte_srs", "lte_ul_mcs", "ul_mcs",
    "nr_pusch", "nr_pucch", "nr_srs", "nr_ul_mcs", "nrulmcs", "nrsinr", "nrrsrp", "nrrsrq",
    "nrulfreq_khz", "nrbler", "nrrank", "dl_mbps", "ul_mbps", "connect_s", "txpower", "nrtxpower",
]

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def num(v):
    """'-97dBm' -> -97.0, '16dB' -> 16.0, None/'' -> ''"""
    if v is None:
        return ""
    m = _NUM.search(str(v))
    return float(m.group()) if m else ""


def txpower(v):
    """'PPusch:26dBm PPucch:22dBm PSrs:26dBm PPrach:20dBm' -> dict"""
    out = {}
    for k, val in re.findall(r"P(Pusch|Pucch|Srs|Prach):\s*(-?\d+)", v or "", re.I):
        out[k.lower()] = float(val)
    return out


def first_mcs(v):
    """'mcsUpCarrier1:29@Unknown mcsUpCarrier2:..' / 'NRmcsUpCarrier1:15@64QAM' -> 29.0"""
    m = re.search(r"Carrier1:\s*(\d+)", v or "")
    return float(m.group(1)) if m else ""


def parse(sig, st):
    """Flatten raw API dicts into one CSV row (without t/time)."""
    band = sig.get("band") or ""
    bands = re.findall(r"\(([BN]\d+)\)", band)
    lte = [b for b in bands if b.startswith("B")]
    nr = [b for b in bands if b.startswith("N")]
    ltp, ntp = txpower(sig.get("txpower")), txpower(sig.get("nrtxpower"))
    ntype_ex = (st or {}).get("CurrentNetworkTypeEx") or ""
    nr_active = bool(nr) or bool(ntp)
    if ntype_ex == "111" or (nr_active and not lte):
        rat = "SA"
    elif nr_active:
        rat = "NSA"
    else:
        rat = "LTE"
    return {
        "ok": 1, "error": "", "rat": rat,
        "net_type": (st or {}).get("CurrentNetworkType") or "",
        "net_type_ex": ntype_ex,
        "endc": (st or {}).get("EndcStatus") or "",
        "nr_icon": (st or {}).get("SignalIconNr") or "",
        "band": band, "lte_band": "+".join(lte), "nr_band": "+".join(nr),
        "pci": sig.get("pci") or "", "scc_pci": sig.get("scc_pci") or "",
        "cell_id": sig.get("cell_id") or "", "earfcn": sig.get("earfcn") or "",
        "rsrp": num(sig.get("rsrp")), "rsrq": num(sig.get("rsrq")), "sinr": num(sig.get("sinr")),
        "lte_pusch": ltp.get("pusch", ""), "lte_pucch": ltp.get("pucch", ""), "lte_srs": ltp.get("srs", ""),
        "lte_ul_mcs": first_mcs(sig.get("ul_mcs")), "ul_mcs": sig.get("ul_mcs") or "",
        "nr_pusch": ntp.get("pusch", ""), "nr_pucch": ntp.get("pucch", ""), "nr_srs": ntp.get("srs", ""),
        "nr_ul_mcs": first_mcs(sig.get("nrulmcs")), "nrulmcs": sig.get("nrulmcs") or "",
        "nrsinr": num(sig.get("nrsinr")), "nrrsrp": num(sig.get("nrrsrp")), "nrrsrq": num(sig.get("nrrsrq")),
        "nrulfreq_khz": num(sig.get("nrulfreq")), "nrbler": num(sig.get("nrbler")),
        "nrrank": sig.get("nrrank") or "",
        "txpower": sig.get("txpower") or "", "nrtxpower": sig.get("nrtxpower") or "",
    }


class Modem:
    """Lazily (re)connecting wrapper. Any failure drops the session; next call logs in again."""

    def __init__(self, cfg):
        m = cfg["modem"]
        self.url, self.user, self.pw = m["url"], m["username"], m["password"]
        self.timeout = float(m["timeout"])
        self.conn = None
        self.client = None
        self.logins = 0

    def _ensure(self):
        if self.client is None:
            if not self.pw:
                raise RuntimeError("no modem password (set HUAWEI_PASSWORD or config.local.ini [modem] password)")
            self.conn = Connection(self.url, username=self.user, password=self.pw, timeout=self.timeout)
            self.client = Client(self.conn)
            self.logins += 1
        return self.client

    def drop(self):
        try:
            if self.conn:
                self.conn.close()  # logs out; frees one of the modem's few session slots
        except Exception:
            pass
        self.conn = self.client = None

    def call(self, fn):
        """fn(client) -> result; one retry with a fresh login on failure."""
        for attempt in (0, 1):
            try:
                return fn(self._ensure())
            except Exception:
                self.drop()
                if attempt:
                    raise

    close = drop


def run_logger(cfg, session_dir, stopper, quiet=False):
    interval = float(cfg["modem"]["interval"])
    status_every = max(1, int(cfg["modem"]["status_every"]))
    log = CsvLog(session_dir / "modem.csv", FIELDS)
    modem = Modem(cfg)
    st, n, fails, backoff = {}, 0, 0, interval
    next_t = time.time()
    try:
        while not stopper.stopped():
            t, ts = now()
            try:
                sig = modem.call(lambda c: c.device.signal())
                if n % status_every == 0:
                    st = modem.call(lambda c: c.monitoring.status())
                row = parse(sig, st)
                try:  # whole-line throughput (all devices); the modem refreshes it about every 2 s
                    ts_ = modem.call(lambda c: c.monitoring.traffic_statistics())
                    row["dl_mbps"] = round(int(ts_["CurrentDownloadRate"]) * 8 / 1e6, 2)
                    row["ul_mbps"] = round(int(ts_["CurrentUploadRate"]) * 8 / 1e6, 2)
                    row["connect_s"] = int(ts_["CurrentConnectTime"])  # seconds since the data connection came up
                except Exception:
                    pass
                fails, backoff = 0, interval
                if not quiet:
                    print(f"[modem] {ts[11:23]} {row['rat']:<3} {row['band']:<36} "
                          f"LTE pusch/pucch={row['lte_pusch']}/{row['lte_pucch']} mcs={row['lte_ul_mcs']} | "
                          f"NR pusch/pucch={row['nr_pusch']}/{row['nr_pucch']} mcs={row['nr_ul_mcs']} "
                          f"sinr={row['nrsinr']} rsrp={row['nrrsrp']} pci={row['scc_pci']} | "
                          f"line {row.get('dl_mbps', '-')}/{row.get('ul_mbps', '-')} Mbit/s", flush=True)
            except Exception as e:
                fails += 1
                backoff = min(10.0, interval * 2 ** min(fails, 4))
                row = {"ok": 0, "error": f"{type(e).__name__}: {e}"[:200]}
                print(f"[modem] {ts[11:23]} poll failed ({fails}): {row['error']}", flush=True)
            row.update(t=f"{t:.3f}", time=ts)
            log.write(row)
            n += 1
            next_t = max(next_t + (interval if not fails else backoff), time.time())
            stopper.wait(next_t - time.time())
    finally:
        modem.close()
        log.close()
