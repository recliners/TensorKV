import socket
import struct
import time


def main():
    # 创建 UDP，绑定到本地 50002 端口
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 50002))
    try:
        while True:
            data, addr = sock.recvfrom(1024)
            if len(data) >= 4:
                opcode = struct.unpack("!I", data[:4])[0]
                if opcode == 3:
                    print(
                        "Received packet with opcode 3 at "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
    finally:
        sock.close()


if __name__ == "__main__":
    main()
