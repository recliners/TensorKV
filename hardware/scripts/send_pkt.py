#!/usr/bin/env python3
import argparse
from scapy.all import Ether, IP, UDP, Raw, sendp

pkt_id = 332


def construct_packet(src_mac, dst_mac, src_ip, dst_ip):
    global pkt_id
    pkt_id += 1
    return (
        Ether(src=src_mac, dst=dst_mac)
        / IP(src=src_ip, dst=dst_ip, id=pkt_id)
        / UDP(sport=1540, dport=5451)
        / Raw(load="1234567890")
    )


def main():
    parser = argparse.ArgumentParser(description="Send a TensorKV probe packet.")
    parser.add_argument("--iface", default="enp5s0f1np1")
    parser.add_argument("--src-mac", default="08:c0:eb:24:68:6b")
    parser.add_argument("--dst-mac", default="08:c0:eb:24:7b:8b")
    parser.add_argument("--src-ip", default="192.168.1.1")
    parser.add_argument("--dst-ip", default="192.168.1.2")
    parser.add_argument("-n", type=int, default=1, help="number of packets")
    args = parser.parse_args()

    for _ in range(args.n):
        pkt = construct_packet(args.src_mac, args.dst_mac, args.src_ip, args.dst_ip)
        sendp(pkt, iface=args.iface, verbose=False)


if __name__ == "__main__":
    main()
