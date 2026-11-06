"""Dashboard statistics and explicit, one-shot modem controls."""
from copy import deepcopy
import math


MODE_LABELS = {"00": "Automatic (LTE + 5G)", "03": "LTE only", "08": "5G only"}


def as_list(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def read_network(client):
    current = client.call(lambda c: c.net.net_mode())
    allowed = client.call(lambda c: c.net.net_mode_list())
    access = allowed.get("AccessList", {}).get("Access", [])
    modes = access if isinstance(access, list) else [access]
    mask = 0
    for entry in as_list(allowed.get("LTEBandList", {}).get("LTEBand")):
        if "all" not in entry.get("Name", "").lower():
            mask |= int(entry["Value"], 16)
    bands = [b for b in range(1, 65) if mask & (1 << (b-1))]
    lock, lock_error = None, ""
    try:
        lock = client.call(lambda c: c.net._session.get("net/lock-freq"))
    except Exception as exc:
        lock_error = type(exc).__name__
    return {"current": current, "modes": modes, "bands": bands, "lock": lock, "lock_error": lock_error}


def read_hosts(client):
    response = client.call(lambda c: c.lan.host_info())
    return as_list((response.get("Hosts") or {}).get("Host"))


def selected_bands(state):
    lte = (state.get("lock") or {}).get("lte_info") or {}
    if lte.get("lock_mode") in ("1", "2", "3") and lte.get("all_bands"):
        return {int(b) for b in lte["all_bands"].split(",") if b.strip().isdigit()}
    value = state["current"].get("LTEBand", "")
    if value.upper() == "ALL":
        return set(state["bands"])
    mask = int(value, 16) if value else 0
    return {b for b in state["bands"] if mask & (1 << (b-1))}


def mode_request(state, code):
    if code not in state["modes"]:
        raise ValueError("The modem does not advertise that mode.")
    body = deepcopy(state["current"])
    if any(isinstance(v, (dict, list)) for v in body.values()):
        raise ValueError("Unrecognised network-mode format; request not sent.")
    body["NetworkMode"] = code
    return "net/net-mode", body


def band_request(state, bands, unrestricted=False):
    lock = state.get("lock")
    if not lock or not isinstance(lock.get("lte_info"), dict):
        raise ValueError("This firmware's LTE band-lock API is unavailable.")
    if state["current"].get("NetworkMode") == "08":
        raise ValueError("LTE band restrictions cannot be changed in 5G-only mode.")
    bands = sorted(set(bands))
    if not unrestricted and (not bands or not set(bands) <= set(state["bands"])):
        raise ValueError("Select at least one advertised LTE band.")
    # This is the H165 lockband.js mode-3 format, not a guessed LTEBand bit mask.
    lte = {"lock_mode": "0", "freq_infos": {}, "all_bands": ""} if unrestricted else {
        "lock_mode": "3", "freq_infos": {"freq_info": [{"band": str(b)} for b in bands]},
        "all_bands": ",".join(map(str, bands))}
    body = {"lte_info": lte}
    if state["current"].get("NetworkMode") != "03" and isinstance(lock.get("nr_info"), dict):
        # Preserve any existing NR restrictions when updating LTE, as the web UI does.
        nr = deepcopy(lock["nr_info"])
        nr["freq_infos"] = nr.get("freq_infos") or {}
        nr["all_bands"] = nr.get("all_bands") or ""
        body["nr_info"] = nr
    return "net/lock-freq", body


def restriction_signature(info):
    if not info:
        return None
    mode = info.get("lock_mode")
    if mode == "0":
        return (mode,)
    frequencies = as_list((info.get("freq_infos") or {}).get("freq_info"))
    return (mode, tuple(sorted((info.get("all_bands") or "").split(","))),
            tuple(sorted(tuple(str(f.get(k) or "") for k in ("band", "freq", "pci")) for f in frequencies)))


def execute_control(client, action, value):
    """Read operations may retry. Never wrap a disruptive write in Modem.call()."""
    if action == "mobile_reconnect":
        client._ensure().net.reconnect()
        return "Mobile-network reconnect request accepted; waiting for the connection."
    expected, endpoint, body = value
    before = read_network(client)
    if before["current"] != expected["current"] or before["lock"] != expected["lock"]:
        raise ValueError("Modem settings changed since the preview. Refresh and preview again.")
    # Rebuild and compare so malformed or stale UI messages cannot send arbitrary requests.
    if action == "set_mode":
        verified = mode_request(before, body.get("NetworkMode"))
    elif action == "set_bands":
        lte = body.get("lte_info", {})
        bands = [int(b) for b in lte.get("all_bands", "").split(",") if b]
        verified = band_request(before, bands, lte.get("lock_mode") == "0")
    else:
        raise ValueError("Unknown control action.")
    if verified != (endpoint, body):
        raise ValueError("Request did not match the preview; nothing was sent.")
    client._ensure().net._session.post_set(endpoint, body)
    after = read_network(client)
    if action == "set_mode":
        matched = after["current"].get("NetworkMode") == body["NetworkMode"]
    else:
        actual = (after.get("lock") or {}).get("lte_info") or {}
        wanted = body["lte_info"]
        matched = actual.get("lock_mode") == wanted["lock_mode"]
        if wanted["lock_mode"] != "0":
            matched = matched and set(actual.get("all_bands", "").split(",")) == set(wanted["all_bands"].split(","))
        if "nr_info" in body:
            matched = matched and restriction_signature((after.get("lock") or {}).get("nr_info")) == restriction_signature(body["nr_info"])
    return "Settings verified by read-back." if matched else "Request accepted, but read-back differs. Refresh before trying again."


class SessionStats:
    def __init__(self):
        self.values = {k: {"n": 0, "sum": 0., "min": math.inf, "max": -math.inf,
                           "observed_s": 0., "negative_s": 0.} for k in ("sinr", "nrsinr")}
        self.previous = None
        self.samples = self.cell_changes = self.nr_adds = self.nr_releases = 0

    def add(self, row):
        self.samples += 1
        previous = self.previous
        delta = row["t"] - previous["t"] if previous else 0
        for key, stats in self.values.items():
            value = row.get(key)
            if value not in (None, ""):
                stats["n"] += 1
                stats["sum"] += float(value)
                stats["min"] = min(stats["min"], float(value))
                stats["max"] = max(stats["max"], float(value))
            if previous and 0 < delta <= 15 and previous.get(key) not in (None, ""):
                stats["observed_s"] += delta
                stats["negative_s"] += delta if float(previous[key]) < 0 else 0
        if previous:
            def anchor(r):
                return r.get("cell_id"), r.get("pci"), r.get("earfcn")
            self.cell_changes += anchor(previous) != anchor(row)
            self.nr_adds += not bool(previous.get("nr_band")) and bool(row.get("nr_band"))
            self.nr_releases += bool(previous.get("nr_band")) and not bool(row.get("nr_band"))
        self.previous = row.copy()
