import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import discord


class RailwayCostMonitor:
    MINUTES_IN_MONTH = 43200.0
    PRICE_CPU = 20.0
    PRICE_RAM = 10.0
    PRICE_EGRESS = 0.05
    PRICE_VOLUME = 0.15
    PRICE_BACKUP = 0.15

    _active = None
    _installed = False
    _orig_request = None
    _orig_send_str = None
    _orig_send_bytes = None

    def __init__(self, state_file="/app/data/railway_cost_meter.json", volume_path="/app/data", interval=10):
        self.state_file = Path(state_file)
        self.volume_path = Path(volume_path)
        self.interval = max(2, int(interval))
        self._task = None
        self._lock = asyncio.Lock()
        self._last = None
        self._last_save = 0.0
        self._last_history = 0.0
        self._official_cache = None
        self._official_cache_at = 0.0
        self.latest = {"cpu": 0.0, "memory": 0, "volume": 0, "tx_bps": 0.0, "rx_bps": 0.0}
        self.state = self._load()
        RailwayCostMonitor._active = self

    @staticmethod
    def _blank():
        return {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "cpu_seconds": 0.0,
                "memory_gb_minutes": 0.0,
                "network_tx_bytes": 0,
                "network_rx_bytes": 0,
                "volume_gb_minutes": 0.0,
            },
            "network": {},
            "daily": {},
            "history": [],
            "restarts": 0,
            "last_saved_at": None,
        }

    @staticmethod
    def _empty_usage():
        return {
            "cpu_seconds": 0.0,
            "memory_gb_minutes": 0.0,
            "network_tx_bytes": 0,
            "network_rx_bytes": 0,
            "volume_gb_minutes": 0.0,
        }

    def _load(self):
        state = self._blank()
        try:
            if self.state_file.exists():
                old = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(old, dict):
                    state.update(old)
                    state.setdefault("totals", self._blank()["totals"])
                    state.setdefault("network", {})
                    state.setdefault("daily", {})
                    state.setdefault("history", [])
                    state["restarts"] = int(state.get("restarts", 0)) + 1
        except Exception as e:
            print(f"RAILWAY_METER load error: {e}")
        return state

    def _save_sync(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state["last_saved_at"] = datetime.now(timezone.utc).isoformat()
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            print(f"RAILWAY_METER save error: {e}")

    @staticmethod
    def _read_int(paths):
        for p in paths:
            try:
                return int(Path(p).read_text().strip())
            except Exception:
                pass
        return None

    def _cpu_seconds(self):
        try:
            for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec "):
                    return int(line.split()[1]) / 1000000.0
        except Exception:
            pass
        ns = self._read_int([
            "/sys/fs/cgroup/cpuacct/cpuacct.usage",
            "/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage",
        ])
        if ns is not None:
            return ns / 1000000000.0
        return time.process_time()

    def _memory_bytes(self):
        value = self._read_int([
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ])
        return max(0, value or 0)

    @staticmethod
    def _network_bytes():
        rx = 0
        tx = 0
        try:
            lines = Path("/proc/net/dev").read_text().splitlines()[2:]
            for line in lines:
                if ":" not in line:
                    continue
                iface, payload = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                fields = payload.split()
                if len(fields) >= 9:
                    rx += int(fields[0])
                    tx += int(fields[8])
        except Exception:
            pass
        return rx, tx

    def _volume_bytes(self):
        root = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or str(self.volume_path))
        if not root.exists():
            root = self.volume_path
        total = 0
        try:
            for base, _, files in os.walk(root):
                for name in files:
                    try:
                        st = os.stat(os.path.join(base, name), follow_symlinks=False)
                        blocks = int(getattr(st, "st_blocks", 0)) * 512
                        total += blocks if blocks else int(st.st_size)
                    except Exception:
                        pass
        except Exception:
            pass
        return total

    def _reading(self):
        rx, tx = self._network_bytes()
        return {
            "mono": time.monotonic(),
            "wall": datetime.now(timezone.utc),
            "cpu": self._cpu_seconds(),
            "memory": self._memory_bytes(),
            "volume": self._volume_bytes(),
            "rx": rx,
            "tx": tx,
        }

    def _apply(self, a, b):
        dt = b["mono"] - a["mono"]
        if dt <= 0 or dt > 300:
            return
        cpu = max(0.0, b["cpu"] - a["cpu"])
        tx = max(0, b["tx"] - a["tx"])
        rx = max(0, b["rx"] - a["rx"])
        gib = float(1024 ** 3)
        mem_min = ((a["memory"] + b["memory"]) / 2.0 / gib) * dt / 60.0
        vol_min = ((a["volume"] + b["volume"]) / 2.0 / gib) * dt / 60.0

        for bucket in (
            self.state["totals"],
            self.state["daily"].setdefault(b["wall"].strftime("%Y-%m-%d"), self._empty_usage()),
        ):
            bucket["cpu_seconds"] = float(bucket.get("cpu_seconds", 0)) + cpu
            bucket["memory_gb_minutes"] = float(bucket.get("memory_gb_minutes", 0)) + mem_min
            bucket["network_tx_bytes"] = int(bucket.get("network_tx_bytes", 0)) + tx
            bucket["network_rx_bytes"] = int(bucket.get("network_rx_bytes", 0)) + rx
            bucket["volume_gb_minutes"] = float(bucket.get("volume_gb_minutes", 0)) + vol_min

        self.latest = {
            "cpu": cpu / dt,
            "memory": b["memory"],
            "volume": b["volume"],
            "tx_bps": tx / dt,
            "rx_bps": rx / dt,
        }

        if len(self.state["daily"]) > 120:
            for day in sorted(self.state["daily"])[:-120]:
                self.state["daily"].pop(day, None)

    async def sample_once(self):
        now = self._reading()
        async with self._lock:
            if self._last is not None:
                self._apply(self._last, now)
            self._last = now

            if now["mono"] - self._last_history >= 300:
                self._last_history = now["mono"]
                self.state["history"].append({
                    "ts": int(now["wall"].timestamp()),
                    "cpu": round(self.latest["cpu"], 6),
                    "memory_mb": round(self.latest["memory"] / 1048576.0, 2),
                    "tx_kbps": round(self.latest["tx_bps"] / 1024.0, 2),
                    "rx_kbps": round(self.latest["rx_bps"] / 1024.0, 2),
                    "volume_mb": round(self.latest["volume"] / 1048576.0, 2),
                })
                self.state["history"] = self.state["history"][-8640:]

            if now["mono"] - self._last_save >= 60:
                self._last_save = now["mono"]
                self._save_sync()

    async def _loop(self):
        self._last = self._reading()
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.sample_once()
            except asyncio.CancelledError:
                self._save_sync()
                raise
            except Exception as e:
                print(f"RAILWAY_METER sampler error: {e}")
                await asyncio.sleep(self.interval)

    def start(self):
        self.install_network_hooks()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="railway-cost-monitor")
            print("Railway Cost Monitor started")

    @staticmethod
    def _classify(host):
        host = (host or "unknown").lower()
        if host.endswith("newsky.app"):
            return "NewSky"
        if "github" in host or host.endswith("githubusercontent.com"):
            return "GitHub"
        if host.endswith("discord.com") or host.endswith("discordapp.com") or host.endswith("discord.gg"):
            return "Discord"
        if host.endswith("railway.com"):
            return "Railway API"
        return "Other"

    @staticmethod
    def _request_size(method, url, kwargs):
        total = len(str(method).encode()) + len(str(url).encode()) + 16
        try:
            for k, v in (kwargs.get("headers") or {}).items():
                total += len(str(k).encode()) + len(str(v).encode()) + 4
        except Exception:
            pass
        obj = kwargs.get("json")
        if obj is not None:
            try:
                total += len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode())
            except Exception:
                pass
        data = kwargs.get("data")
        try:
            if isinstance(data, bytes):
                total += len(data)
            elif isinstance(data, str):
                total += len(data.encode())
            elif isinstance(data, dict):
                total += len("&".join(f"{k}={v}" for k, v in data.items()).encode())
        except Exception:
            pass
        return total

    def record_outbound(self, host, amount, request=False, ws=False):
        source = self._classify(host)
        item = self.state["network"].setdefault(source, {
            "bytes": 0, "requests": 0, "ws_frames": 0, "hosts": {}
        })
        item["bytes"] = int(item.get("bytes", 0)) + max(0, int(amount))
        item["requests"] = int(item.get("requests", 0)) + (1 if request else 0)
        item["ws_frames"] = int(item.get("ws_frames", 0)) + (1 if ws else 0)
        h = item["hosts"].setdefault(host or "unknown", {"bytes": 0, "requests": 0})
        h["bytes"] = int(h.get("bytes", 0)) + max(0, int(amount))
        h["requests"] = int(h.get("requests", 0)) + (1 if request else 0)

    def install_network_hooks(self):
        RailwayCostMonitor._active = self
        if RailwayCostMonitor._installed:
            return
        RailwayCostMonitor._installed = True

        RailwayCostMonitor._orig_request = aiohttp.ClientSession._request

        async def wrapped_request(session, method, url, **kwargs):
            monitor = RailwayCostMonitor._active
            host = "unknown"
            try:
                host = urlsplit(str(url)).hostname or "unknown"
            except Exception:
                pass
            if monitor:
                monitor.record_outbound(host, monitor._request_size(method, url, kwargs), request=True)
            return await RailwayCostMonitor._orig_request(session, method, url, **kwargs)

        aiohttp.ClientSession._request = wrapped_request

        if hasattr(aiohttp, "ClientWebSocketResponse"):
            RailwayCostMonitor._orig_send_str = aiohttp.ClientWebSocketResponse.send_str
            RailwayCostMonitor._orig_send_bytes = aiohttp.ClientWebSocketResponse.send_bytes

            async def wrapped_send_str(ws, data, *args, **kwargs):
                monitor = RailwayCostMonitor._active
                if monitor:
                    try:
                        host = ws._response.url.host or "unknown"
                    except Exception:
                        host = "unknown"
                    monitor.record_outbound(host, len(str(data).encode()) + 8, ws=True)
                return await RailwayCostMonitor._orig_send_str(ws, data, *args, **kwargs)

            async def wrapped_send_bytes(ws, data, *args, **kwargs):
                monitor = RailwayCostMonitor._active
                if monitor:
                    try:
                        host = ws._response.url.host or "unknown"
                    except Exception:
                        host = "unknown"
                    try:
                        amount = len(data) + 8
                    except Exception:
                        amount = 8
                    monitor.record_outbound(host, amount, ws=True)
                return await RailwayCostMonitor._orig_send_bytes(ws, data, *args, **kwargs)

            aiohttp.ClientWebSocketResponse.send_str = wrapped_send_str
            aiohttp.ClientWebSocketResponse.send_bytes = wrapped_send_bytes

    @classmethod
    def local_costs(cls, u):
        cpu = float(u.get("cpu_seconds", 0)) / 60.0 * cls.PRICE_CPU / cls.MINUTES_IN_MONTH
        ram = float(u.get("memory_gb_minutes", 0)) * cls.PRICE_RAM / cls.MINUTES_IN_MONTH
        egress = float(u.get("network_tx_bytes", 0)) / 1000000000.0 * cls.PRICE_EGRESS
        volume = float(u.get("volume_gb_minutes", 0)) * cls.PRICE_VOLUME / cls.MINUTES_IN_MONTH
        return {"cpu": cpu, "ram": ram, "egress": egress, "volume": volume, "total": cpu + ram + egress + volume}

    @classmethod
    def official_costs(cls, m):
        cpu = float(m.get("CPU_USAGE", 0)) * cls.PRICE_CPU / cls.MINUTES_IN_MONTH
        ram = float(m.get("MEMORY_USAGE_GB", 0)) * cls.PRICE_RAM / cls.MINUTES_IN_MONTH
        egress = float(m.get("NETWORK_TX_GB", 0)) * cls.PRICE_EGRESS
        volume = float(m.get("DISK_USAGE_GB", 0)) * cls.PRICE_VOLUME / cls.MINUTES_IN_MONTH
        backup = float(m.get("BACKUP_USAGE_GB", 0)) * cls.PRICE_BACKUP / cls.MINUTES_IN_MONTH
        return {
            "cpu": cpu, "ram": ram, "egress": egress,
            "volume": volume, "backup": backup,
            "total": cpu + ram + egress + volume + backup,
        }

    @staticmethod
    async def _gql(session, query, variables, token, project_token=False):
        headers = {"Content-Type": "application/json"}
        if project_token:
            headers["Project-Access-Token"] = token
        else:
            headers["Authorization"] = "Bearer " + token
        async with session.post(
            "https://backboard.railway.com/graphql/v2",
            headers=headers,
            json={"query": query, "variables": variables},
            timeout=20,
        ) as r:
            payload = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"Railway HTTP {r.status}: {str(payload)[:300]}")
            if payload.get("errors"):
                raise RuntimeError(str(payload["errors"])[:500])
            return payload.get("data") or {}

    async def _workspace(self, session, token, project_id):
        explicit = os.getenv("RAILWAY_WORKSPACE_ID")
        if explicit:
            return explicit
        data = await self._gql(session, "query { workspaces { id name } }", {}, token)
        for ws in data.get("workspaces") or []:
            wid = ws.get("id")
            if not wid:
                continue
            try:
                q = "query P($w:String!){projects(first:5000,includeDeleted:true,workspaceId:$w){edges{node{id name}}}}"
                p = await self._gql(session, q, {"w": wid}, token)
                nodes = [x.get("node", {}) for x in ((p.get("projects") or {}).get("edges") or [])]
                if any(n.get("id") == project_id for n in nodes):
                    return wid
            except Exception:
                pass
        return None

    async def fetch_official(self, force=False):
        now = time.monotonic()
        if not force and self._official_cache is not None and now - self._official_cache_at < 120:
            return self._official_cache

        project_id = os.getenv("RAILWAY_PROJECT_ID")
        service_id = os.getenv("RAILWAY_SERVICE_ID")
        env_id = os.getenv("RAILWAY_ENVIRONMENT_ID")
        api_token = os.getenv("RAILWAY_API_TOKEN")
        project_token = os.getenv("RAILWAY_TOKEN")
        out = {"billing": None, "live": None, "agent": None, "error": None}

        if not project_id or not service_id:
            out["error"] = "Railway system IDs are unavailable."
            self._official_cache = out
            self._official_cache_at = now
            return out

        token = api_token or project_token
        use_project = not api_token and bool(project_token)
        if not token:
            out["error"] = "Set RAILWAY_API_TOKEN for exact billing, or RAILWAY_TOKEN for live metrics."
            self._official_cache = out
            self._official_cache_at = now
            return out

        try:
            async with aiohttp.ClientSession() as session:
                if env_id:
                    start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
                    q = """
                    query M($e:String!,$s:String,$start:DateTime!,$m:[MetricMeasurement!]!){
                      metrics(environmentId:$e,serviceId:$s,startDate:$start,measurements:$m){
                        measurement values{ts value}
                      }
                    }"""
                    try:
                        d = await self._gql(session, q, {
                            "e": env_id, "s": service_id, "start": start,
                            "m": ["CPU_USAGE","MEMORY_USAGE_GB","NETWORK_RX_GB","NETWORK_TX_GB","DISK_USAGE_GB","BACKUP_USAGE_GB"],
                        }, token, use_project)
                        live = {}
                        for item in d.get("metrics") or []:
                            values = [float(v.get("value", 0)) for v in (item.get("values") or [])]
                            if values:
                                live[item.get("measurement")] = {
                                    "current": values[-1], "avg": sum(values) / len(values),
                                    "peak": max(values), "samples": len(values),
                                }
                        out["live"] = live
                    except Exception as e:
                        out["live_error"] = str(e)

                if api_token:
                    wid = await self._workspace(session, api_token, project_id)
                    if not wid:
                        out["billing_error"] = "Workspace not resolved. Set RAILWAY_WORKSPACE_ID."
                    else:
                        qc = "query C($w:String!){workspace(workspaceId:$w){id name customer{currentUsage billingPeriod{start end}}}}"
                        cd = await self._gql(session, qc, {"w": wid}, api_token)
                        ws = cd.get("workspace") or {}
                        customer = ws.get("customer") or {}
                        bp = customer.get("billingPeriod") or {}
                        if bp.get("start") and bp.get("end"):
                            qu = """
                            query U($w:String!,$m:[MetricMeasurement!]!,$a:DateTime!,$b:DateTime!){
                              usage(workspaceId:$w,measurements:$m,groupBy:[PROJECT_ID,SERVICE_ID],
                                startDate:$a,endDate:$b,includeDeleted:true){
                                measurement value tags{projectId serviceId}
                              }
                            }"""
                            ud = await self._gql(session, qu, {
                                "w": wid,
                                "m": ["MEMORY_USAGE_GB","CPU_USAGE","NETWORK_TX_GB","DISK_USAGE_GB","BACKUP_USAGE_GB"],
                                "a": bp["start"], "b": bp["end"],
                            }, api_token)
                            m = defaultdict(float)
                            for row in ud.get("usage") or []:
                                tags = row.get("tags") or {}
                                if tags.get("projectId") == project_id and tags.get("serviceId") == service_id:
                                    m[row.get("measurement")] += float(row.get("value", 0))
                            out["billing"] = {
                                "period": bp, "workspace": ws.get("name"),
                                "workspace_current_usage": customer.get("currentUsage"),
                                "measurements": dict(m), "costs": self.official_costs(m),
                            }

                        try:
                            qa = "query A($w:String!){agentUsage(workspaceId:$w){totalUsedCents hardLimitCents softLimitCents usageRemaining billingPeriodEnd}}"
                            ad = await self._gql(session, qa, {"w": wid}, api_token)
                            out["agent"] = ad.get("agentUsage")
                        except Exception:
                            pass
        except Exception as e:
            out["error"] = str(e)

        self._official_cache = out
        self._official_cache_at = now
        return out

    @staticmethod
    def fmt_bytes(value):
        n = float(value or 0)
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while n >= 1024 and i < len(units) - 1:
            n /= 1024.0
            i += 1
        return f"{n:.2f} {units[i]}"

    @staticmethod
    def money(value):
        return "$" + f"{float(value or 0):.4f}"

    @staticmethod
    def pages():
        return ["Overview", "CPU & RAM", "Network", "Sources", "Hosts", "Storage", "Railway", "History"]

    async def snapshot(self, force=False):
        await self.sample_once()
        async with self._lock:
            state = json.loads(json.dumps(self.state))
            latest = dict(self.latest)
        official = await self.fetch_official(force=force)
        return state, latest, official

    async def build_embed(self, page=0, refresh_official=False):
        state, latest, official = await self.snapshot(force=refresh_official)
        total = state["totals"]
        local = self.local_costs(total)
        names = self.pages()
        page %= len(names)
        e = discord.Embed(
            title=f"Railway Cost Monitor - {names[page]}",
            color=0x7A5AF8,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_footer(text="Official Railway billing is authoritative. Local detailed meter starts when this monitor is deployed.")

        if page == 0:
            billed = (official.get("billing") or {}).get("costs")
            shown = billed or local
            mode = "Railway official" if billed else "local meter"
            e.description = (
                f"Current service usage cost: **{self.money(shown.get('total'))}** ({mode})\n"
                f"Meter since: {str(state.get('created_at',''))[:19]} UTC\n"
                f"Restarts tracked: {state.get('restarts',0)}\n\n"
                "Use the buttons below to switch pages."
            )
            e.add_field(name="CPU", value=f"Now {latest['cpu']:.5f} vCPU\nLocal {self.money(local['cpu'])}", inline=True)
            e.add_field(name="RAM", value=f"Now {latest['memory']/1048576:.1f} MB\nLocal {self.money(local['ram'])}", inline=True)
            e.add_field(name="Egress", value=f"{self.fmt_bytes(total.get('network_tx_bytes'))}\nLocal {self.money(local['egress'])}", inline=True)
            e.add_field(name="Volume", value=f"{self.fmt_bytes(latest['volume'])}\nLocal {self.money(local['volume'])}", inline=True)
            if billed:
                e.add_field(
                    name="Railway billed resources",
                    value=(
                        f"CPU {self.money(billed['cpu'])} | RAM {self.money(billed['ram'])}\n"
                        f"Egress {self.money(billed['egress'])} | Volume {self.money(billed['volume'])} | Backup {self.money(billed['backup'])}"
                    ),
                    inline=False,
                )
            elif official.get("error") or official.get("billing_error"):
                e.add_field(name="Official billing", value=str(official.get("error") or official.get("billing_error"))[:1024], inline=False)

        elif page == 1:
            cpu_min = float(total.get("cpu_seconds", 0)) / 60.0
            ram_min = float(total.get("memory_gb_minutes", 0))
            e.description = "Container-wide cgroup counters include bot.py and every child Python/Node process."
            e.add_field(name="CPU now", value=f"{latest['cpu']:.6f} vCPU", inline=True)
            e.add_field(name="CPU accumulated", value=f"{cpu_min:.3f} vCPU-min\n{self.money(local['cpu'])}", inline=True)
            e.add_field(name="RAM now", value=f"{latest['memory']/1048576:.2f} MB", inline=True)
            e.add_field(name="RAM accumulated", value=f"{ram_min:.3f} GB-min\n{self.money(local['ram'])}", inline=True)
            live = official.get("live") or {}
            lines = []
            if live.get("CPU_USAGE"):
                x = live["CPU_USAGE"]
                lines.append(f"CPU 2h: current {x['current']:.5f}, avg {x['avg']:.5f}, peak {x['peak']:.5f}")
            if live.get("MEMORY_USAGE_GB"):
                x = live["MEMORY_USAGE_GB"]
                lines.append(f"RAM 2h: current {x['current']*1024:.1f} MB, avg {x['avg']*1024:.1f} MB, peak {x['peak']*1024:.1f} MB")
            if lines:
                e.add_field(name="Railway live metrics", value="\n".join(lines), inline=False)

        elif page == 2:
            tx = int(total.get("network_tx_bytes", 0))
            rx = int(total.get("network_rx_bytes", 0))
            e.description = "Kernel interface totals cover every process and protocol in the container. Railway charges outbound TX; RX is shown for diagnosis."
            e.add_field(name="TX / Egress", value=f"{self.fmt_bytes(tx)}\n{self.money(local['egress'])}", inline=True)
            e.add_field(name="RX / Ingress", value=f"{self.fmt_bytes(rx)}\nNot egress-billed", inline=True)
            e.add_field(name="Current rate", value=f"TX {self.fmt_bytes(latest['tx_bps'])}/s\nRX {self.fmt_bytes(latest['rx_bps'])}/s", inline=True)
            b = official.get("billing")
            if b:
                m = b.get("measurements") or {}
                e.add_field(name="Railway billing-period egress", value=f"{m.get('NETWORK_TX_GB',0):.6f} GB -> {self.money(b['costs']['egress'])}", inline=False)

        elif page == 3:
            attrs = state.get("network", {})
            known = sum(int(v.get("bytes", 0)) for v in attrs.values())
            transport = max(0, int(total.get("network_tx_bytes", 0)) - known)
            e.description = "Application attribution is detailed, while total TX above is the kernel truth. Unclassified includes TLS/TCP/IP overhead and subprocess traffic."
            for name, item in sorted(attrs.items(), key=lambda kv: int(kv[1].get("bytes",0)), reverse=True)[:8]:
                e.add_field(
                    name=name,
                    value=f"{self.fmt_bytes(item.get('bytes'))} | {item.get('requests',0)} HTTP | {item.get('ws_frames',0)} WS frames",
                    inline=False,
                )
            e.add_field(name="Transport / unclassified", value=self.fmt_bytes(transport), inline=False)

        elif page == 4:
            rows = []
            for source, item in state.get("network", {}).items():
                for host, h in (item.get("hosts") or {}).items():
                    rows.append((int(h.get("bytes",0)), source, host, int(h.get("requests",0))))
            rows.sort(reverse=True)
            e.description = "Top outbound hosts seen through aiohttp and Discord's aiohttp WebSocket."
            if rows:
                text = "\n".join(f"{source}: {host} - {self.fmt_bytes(size)}, {reqs} req" for size, source, host, reqs in rows[:15])
            else:
                text = "No attributed traffic yet."
            e.add_field(name="Hosts", value=text[:1024], inline=False)

        elif page == 5:
            vol_min = float(total.get("volume_gb_minutes", 0))
            e.description = "Local volume uses allocated filesystem blocks. Railway API values are shown when available; backups cannot be seen from inside the container."
            e.add_field(name="Volume now", value=self.fmt_bytes(latest["volume"]), inline=True)
            e.add_field(name="Local accumulated", value=f"{vol_min:.4f} GB-min\n{self.money(local['volume'])}", inline=True)
            b = official.get("billing")
            if b:
                m = b.get("measurements") or {}
                c = b.get("costs") or {}
                e.add_field(name="Railway Volume", value=f"{m.get('DISK_USAGE_GB',0):.6f} GB-min\n{self.money(c.get('volume'))}", inline=True)
                e.add_field(name="Railway Backups", value=f"{m.get('BACKUP_USAGE_GB',0):.6f} GB-min\n{self.money(c.get('backup'))}", inline=True)

        elif page == 6:
            b = official.get("billing")
            e.description = "Billing-period data from Railway Public API is the source of truth used to reconcile the local meter."
            if b:
                m = b.get("measurements") or {}
                c = b.get("costs") or {}
                p = b.get("period") or {}
                e.add_field(name="Billing period", value=f"{p.get('start','?')}\n-> {p.get('end','?')}", inline=False)
                e.add_field(name="CPU", value=f"{m.get('CPU_USAGE',0):.6f}\n{self.money(c.get('cpu'))}", inline=True)
                e.add_field(name="RAM", value=f"{m.get('MEMORY_USAGE_GB',0):.6f} GB-min\n{self.money(c.get('ram'))}", inline=True)
                e.add_field(name="Egress", value=f"{m.get('NETWORK_TX_GB',0):.6f} GB\n{self.money(c.get('egress'))}", inline=True)
                e.add_field(name="Volume", value=f"{m.get('DISK_USAGE_GB',0):.6f} GB-min\n{self.money(c.get('volume'))}", inline=True)
                e.add_field(name="Backup", value=f"{m.get('BACKUP_USAGE_GB',0):.6f} GB-min\n{self.money(c.get('backup'))}", inline=True)
                e.add_field(name="Service metered total", value=f"**{self.money(c.get('total'))}**", inline=False)
                if b.get("workspace_current_usage") is not None:
                    e.add_field(name="Workspace currentUsage", value=str(b.get("workspace_current_usage")), inline=False)
            else:
                msg = official.get("billing_error") or official.get("error") or "No official billing data."
                e.add_field(name="Official billing unavailable", value=str(msg)[:1024], inline=False)
                e.add_field(
                    name="For exact billing",
                    value="Add an account/workspace token as RAILWAY_API_TOKEN. If workspace auto-detection fails, also add RAILWAY_WORKSPACE_ID.",
                    inline=False,
                )
            a = official.get("agent")
            if a:
                used = int(a.get("totalUsedCents") or 0) / 100.0
                hard = int(a.get("hardLimitCents") or 0) / 100.0
                e.add_field(name="Railway Agent - workspace, separate", value=f"Used USD {used:.2f} | hard limit USD {hard:.2f}", inline=False)
            e.add_field(
                name="Not attributed to this bot",
                value="Plan minimum/subscription, purchased domains, object Buckets and other workspace-level products are separate from this service's runtime metered resources.",
                inline=False,
            )

        else:
            lines = []
            for day in sorted(state.get("daily", {}), reverse=True)[:10]:
                u = state["daily"][day]
                c = self.local_costs(u)
                lines.append(
                    f"{day} | {self.money(c['total'])} | TX {self.fmt_bytes(u.get('network_tx_bytes'))} | CPU {float(u.get('cpu_seconds',0))/60:.2f} vCPU-min"
                )
            e.description = "Persistent local history stored in /app/data/railway_cost_meter.json."
            e.add_field(name="Recent days", value="\n".join(lines)[:1024] if lines else "No samples yet.", inline=False)
            e.add_field(
                name="Accuracy",
                value=(
                    "Exact billing: Railway billing-period API.\n"
                    "Container totals: cgroup CPU/RAM + kernel TX/RX.\n"
                    "Destination split: estimated application bytes; transport overhead remains unclassified."
                ),
                inline=False,
            )
        return e


class RailwayCostView(discord.ui.View):
    def __init__(self, monitor, owner_id, page=0):
        super().__init__(timeout=600)
        self.monitor = monitor
        self.owner_id = owner_id
        self.page = page
        self.message = None

        self.prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        self.label = discord.ui.Button(label="1/8 - Overview", style=discord.ButtonStyle.secondary, disabled=True)
        self.next = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        self.refresh = discord.ui.Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary)
        self.prev.callback = self._prev
        self.next.callback = self._next
        self.refresh.callback = self._refresh
        for item in (self.prev, self.label, self.next, self.refresh):
            self.add_item(item)
        self._sync()

    def _sync(self):
        names = self.monitor.pages()
        self.label.label = f"{self.page+1}/{len(names)} - {names[self.page]}"

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Цю панель відкрив інший адміністратор. Запусти !railway сам.", ephemeral=True)
            return False
        return True

    async def _render(self, interaction, force=False):
        self._sync()
        await interaction.response.defer()
        embed = await self.monitor.build_embed(self.page, refresh_official=force)
        await interaction.edit_original_response(embed=embed, view=self)

    async def _prev(self, interaction):
        self.page = (self.page - 1) % len(self.monitor.pages())
        await self._render(interaction)

    async def _next(self, interaction):
        self.page = (self.page + 1) % len(self.monitor.pages())
        await self._render(interaction)

    async def _refresh(self, interaction):
        await self._render(interaction, force=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
