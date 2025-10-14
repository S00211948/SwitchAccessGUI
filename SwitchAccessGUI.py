import tkinter as tk
from netmiko import ConnectHandler
from tkinter import messagebox
import re

# ---------------- TOOLTIP CLASS (fixed) ----------------
class ToolTip:
    def __init__(self, widget):
        self.widget = widget
        self.tipwindow = None

    def showtip(self, text, x, y):
        """Display tooltip text at a given screen position."""
        if self.tipwindow or not text:
            return
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x+20}+{y+10}")
        label = tk.Label(tw, text=text, justify=tk.LEFT,
                         background="#FFFFE0", relief=tk.SOLID, borderwidth=1,
                         font=("Arial", 10))
        label.pack()

    def hidetip(self):
        tw = self.tipwindow
        self.tipwindow = None
        if tw:
            tw.destroy()

# ---------------- SSH CONNECTION ----------------
def get_interface_status(ip, username, password):
    device = {
        'device_type': 'cisco_ios',
        'host': ip,
        'username': username,
        'password': password,
    }
    net_connect = ConnectHandler(**device)
    output = net_connect.send_command("show interface status")
    net_connect.disconnect()
    return output

# ---------------- PARSE INTERFACES ----------------
def parse_status(output):
    interfaces_by_member = {}
    for line in output.splitlines():
        if re.match(r"^Port", line) or not line.strip():
            continue

        fields = line.split()
        if len(fields) >= 6:  # Port Name Status Vlan Duplex Speed Type
            counter = 1
            keywords = ["connected", "notconnect", "err-disabled", "disabled"]
            port = fields[0]
            name = ""
            while str.lower(fields[counter]) not in keywords:
                name = f"{name} {fields[counter]}"
                counter+=1
            status = fields[counter]
            vlan = fields[counter+1]#3
            duplex = fields[counter+2]#4
            speed = fields[counter+3]#5
        elif len(fields) >= 2:
            port = fields[0]
            status = fields[2]
            vlan = duplex = speed = ""
        else:
            continue

        match = re.match(r"Gi(\d+)/\d+/\d+", port)
        member = int(match.group(1)) if match else 1

        if member not in interfaces_by_member:
            interfaces_by_member[member] = []

        interfaces_by_member[member].append((port, name, status, vlan, duplex, speed))

    return interfaces_by_member

# ---------------- FILTER FRONT PANEL PORTS ----------------
def filter_front_ports(interfaces):
    front_ports = []
    for p in interfaces:
        port_name = p[0]
        m = re.match(r"Gi\d+/0/(\d+)", port_name)
        if m:
            port_num = int(m.group(1))
            if 1 <= port_num <= 48:  # adjust if your switch has 24 or another count
                front_ports.append(p)
    # Sort by port number for correct order
    front_ports.sort(key=lambda x: int(re.search(r"\d+$", x[0]).group()))
    return front_ports

# ---------------- DRAW SWITCH ----------------
def draw_switch(canvas, interfaces, member_id):
    canvas.delete("all")
    tooltip_data = {}

    interfaces = filter_front_ports(interfaces)

    box_width = 40
    box_height = 30
    padding = 5
    cols = 24
    rows = 2

    width_needed = padding * (cols + 1) + box_width * cols
    height_needed = 30 + padding * (rows + 1) + box_height * rows
    canvas.config(width=width_needed, height=height_needed)

    canvas.create_text(10, 10, text=f"Switch {member_id}",
                       anchor="w", fill="white", font=("Arial", 12, "bold"))

    for idx, (port, name, status, vlan, duplex, speed) in enumerate(interfaces):
        row = 0 if idx < cols else 1
        col = idx % cols

        x1 = padding + col * (box_width + padding)
        y1 = 30 + row * (box_height + padding)
        x2 = x1 + box_width
        y2 = y1 + box_height

        # Decide colour
        if status.lower() == "connected":
            color = "green"
        elif "notconnect" in status.lower():
            color = "red"
        else:
            color = "gray"

        rect = canvas.create_rectangle(x1, y1, x2, y2, fill=color)
        text_item = canvas.create_text((x1+x2)/2, (y1+y2)/2,
                                       text=port.split('/')[-1],
                                       fill="white", font=("Arial", 8))
        tooltip_text = f"{port}\nName: {name}\nStatus: {status}\nVLAN: {vlan}\nDuplex: {duplex}\nSpeed: {speed}"
        tooltip_data[rect] = tooltip_text
        tooltip_data[text_item] = tooltip_text

    # Bind tooltip events
    tooltip = ToolTip(canvas)

    def on_motion(event):
        item = canvas.find_withtag("current")
        if item and item[0] in tooltip_data:
            tooltip.showtip(tooltip_data[item[0]], event.x_root, event.y_root)
        else:
            tooltip.hidetip()

    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Leave>", lambda e: tooltip.hidetip())

# ---------------- REFRESH DRAWING ----------------
def refresh():
    try:
        output = get_interface_status(ip_entry.get(),
                                      user_entry.get(),
                                      pass_entry.get())
        interfaces_by_member = parse_status(output)

        # Clear old canvases
        for c in canvases:
            c.destroy()
        canvases.clear()

        # Draw each stack member
        r = 5
        for member_id in sorted(interfaces_by_member.keys()):
            frame = tk.Frame(root, bg="black")
            frame.grid(row=r, column=0, columnspan=2, pady=10, padx=10, sticky="EW")
            canvas = tk.Canvas(frame, bg="black", highlightthickness=0)
            canvas.pack()
            draw_switch(canvas, interfaces_by_member[member_id], member_id)
            canvases.append(frame)
            r += 1

        root.update_idletasks()
        root.geometry('')  # auto-size window to fit content

    except Exception as e:
        messagebox.showerror("Error", str(e))

    root.after(30000, refresh)  # refresh every 10 seconds

# ---------------- GUI SETUP ----------------
root = tk.Tk()
root.title("Cisco Stacked Switch Port Status")

tk.Label(root, text="IP Address:").grid(row=0, column=0)
ip_entry = tk.Entry(root)
ip_entry.grid(row=0, column=1,sticky="EW")

tk.Label(root, text="Username:").grid(row=1, column=0)
user_entry = tk.Entry(root)
user_entry.grid(row=1, column=1,sticky="EW")

tk.Label(root, text="Password:").grid(row=2, column=0)
pass_entry = tk.Entry(root, show="*")
pass_entry.grid(row=2, column=1,sticky="EW")

connect_button = tk.Button(root, text="Start Monitor", command=refresh)
connect_button.grid(row=3, column=0, columnspan=2,sticky="EW")

canvases = []

root.mainloop()