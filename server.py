
import socket
import select
import threading
from random import randint
from threading import Timer
from sortedcontainers import SortedSet
from header import Header, Packet
from lib import logServer, setupLogging
from config import *
from collections import deque


def keySort(l: Packet):
    return l.header.SEQ_NO


class Server:
    def __init__(self, addr="127.0.0.1", port=8000, r_wnd_size=4):
        self.buf_size: int = 1024
        self.sock: socket.socket = socket.socket(
            family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.sock.bind((addr, port))

        self.addr: str = addr
        self.port: int = port
        self.connectionState: ConnState = ConnState.NO_CONNECT

        self.temp_loc = None
        self.client_loc = None

        self.received_data_packets = SortedSet([], key=keySort)

        self.synack_packet_fails = 0

        self.SEQ_NO: int = randint(12, 1234)
        self.ACK_NO: int = 1
        # For send()
        self.has_receive_buffer: threading.Event = threading.Event()
        self.has_window_buffer: threading.Event = threading.Event()

        self.receive_packet_buffer = deque([])
        self.window_packet_buffer = deque([])
        # self.send_buffer: None | Packet = None

        self.client_loc: None | tuple

        self.recv_thread = threading.Thread(
            target=self.receive)

        # start a receive process
        self.recv_thread.start()
        self.processPacketLoop()

    def fillWindowBuffer(self):
        '''
         Update window, manage time
        '''

        while (len(self.window_packet_buffer) < self.rwnd_size):
            if(not self.temp_buffer):
                break
            packet = self.temp_buffer.popleft()

            self.SEQ_NO += 1
            seq_no = self.SEQ_NO.__str__()
            ack_no = self.ACK_NO.__str__()
            logClient(self.SEQ_NO)

            packet.header.SEQ_NO = int(seq_no)
            packet.header.ACK_NO = int(ack_no)

            self.window_packet_buffer.append(
                [packet, time.time(), PacketState.NOT_SENT])
        logClient("NOTIFY WINDOW")
        self.has_window_buffer.set()

    def pushPacketToReceiveBuffer(self, packet: Packet, location: tuple):
        '''
            Fills the buffer to send the packet
        '''
        self.receive_packet_buffer.append((packet, location))
        self.has_receive_buffer.set()

    def processSinglePacket(self, packet: Packet, location: tuple):

        if self.connectionState is not ConnState.CONNECTED:
            self.temp_loc = location
            self.tryConnect(packet)

        else:
            logServer(
                f"Got packet number: {0} of size {len(packet.data)} bytes.")
            self.processData(packet)

    def processData(self, packet):
        '''
            Respond to a packet
        '''
        pass

    def updateWindow(self, packet):
        # Handle ack packet
        if packet.header.has_flag(ACK_FLAG):

            ack_num = packet.header.ACK_NO
            logServer(
                f"Received an ACK packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}")
            if len(self.window_packet_buffer) != 0:

                base_ack = self.window_packet_buffer[0][0].header.ACK_NO
                index = ack_num - base_ack
                self.window_packet_buffer[index][0].status = ACK

                if index == 0:
                    self.window_packet_buffer.popleft()
                    self.slideWindow()

            if len(self.window_packet_buffer) == 0:
                self.has_window_buffer.clear()

        # Handle data packet
        else:
            self.received_data_packets.add(packet)
            seq_no = packet.header.SEQ_NO
            logServer(
                f"Received a data packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}")

            logServer(
                f"Sending ACK packet: seq_no:{self.SEQ_NO} ack_no:{seq_no+1}")
            ackPacket = Packet(Header(ACK_NO=seq_no+1,
                                      SEQ_NO=self.SEQ_NO,
                                      FLAGS=ACK_FLAG))

            self.sock.sendto(ackPacket.as_bytes(), self.client_loc)

    def processPacketLoop(self):
        """
        Sends a packet
        """
        while True:
            if self.connectionState != ConnState.CONNECTED:
                # Connection Packet?
                logServer("Waiting on receive buffer")
                self.has_receive_buffer.wait()
                logServer("Notified!")
                # Sanity check
                if self.receive_packet_buffer is None:
                    logServer(
                        f"Execption: send() tried with empty send_buffer or client_loc")
                    exit(1)

                packet, location = self.receive_packet_buffer.popleft()

                self.processSinglePacket(packet, location)

                self.has_receive_buffer.clear()

            else:

                if not self.window_packet_buffer:
                    logServer("Waiting on window")
                    self.has_window_buffer.wait()

                for i in range(0, len(self.window_packet_buffer)):
                    packet, timestamp, status = self.window_packet_buffer[i]

                    if status == PacketState.NOT_SENT:
                        self.window_packet_buffer[i][2] = PacketState.SENT
                        logClient(
                            f"Sending Packet with SEQ#{packet.header.SEQ_NO} to server")
                        self.sock.sendto(packet.as_bytes(), self.client_loc)

                    elif status == PacketState.SENT:
                        if time.time() - timestamp > PACKET_TIMEOUT:
                            logClient(
                                f"Resending Packet with SEQ#{packet.header.SEQ_NO} to server")
                            self.sock.sendto(
                                packet.as_bytes(), self.client_loc)
                            self.window_packet_buffer[i][1] = time.time()

    def tryConnect(self, packet: Packet):
        if self.connectionState is ConnState.NO_CONNECT:

            if packet.header.has_flag(SYN_FLAG):

                self.ACK_NO = packet.header.SEQ_NO+1

                self.connectionState = ConnState.SYN
                logServer(
                    f"SYN_ACK being sent to client at {self.temp_loc}")

                synAckPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG))
                self.sock.sendto(synAckPacket.as_bytes(), self.temp_loc)
                self.connectionState = ConnState.SYNACK

            else:
                logServer(
                    f"Expected SYN_FLAG with NO_CONNECT state, got {packet.header.FLAGS} instead")

        elif self.connectionState is ConnState.SYNACK:

            if packet.header.has_flag(ACK_FLAG):
                self.connectionState = ConnState.CONNECTED
                logServer(f"State changed to connected")
                self.SEQ_NO += 1
                self.client_loc = self.temp_loc

            elif packet.header.has_flag(SYN_FLAG):
                self.connectionState = ConnState.SYN
                logServer(
                    f"SYN_ACK again being sent to client at {self.client_loc}")

                synAckPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG))
                self.sock.sendto(packet.as_bytes(), self.client_loc)
                self.connectionState = ConnState.SYNACK
            else:
                logServer(
                    f"Expected ACK_FLAG with SYNACK state, got {packet.header.FLAGS} instead")
        else:
            logServer(f"Invalid state {self.connectionState}")

    def handleHandshakeTimeout(self):
        '''
            Timeouts for handshake, fin
        '''
        if self.connectionState is not ConnState.CONNECTED:
            if self.connectionState == ConnState.SYNACK:
                logClient(f"Timed out recv , cur state {self.connectionState}")
                self.synack_packet_fails += 1

                if self.synack_packet_fails >= MAX_FAIL_COUNT:
                    logServer(
                        f"Timed out recv when in state {self.connectionState}, expecting ACK. Giving up")
                    self.temp_loc = None
                    self.connectionState == ConnState.NO_CONNECT
                else:
                    synPacket = Packet(
                        Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG))
                    self.pushPacketToReceiveBuffer(synackPacket, location)
            else:
                logServer(f"Invalid state: {self.connectionState}")
                exit(1)
        else:
            # FIN ...
            pass

    def receive(self):
        logServer(f"Listening for connections on port {self.port}")
        while True:
            '''
            try:
                if(self.connectionState not in [ConnState.NO_CONNECT, ConnState.CONNECTED]):
                    self.sock.settimeout(SOCKET_TIMEOUT)
                else:
                    self.sock.settimeout(None)
                bytes_addr_pair = self.sock.recvfrom(self.buf_size)
                req, location = bytes_addr_pair
                packet = Packet(req)
                self.pushPacketToReceiveBuffer(packet, location)
            except socket.timeout as e:
                self.handleHandshakeTimeout()
            '''
            try:
                if(self.connectionState == ConnState.SYNACK):
                    self.sock.settimeout(SOCKET_TIMEOUT)
                else:
                    self.sock.settimeout(None)

                message, location = self.sock.recvfrom(self.buf_size)
                if(message is not None):
                    packet = Packet(message)
                    message = None

                if self.connectionState != ConnState.CONNECTED:
                    self.pushPacketToReceiveBuffer(packet, location)
                else:
                    self.updateWindow(packet)

            except socket.timeout as e:
                self.handleHandshakeTimeout()


if __name__ == "__main__":
    setupLogging()
    Server()
