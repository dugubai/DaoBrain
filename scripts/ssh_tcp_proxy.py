"""可选的SSH透明TCP通道；仅用于本机OpenSSH/nc的Bad file descriptor故障。

ProxyCommand使用：python3 /绝对路径/DaoBrain/scripts/ssh_tcp_proxy.py %h %p
它不读取SSH密钥；认证与主机指纹校验仍由SSH完成，不修改全局配置。
"""
import os
import select
import socket
import sys


def main():
    if len(sys.argv) != 3:
        raise SystemExit("需要主机和端口两个参数")
    sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=20)
    sock.settimeout(None)
    stdin_open = True
    try:
        while True:
            # stdin关闭后仍接收远端输出，避免tar/命令管道提前丢失返回结果。
            readable, _, _ = select.select([sock, 0] if stdin_open else [sock], [], [])
            if sock in readable:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                remaining = memoryview(chunk)
                while remaining:
                    remaining = remaining[os.write(1, remaining):]
            if stdin_open and 0 in readable:
                chunk = os.read(0, 65536)
                if chunk:
                    sock.sendall(chunk)
                else:
                    stdin_open = False
                    sock.shutdown(socket.SHUT_WR)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
