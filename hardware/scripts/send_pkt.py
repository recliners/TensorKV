
import os, struct
from scapy.all import *

pkt_id = 332

def construct_packet():
    global pkt_id
    # 递增数据包的 ID (IP 报头中使用)
    pkt_id += 1
    ether_layer = Ether(src="08:c0:eb:24:68:6b", dst="08:c0:eb:24:7b:8b")
    ip_layer = IP(src="192.168.1.1", dst="192.168.1.2", id=pkt_id)
    udp_layer = UDP(sport=1540, dport=5451)
    raw_data = "1234567890"
    raw_layer = Raw(load=raw_data)
    packet = ether_layer / ip_layer / udp_layer / raw_layer
    return packet

for _ in range(1):
    pkt = construct_packet()
    sendp(pkt, iface="enp5s0f1np1", verbose=False)
