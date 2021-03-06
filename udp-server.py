import socket
from collections import deque
from header import Header


class Server:
    def __init__(self, addr='127.0.0.1', port=8000, r_wnd_size=4):
        self.buf_size = 1024
        self.sock = socket.socket(family=socket.AF_INET,
                                  type=socket.SOCK_DGRAM)
        self.sock.bind((addr, port))
        self.receive_wind = set()

    def strip_header(self, pack):
        # network = big endian
        ACK_NO = int.from_bytes(pack[0:4], byteorder='big')
        SEQ_NO = int.from_bytes(pack[4:8], byteorder='big')
        FLAGS = pack[8]
        rwnd_size = int.from_bytes(pack[9:13], byteorder='big', signed=True)
        data = pack[13:]

        return data, ACK_NO, SEQ_NO, FLAGS, rwnd_size

    def handle_request(self, req):
        req, *_ = self.strip_header(req)
        return Header(FLAGS=b'\x11').return_header() + str.encode("I wanna be a cowboy, baby", encoding='ascii')

    def handle_ACK(self):
        pass

    def run(self):

        while(True):
            bytes_addr_pair = self.sock.recvfrom(
                self.buf_size)  # recvfrom has IP address
            # todo, fork a process for each client
            req, address = bytes_addr_pair

            data = self.handle_request(req)

            if(data != -1):
                self.sock.sendto(data, address)
            else:
                handle_ACK()


def main():
    server = Server()
    server.run()


if __name__ == '__main__':
    main()
