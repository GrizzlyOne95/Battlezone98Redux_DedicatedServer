import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import socket
import threading
import datetime
import binascii
import struct
import json

# Configuration derived from your logs
DEFAULT_HOST = "68.183.35.188" # Rebellion BZRNet Server
DEFAULT_PORT = 1338            # BZRNet UDP Port

class BZRNetProbe:
    def __init__(self, root):
        self.root = root
        self.root.title("BZRNet Protocol Probe")
        self.root.geometry("900x700")
        
        self.server_socket = None
        self.server_thread = None
        self.is_server_running = False
        
        self.setup_ui()
        
    def setup_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.client_tab = ttk.Frame(notebook)
        self.server_tab = ttk.Frame(notebook)
        self.parser_tab = ttk.Frame(notebook)
        
        notebook.add(self.client_tab, text="UDP Client (Probe)")
        notebook.add(self.server_tab, text="UDP Server (Listen)")
        notebook.add(self.parser_tab, text="Log Parser")
        
        self.setup_client_tab()
        self.setup_server_tab()
        self.setup_parser_tab()
        
    def setup_client_tab(self):
        frame = ttk.LabelFrame(self.client_tab, text="Send Packet", padding=10)
        frame.pack(fill="x", padx=10, pady=5)
        
        # Target
        ttk.Label(frame, text="Target IP:").grid(row=0, column=0, sticky="w")
        self.target_ip = ttk.Entry(frame, width=20)
        self.target_ip.insert(0, DEFAULT_HOST)
        self.target_ip.grid(row=0, column=1, padx=5)
        
        ttk.Label(frame, text="Port:").grid(row=0, column=2, sticky="w")
        self.target_port = ttk.Entry(frame, width=10)
        self.target_port.insert(0, str(DEFAULT_PORT))
        self.target_port.grid(row=0, column=3, padx=5)
        
        # Payload
        ttk.Label(frame, text="Hex Payload:").grid(row=1, column=0, sticky="w", pady=5)
        self.payload_entry = ttk.Entry(frame, width=60)
        # Preset from your log
        self.payload_entry.insert(0, "40020000019c220b17800000000000000000") 
        self.payload_entry.grid(row=1, column=1, columnspan=3, padx=5, sticky="ew")
        
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=1, columnspan=3, sticky="w", pady=5)
        ttk.Button(btn_frame, text="Send UDP", command=self.send_packet).pack(side="left")
        ttk.Button(btn_frame, text="Clear Log", command=lambda: self.client_log.delete(1.0, tk.END)).pack(side="left", padx=5)
        
        # Response Log
        log_frame = ttk.LabelFrame(self.client_tab, text="Traffic Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.client_log = scrolledtext.ScrolledText(log_frame, height=10, bg="black", fg="#00ff00", font=("Consolas", 10))
        self.client_log.pack(fill="both", expand=True)
        
    def setup_server_tab(self):
        frame = ttk.LabelFrame(self.server_tab, text="Fake Server Settings", padding=10)
        frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(frame, text="Listen Port:").pack(side="left")
        self.listen_port = ttk.Entry(frame, width=10)
        self.listen_port.insert(0, str(DEFAULT_PORT))
        self.listen_port.pack(side="left", padx=5)
        
        self.server_btn = ttk.Button(frame, text="Start Server", command=self.toggle_server)
        self.server_btn.pack(side="left", padx=10)
        
        # Server Log
        log_frame = ttk.LabelFrame(self.server_tab, text="Incoming Packets", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.server_log = scrolledtext.ScrolledText(log_frame, height=10, bg="black", fg="#00ffff", font=("Consolas", 10))
        self.server_log.pack(fill="both", expand=True)
        
    def setup_parser_tab(self):
        frame = ttk.Frame(self.parser_tab, padding=10)
        frame.pack(fill="both", expand=True)
        
        ttk.Label(frame, text="Paste Game Logs Here:").pack(anchor="w")
        self.parser_input = scrolledtext.ScrolledText(frame, height=10)
        self.parser_input.pack(fill="x", pady=5)
        
        ttk.Button(frame, text="Extract & Analyze Hex", command=self.parse_logs).pack(anchor="w")
        
        ttk.Label(frame, text="Analysis Output:").pack(anchor="w", pady=(10,0))
        self.parser_output = scrolledtext.ScrolledText(frame, height=15, bg="#f0f0f0", font=("Consolas", 9))
        self.parser_output.pack(fill="both", expand=True, pady=5)

    def log(self, widget, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        widget.insert(tk.END, f"[{ts}] {msg}\n")
        widget.see(tk.END)

    def send_packet(self):
        ip = self.target_ip.get()
        try:
            port = int(self.target_port.get())
            hex_data = self.payload_entry.get().replace(" ", "")
            data = binascii.unhexlify(hex_data)
        except Exception as e:
            self.log(self.client_log, f"Error: {e}")
            return
            
        def _send():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(2.0)
                self.log(self.client_log, f"TX -> {ip}:{port} | {hex_data}")
                sock.sendto(data, (ip, port))
                
                try:
                    resp, addr = sock.recvfrom(4096)
                    self.log(self.client_log, f"RX <- {addr[0]}:{addr[1]} | {binascii.hexlify(resp).decode()}")
                except socket.timeout:
                    self.log(self.client_log, "Timed out waiting for response.")
                finally:
                    sock.close()
            except Exception as e:
                self.log(self.client_log, f"Socket Error: {e}")
                
        threading.Thread(target=_send, daemon=True).start()

    def toggle_server(self):
        if self.is_server_running:
            self.is_server_running = False
            if self.server_socket:
                self.server_socket.close()
            self.server_btn.config(text="Start Server")
            self.log(self.server_log, "Server stopped.")
        else:
            try:
                port = int(self.listen_port.get())
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.server_socket.bind(("0.0.0.0", port))
                self.is_server_running = True
                self.server_btn.config(text="Stop Server")
                self.log(self.server_log, f"Server listening on port {port}...")
                
                self.server_thread = threading.Thread(target=self.server_loop, daemon=True)
                self.server_thread.start()
            except Exception as e:
                self.log(self.server_log, f"Failed to start server: {e}")

    def server_loop(self):
        while self.is_server_running:
            try:
                data, addr = self.server_socket.recvfrom(4096)
                hex_data = binascii.hexlify(data).decode()
                self.log(self.server_log, f"RX <- {addr[0]}:{addr[1]} | {hex_data}")
                
                # Auto-Reply Logic (Mocking)
                # If we receive the '40...' packet, maybe send an ACK?
                # For now, just log.
                
            except OSError:
                break
            except Exception as e:
                self.log(self.server_log, f"Server Error: {e}")

    def parse_logs(self):
        text = self.parser_input.get(1.0, tk.END)
        self.parser_output.delete(1.0, tk.END)
        
        lines = text.splitlines()
        for line in lines:
            if "BZRNet P2P Sent Packet" in line or "BZRNet P2P Received" in line:
                parts = line.split(":")
                if len(parts) >= 4: # Timestamp:Msg:IP:Port: Hex
                    hex_part = parts[-1].strip()
                    self.analyze_packet(hex_part, "Sent" if "Sent" in line else "Recv")

    def analyze_packet(self, hex_data, direction):
        try:
            data = binascii.unhexlify(hex_data)
            length = len(data)
            
            analysis = f"{direction} [{length} bytes]: {hex_data}\n"
            
            # Basic Header Guessing
            if length >= 1:
                pkt_type = data[0]
                analysis += f"  Type: 0x{pkt_type:02x}\n"
                
                if pkt_type == 0x40:
                    analysis += "  -> Possible 'Client Hello' / 'Auth Request'\n"
                    if length >= 18:
                        # 40 02 00 00 01 9c 22 0b 17 80 ...
                        # 02 00 -> 2 (Version?)
                        # 00 01 -> 1 (Sequence?)
                        # 9c 22 0b 17 -> Timestamp/ID?
                        
                        # Try unpacking
                        try:
                            # Skip type (1 byte)
                            # Assume Little Endian
                            u1, u2, u3 = struct.unpack("<HHI", data[1:9])
                            analysis += f"  -> Field 1 (u16): {u1}\n"
                            analysis += f"  -> Field 2 (u16): {u2}\n"
                            analysis += f"  -> Field 3 (u32): {u3} (0x{u3:08x})\n"
                        except: pass
            
            self.parser_output.insert(tk.END, analysis + "-"*40 + "\n")
        except Exception as e:
            self.parser_output.insert(tk.END, f"Failed to parse {hex_data}: {e}\n")

if __name__ == "__main__":
    root = tk.Tk()
    app = BZRNetProbe(root)
    root.mainloop()
