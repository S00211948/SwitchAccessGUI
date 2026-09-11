import json
import os
import re
import socket
import threading
import tkinter as tk
from tkinter import StringVar, messagebox, ttk

from netmiko import ConnectHandler#, NetmikoTimeoutError, NetmikoAuthenticationError

SWITCHES_FILE = "./switches.json"
REFRESH_INTERVAL_MS = 30000  # 30 seconds
MAX_FRONT_PORT = 48  # adjust if your switches have 24 (or another) front-panel ports

# ---------------- THEME ----------------
BG = "#1e1f24"
PANEL_BG = "#262832"
FIELD_BG = "#31333d"
TEXT = "#e8e8ea"
MUTED = "#9aa0a8"
ACCENT = "#3fa7ff"
GOOD = "#3ecf6a"
BAD = "#ef5a5a"


# ---------------- TOOLTIP ----------------
class ToolTip:
    """Simple tooltip that follows the mouse over canvas items."""

    def __init__(self, widget):
        self.widget = widget
        self.tipwindow = None

    def showtip(self, text, x, y):
        if self.tipwindow or not text:
            return
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x + 20}+{y + 10}")
        tk.Label(
            tw, text=text, justify=tk.LEFT,
            background="#FFFFE0", relief=tk.SOLID, borderwidth=1,
            font=("Arial", 10),
        ).pack()

    def hidetip(self):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


# ---------------- SSH ----------------
def get_interface_status(ip, username, password):
    device = {
        "device_type": "cisco_ios",
        "host": ip,
        "username": username,
        "password": password,
    }
    net_connect = ConnectHandler(**device)
    try:
        hostname_output = net_connect.send_command("show running-config | include hostname")
        match = re.search(r"hostname\s+(\S+)", hostname_output)
        hostname = match.group(1) if match else ip
        output = net_connect.send_command("show interface status")
    finally:
        net_connect.disconnect()
    return hostname, output


# ---------------- PORT NEIGHBOR LOOKUP (on-demand, per click) ----------------
def _parse_cdp_detail(output):
    """Pull hostname/IP/platform out of `show cdp neighbors <port> detail`."""
    if not output or "Device ID" not in output:
        return None
    device_id = re.search(r"Device ID:\s*(\S+)", output)
    ip_addr = re.search(r"IP address:\s*(\S+)", output)
    platform = re.search(r"Platform:\s*([^,]+),", output)
    if not device_id:
        return None
    return {
        "hostname": device_id.group(1),
        "ip": ip_addr.group(1) if ip_addr else None,
        "platform": platform.group(1).strip() if platform else None,
    }


def _parse_lldp_detail(output):
    """Pull hostname/IP/platform out of `show lldp neighbors interface <port> detail`."""
    if not output or ("System Name" not in output and "Chassis id" not in output):
        return None
    name = re.search(r"System Name:\s*(\S+)", output)
    mgmt_ip = re.search(r"Management Address(?:es)?:\s*\n?\s*(?:IP:\s*)?(\S+)", output)
    platform = re.search(r"System Description:\s*\n\s*(.+)", output)
    if not name and not mgmt_ip:
        return None
    return {
        "hostname": name.group(1) if name else None,
        "ip": mgmt_ip.group(1) if mgmt_ip else None,
        "platform": platform.group(1).strip() if platform else None,
    }


def _parse_mac_table(output):
    """Grab the first MAC address seen on a port from `show mac address-table interface <port>`."""
    for line in output.splitlines():
        m = re.search(r"([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})", line)
        if m:
            return m.group(1)
    return None


def _parse_arp_ip(output, mac):
    """Find the IP paired with `mac` in `show ip arp` output."""
    for line in output.splitlines():
        if mac.lower() in line.lower():
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
            if m:
                return m.group(1)
    return None


def query_port_neighbor(ip, username, password, port):
    """Look up what's connected to a single port, on demand.

    Tries CDP first, then LLDP - both give a hostname directly from the
    neighboring device. If the attached device doesn't speak either (a
    plain PC, printer, etc.), falls back to reading the port's MAC address
    off the MAC address table, matching that MAC in the ARP table to get
    an IP, and reverse-resolving that IP via DNS.

    Only ever called for a single port on click, never during the
    polling refresh, to avoid running several extra show commands per
    port on every refresh cycle.
    """
    device = {
        "device_type": "cisco_ios",
        "host": ip,
        "username": username,
        "password": password,
    }
    net_connect = ConnectHandler(**device)
    try:
        cdp = _parse_cdp_detail(net_connect.send_command(f"show cdp neighbors {port} detail"))
        if cdp:
            return {"port": port, "source": "CDP", **cdp}

        lldp = _parse_lldp_detail(
            net_connect.send_command(f"show lldp neighbors interface {port} detail")
        )
        if lldp:
            return {"port": port, "source": "LLDP", **lldp}

        mac = _parse_mac_table(net_connect.send_command(f"show mac address-table interface {port}"))
        if not mac:
            return {"port": port, "source": None}

        arp_ip = _parse_arp_ip(net_connect.send_command(f"show ip arp | include {mac}"), mac)

        hostname = None
        if arp_ip:
            try:
                hostname = socket.gethostbyaddr(arp_ip)[0]
            except (socket.herror, socket.gaierror, OSError):
                hostname = None

        return {
            "port": port,
            "source": "ARP" if arp_ip else None,
            "mac": mac,
            "ip": arp_ip,
            "hostname": hostname,
        }
    finally:
        net_connect.disconnect()


# ---------------- PARSE INTERFACES ----------------
def parse_status(output):
    """Parse `show interface status` output into
    {stack_member: [(port, name, status, vlan, duplex, speed), ...]}
    """
    interfaces_by_member = {}
    keywords = ("connected", "notconnect", "err-disabled", "disabled", "monitor", "inactive")

    for line in output.splitlines():
        if not line.strip() or line.startswith("Port"):
            continue

        fields = line.split()
        if len(fields) < 2:
            continue

        port = fields[0]

        # Find the status keyword; everything between the port and the
        # status keyword is the (possibly multi-word, possibly empty)
        # description/name field.
        status_idx = None
        for i in range(1, len(fields)):
            if fields[i].lower() in keywords:
                status_idx = i
                break

        if status_idx is None:
            # Couldn't confidently parse this line - skip it rather than
            # risk an IndexError or storing garbage data.
            continue

        name = " ".join(fields[1:status_idx])
        status = fields[status_idx]
        rest = fields[status_idx + 1:]
        vlan = rest[0] if len(rest) > 0 else ""
        duplex = rest[1] if len(rest) > 1 else ""
        speed = rest[2] if len(rest) > 2 else ""

        match = re.match(r"Gi(\d+)/\d+/\d+", port)
        member = int(match.group(1)) if match else 1

        interfaces_by_member.setdefault(member, []).append(
            (port, name, status, vlan, duplex, speed)
        )

    return interfaces_by_member


# ---------------- FILTER FRONT PANEL PORTS ----------------
def filter_front_ports(interfaces):
    front_ports = []
    for p in interfaces:
        m = re.match(r"Gi\d+/0/(\d+)", p[0])
        if m and 1 <= int(m.group(1)) <= MAX_FRONT_PORT:
            front_ports.append(p)
    front_ports.sort(key=lambda x: int(re.search(r"\d+$", x[0]).group()))
    return front_ports


# ---------------- DRAW SWITCH ----------------
def draw_switch(canvas, interfaces, member_id, hostname, on_port_click=None):
    canvas.delete("all")
    tooltip_data = {}
    port_by_item = {}

    interfaces = filter_front_ports(interfaces)

    box_width = 40
    box_height = 30
    padding = 5
    cols = 24
    rows = 2

    width_needed = padding * (cols + 1) + box_width * cols
    height_needed = 30 + padding * (rows + 1) + box_height * rows
    canvas.config(width=width_needed, height=height_needed)

    canvas.create_text(
        10, 10, text=f"{hostname} - Switch {member_id}",
        anchor="w", fill="white", font=("Arial", 12, "bold"),
    )

    status_colors = {"connected": "green", "notconnect": "red"}

    for port, name, status, vlan, duplex, speed in interfaces:
        port_num = int(port.split("/")[-1])
        col = (port_num - 1) // 2
        row = 0 if port_num % 2 == 1 else 1

        x1 = padding + col * (box_width + padding)
        y1 = 30 + row * (box_height + padding)
        x2 = x1 + box_width
        y2 = y1 + box_height

        color = status_colors.get(status.lower(), "gray")

        rect = canvas.create_rectangle(x1, y1, x2, y2, fill=color)
        text_item = canvas.create_text(
            (x1 + x2) / 2, (y1 + y2) / 2,
            text=str(port_num), fill="white", font=("Arial", 8),
        )
        tooltip_text = (
            f"{port}\nName: {name or '-'}\nStatus: {status}\n"
            f"VLAN: {vlan}\nDuplex: {duplex}\nSpeed: {speed}"
        )
        tooltip_data[rect] = tooltip_text
        tooltip_data[text_item] = tooltip_text
        port_by_item[rect] = port
        port_by_item[text_item] = port

    tooltip = ToolTip(canvas)

    def on_motion(event):
        item = canvas.find_withtag("current")
        if item and item[0] in tooltip_data:
            tooltip.showtip(tooltip_data[item[0]], event.x_root, event.y_root)
            canvas.config(cursor="hand2")
        else:
            tooltip.hidetip()
            canvas.config(cursor="")

    def on_click(event):
        item = canvas.find_withtag("current")
        if item and item[0] in port_by_item and on_port_click:
            on_port_click(port_by_item[item[0]])

    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Leave>", lambda e: tooltip.hidetip())
    canvas.bind("<Button-1>", on_click)


# ---------------- APP ----------------
class SwitchGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Cisco Stacked Switch Port Status")
        self.root.configure(bg=BG)
        self.root.columnconfigure(0, weight=1)

        # Credentials actually in use for the live connection. Captured once
        # when "Connect" is clicked, and reused for every scheduled refresh -
        # editing the entry fields afterwards has no effect until the user
        # clicks Connect again.
        self.active_ip = None
        self.active_username = None
        self.active_password = None

        self.canvases = []
        self.refresh_job = None
        self.switches_by_label = {}
        self.port_dialog = None
        self.port_dialog_body = None

        self._init_style()
        self._build_form()
        self._load_switch_data()

        # Let the window size itself to fit everything, then don't allow
        # shrinking below that so the form never gets clipped.
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    # ---- styling ----
    def _init_style(self):
        style = ttk.Style(self.root)
        # 'clam' is required for background/foreground colors to actually
        # apply on ttk widgets across platforms.
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=PANEL_BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=PANEL_BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 15, "bold"))
        style.configure(
            "TEntry", fieldbackground=FIELD_BG, foreground=TEXT,
            insertcolor=TEXT, bordercolor=FIELD_BG, lightcolor=FIELD_BG, darkcolor=FIELD_BG,
        )
        style.configure(
            "TMenubutton", background=FIELD_BG, foreground=TEXT,
            bordercolor=FIELD_BG, arrowcolor=TEXT,
        )
        style.configure(
            "Accent.TButton", background=ACCENT, foreground="#0b0b0d",
            font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0,
        )
        style.map("Accent.TButton", background=[("active", "#68bbff"), ("disabled", MUTED)])

    # ---- UI construction ----
    def _build_form(self):
        header = ttk.Label(self.root, text="Cisco Stacked Switch Port Status", style="Header.TLabel")
        header.grid(row=0, column=0, sticky="W", padx=16, pady=(14, 8))

        panel = ttk.Frame(self.root, style="Panel.TFrame", padding=14)
        panel.grid(row=1, column=0, sticky="EW", padx=16, pady=(0, 10))
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(3, weight=1)

        ttk.Label(panel, text="IP Address", style="Panel.TLabel").grid(row=0, column=0, sticky="W", pady=4)
        self.ip_entry = ttk.Entry(panel)
        self.ip_entry.grid(row=0, column=1, sticky="EW", padx=(8, 16), pady=4)

        ttk.Label(panel, text="Area", style="Panel.TLabel").grid(row=0, column=2, sticky="W", pady=4)
        self.selected_option = StringVar()
        self.area_menu = ttk.OptionMenu(panel, self.selected_option, "")
        self.area_menu.grid(row=0, column=3, sticky="EW", padx=(8, 0), pady=4)
        self.selected_option.trace_add("write", self._on_area_selected)

        ttk.Label(panel, text="Username", style="Panel.TLabel").grid(row=1, column=0, sticky="W", pady=4)
        self.user_entry = ttk.Entry(panel)
        self.user_entry.grid(row=1, column=1, sticky="EW", padx=(8, 16), pady=4)

        ttk.Label(panel, text="Password", style="Panel.TLabel").grid(row=1, column=2, sticky="W", pady=4)
        self.pass_entry = ttk.Entry(panel, show="*")
        self.pass_entry.grid(row=1, column=3, sticky="EW", padx=(8, 0), pady=4)

        self.connect_button = ttk.Button(
            panel, text="Connect", style="Accent.TButton", command=self.start_session
        )
        self.connect_button.grid(row=2, column=0, columnspan=4, sticky="EW", pady=(12, 2))

        # Plain tk.Label (not ttk) so we can freely recolor it for
        # connecting/connected/error states.
        self.status_label = tk.Label(
            panel, text="Not connected", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9)
        )
        self.status_label.grid(row=3, column=0, columnspan=4, sticky="W", pady=(6, 0))

        self.switches_frame = ttk.Frame(self.root, style="TFrame")
        self.switches_frame.grid(row=2, column=0, sticky="EW", padx=16, pady=(0, 16))

    # ---- switches.json ----
    def _load_switch_data(self):
        """Load {"Area": [{"name": ..., "ip": ...}, ...]} from switches.json, if present."""
        if not os.path.exists(SWITCHES_FILE):
            self.selected_option.set("No switches.json found")
            return

        try:
            with open(SWITCHES_FILE, "r") as file:
                switch_data = json.load(file)
        except (json.JSONDecodeError, OSError) as e:
            messagebox.showerror("Error", f"Could not read {SWITCHES_FILE}: {e}")
            return

        labels = []
        for area, switches in switch_data.items():
            for switch in switches:
                label = f"{area} - {switch.get('name', switch.get('ip', '?'))}"
                self.switches_by_label[label] = switch.get("ip", "")
                labels.append(label)

        menu = self.area_menu["menu"]
        menu.delete(0, "end")
        for label in labels:
            menu.add_command(label=label, command=lambda v=label: self.selected_option.set(v))

        if labels:
            self.selected_option.set(labels[0])

    def _on_area_selected(self, *_):
        ip = self.switches_by_label.get(self.selected_option.get())
        if ip:
            self.ip_entry.delete(0, tk.END)
            self.ip_entry.insert(0, ip)

    # ---- connection handling ----
    def start_session(self):
        """Snapshot the current form values and connect to that switch.

        These values are stored on self and reused for every subsequent
        auto-refresh - the entry fields are never re-read again until this
        button is clicked again. That way editing the IP field mid-refresh
        (e.g. to prep a connection to a different switch) can't cause a
        refresh to connect to a half-typed address.
        """
        ip = self.ip_entry.get().strip()
        username = self.user_entry.get().strip()
        password = self.pass_entry.get()

        if not ip or not username or not password:
            messagebox.showwarning("Missing info", "Please fill in IP address, username and password.")
            return

        # Cancel any refresh loop that was running against a previous switch.
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None

        self.active_ip = ip
        self.active_username = username
        self.active_password = password

        self.connect_button.config(state="disabled", text="Connecting...")
        self.status_label.config(text=f"Connecting to {ip}...", fg=MUTED)

        threading.Thread(target=self._connect_and_refresh, daemon=True).start()

    def _connect_and_refresh(self):
        try:
            hostname, output = get_interface_status(
                self.active_ip, self.active_username, self.active_password
            )
            interfaces_by_member = parse_status(output)
            self.root.after(0, self._on_connect_success, hostname, interfaces_by_member)
        #except (NetmikoTimeoutError, NetmikoAuthenticationError) as e:
        #    self.root.after(0, self._on_connect_error, str(e))
        except Exception as e:
            self.root.after(0, self._on_connect_error, str(e))

    def _on_connect_success(self, hostname, interfaces_by_member):
        self.connect_button.config(state="normal", text="Connect")
        self.status_label.config(text=f"Connected to {hostname} ({self.active_ip})", fg=GOOD)
        self._draw_all(hostname, interfaces_by_member)
        self._schedule_refresh()

    def _on_connect_error(self, message):
        self.connect_button.config(state="normal", text="Connect")
        self.status_label.config(text="Connection failed", fg=BAD)
        messagebox.showerror("Connection Error", message)

    def _draw_all(self, hostname, interfaces_by_member):
        for c in self.canvases:
            c.destroy()
        self.canvases.clear()

        for member_id in sorted(interfaces_by_member.keys()):
            frame = tk.Frame(self.switches_frame, bg="black")
            frame.pack(fill="x", pady=(0, 10))
            canvas = tk.Canvas(frame, bg="black", highlightthickness=0)
            canvas.pack()
            draw_switch(
                canvas, interfaces_by_member[member_id], member_id, hostname,
                on_port_click=self._handle_port_click,
            )
            self.canvases.append(frame)

        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    # ---- port neighbor lookup (click) ----
    def _handle_port_click(self, port):
        if not self.active_ip:
            return

        self._open_port_dialog(port)
        threading.Thread(target=self._query_port_worker, args=(port,), daemon=True).start()

    def _open_port_dialog(self, port):
        if self.port_dialog is not None and self.port_dialog.winfo_exists():
            self.port_dialog.destroy()

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Port {port}")
        dlg.configure(bg=PANEL_BG)
        dlg.resizable(False, False)

        body = tk.Label(
            dlg, text=f"Looking up neighbor on {port}...\n(CDP, then LLDP, then MAC/ARP)",
            bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10), justify="left", padx=18, pady=18,
        )
        body.pack()

        close_btn = ttk.Button(dlg, text="Close", command=dlg.destroy)
        close_btn.pack(pady=(0, 14))

        self.port_dialog = dlg
        self.port_dialog_body = body

    def _query_port_worker(self, port):
        try:
            result = query_port_neighbor(
                self.active_ip, self.active_username, self.active_password, port
            )
        except Exception as e:
            result = {"port": port, "source": None, "error": str(e)}
        self.root.after(0, self._update_port_dialog, result)

    def _update_port_dialog(self, result):
        # The user may have closed the dialog, or clicked a different port,
        # before this query finished - if so, just drop the stale result.
        if self.port_dialog is None or not self.port_dialog.winfo_exists():
            return
        if self.port_dialog.title() != f"Port {result['port']}":
            return

        source = result.get("source")
        if result.get("error"):
            text = f"Query failed:\n{result['error']}"
        elif source in ("CDP", "LLDP"):
            lines = [f"Discovered via: {source}", ""]
            lines.append(f"Hostname: {result.get('hostname') or 'unknown'}")
            lines.append(f"IP address: {result.get('ip') or 'unknown'}")
            if result.get("platform"):
                lines.append(f"Platform: {result['platform']}")
            text = "\n".join(lines)
        elif source == "ARP":
            lines = ["Discovered via: MAC/ARP table (no CDP/LLDP neighbor)", ""]
            lines.append(f"MAC address: {result.get('mac')}")
            lines.append(f"IP address: {result.get('ip') or 'unknown'}")
            lines.append(f"Hostname: {result.get('hostname') or 'no rDNS record'}")
            text = "\n".join(lines)
        else:
            text = "No neighbor information found.\n(No CDP/LLDP neighbor, and nothing learned on this port.)"

        self.port_dialog_body.config(text=text, justify="left")

    def _schedule_refresh(self):
        def do_refresh():
            threading.Thread(target=self._background_refresh, daemon=True).start()

        self.refresh_job = self.root.after(REFRESH_INTERVAL_MS, do_refresh)

    def _background_refresh(self):
        try:
            hostname, output = get_interface_status(
                self.active_ip, self.active_username, self.active_password
            )
            interfaces_by_member = parse_status(output)
            self.root.after(0, self._on_refresh_success, hostname, interfaces_by_member)
        except Exception as e:
            self.root.after(0, self._on_refresh_error, str(e))

    def _on_refresh_success(self, hostname, interfaces_by_member):
        self.status_label.config(text=f"Connected to {hostname} ({self.active_ip})", fg=GOOD)
        self._draw_all(hostname, interfaces_by_member)
        self._schedule_refresh()

    def _on_refresh_error(self, message):
        self.status_label.config(text=f"Refresh failed: {message}", fg=BAD)
        # Keep retrying on the same interval rather than giving up silently.
        self._schedule_refresh()


if __name__ == "__main__":
    root = tk.Tk()
    app = SwitchGUI(root)
    root.mainloop()