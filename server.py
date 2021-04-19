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
import time
import os


def keySort(l: Packet):
    return l.header.SEQ_NO


class Server:
    def __init__(self, addr="127.0.0.1", port=8000, r_wnd_size=4):

        self.appl_data = []

        self.buf_size: int = 1024
        self.sock: socket.socket = socket.socket(
            family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
        self.sock.bind((addr, port))

        self.addr: str = addr
        self.port: int = port
        self.connectionState: ConnState = ConnState.NO_CONNECT

        self.temp_loc = None
        self.client_loc = None

        self.temp_buffer = deque([])
        self.rwnd_size = 5

        self.received_data_packets = SortedSet([], key=keySort)

        self.synack_packet_fails = 0
        self.finack = False

        self.SEQ_NO: int = randint(12, 1234)
        self.ACK_NO: int = 1
        # For send()
        self.has_receive_buffer: threading.Event = threading.Event()
        self.has_window_buffer: threading.Event = threading.Event()
        # self.appl_recv_wait = threading.Semaphore()
        self.acquired_window_buffer = threading.Lock()

        self.receive_packet_buffer = deque([])
        self.window_packet_buffer = deque([])
        # self.send_buffer: None | Packet = None

        self.client_loc: None | tuple

        self.recv_thread = threading.Thread(target=self.receive)

        # start a receive process
        self.recv_thread.start()
        self.process_packet_thread = threading.Thread(target=self.processPacketLoop)
        self.process_packet_thread.start()

    def fileTransfer(self, data):
        pack_len = PACKET_LENGTH

        if len(data) > PACKET_LENGTH:
            packets = [
                Packet(Header(), data[i : min(i + pack_len, len(data))])
                for i in range(0, len(data), pack_len)
            ]
            # print("Number of packets: ", len(packets))
        else:
            packets = [Packet(Header(), data)]

        for packet in packets:
            self.temp_buffer.append(packet)

        self.fillWindowBuffer()
        # print("temp, ", len(self.temp_buffer))

    """
    def packetReceive(self):
        self.appl_recv_wait.acquire()
        packet = self.received_data_packets[0]
        self.received_data_packets.remove(self.received_data_packets[0])
        return packet
    """

    def fillWindowBuffer(self):
        """
        Update window, manage time
        """
        self.acquired_window_buffer.acquire()
        while len(self.window_packet_buffer) < self.rwnd_size:
            if not self.temp_buffer:
                break
            packet = self.temp_buffer.popleft()

            self.SEQ_NO += 1
            seq_no = self.SEQ_NO.__str__()
            ack_no = self.ACK_NO.__str__()

            packet.header.SEQ_NO = int(seq_no)
            packet.header.ACK_NO = int(ack_no)

            self.window_packet_buffer.append(
                [packet, time.time(), PacketState.NOT_SENT]
            )
        self.acquired_window_buffer.release()
        logServer("NOTIFY WINDOW")
        self.has_window_buffer.set()

    def pushPacketToReceiveBuffer(self, packet: Packet, location: tuple):
        """
        Fills the buffer to send the packet
        """
        self.receive_packet_buffer.append((packet, location))
        self.has_receive_buffer.set()

    def processSinglePacket(self, packet: Packet, location: tuple):

        if self.connectionState != ConnState.CONNECTED:
            self.temp_loc = location
            self.tryConnect(packet)

        else:
            logServer(f"Got packet number: {0} of size {len(packet.data)} bytes.")
            self.processData(packet)

    def processPacketLoop(self):
        """
        Sends a packet
        """
        while True:
            if self.connectionState not in CONNECTED_STATES:
                # Connection Packet?
                logServer("Waiting on receive buffer")
                self.has_receive_buffer.wait()
                logServer("Notified!")
                # Sanity check
                if self.receive_packet_buffer is None:
                    logServer(
                        f"Execption: send() tried with empty send_buffer or client_loc"
                    )
                    exit(1)
                packet, location = self.receive_packet_buffer.popleft()

                self.processSinglePacket(packet, location)

                self.has_receive_buffer.clear()

            elif self.connectionState != ConnState.FIN_WAIT:
                if (
                    not len(self.window_packet_buffer)
                    and self.connectionState == ConnState.CLOSE_WAIT
                ):
                    logServer(
                        f"Sending FIN_ACK packet: seq_no:{self.SEQ_NO} ack_no:{self.SEQ_NO}"
                    )
                    ackPacket = Packet(
                        Header(
                            ACK_NO=self.SEQ_NO, SEQ_NO=self.SEQ_NO, FLAGS=FINACK_FLAG
                        )
                    )
                    logServer("Changing state to FIN_WAIT")
                    logServer("Waiting for Final ACK for  client")
                    self.connectionState = ConnState.FIN_WAIT
                    self.sock.sendto(ackPacket.as_bytes(), self.client_loc)
                    timestamp = time.time()
                    attempt = 1
                    while not self.finack:
                        if time.time() - timestamp > PACKET_TIMEOUT:
                            logServer("Resending FINACK to client")
                            self.sock.sendto(ackPacket.as_bytes(), self.client_loc)
                            attempt += 1
                        if attempt >= MAX_FAIL_COUNT:
                            logServer(
                                "Client not responding :( Hope it has closed the connection."
                            )
                            logServer("Server closing the connection")
                            self.connectionState = ConnState.NO_CONNECT
                            break

                elif len(self.window_packet_buffer) == 0:
                    logServer("Waiting on window")
                    self.has_window_buffer.wait()
                    logServer("Waited on window")

                i = 0
                while i < len(self.window_packet_buffer):
                    self.acquired_window_buffer.acquire()
                    packet, timestamp, status = self.window_packet_buffer[i]

                    if status == PacketState.NOT_SENT:
                        self.window_packet_buffer[i][2] = PacketState.SENT
                        logServer(
                            f"Sending Packet with SEQ#{packet.header.SEQ_NO} to client"
                        )
                        self.sock.sendto(packet.as_bytes(), self.client_loc)

                    elif status == PacketState.SENT:
                        if time.time() - timestamp > PACKET_TIMEOUT:
                            logServer(
                                f"Resending Packet with SEQ#{packet.header.SEQ_NO} to client"
                            )
                            self.sock.sendto(packet.as_bytes(), self.client_loc)
                            self.window_packet_buffer[i][1] = time.time()

                    elif status == PacketState.ACKED and i == 0:
                        self.window_packet_buffer.popleft()
                        i -= 1
                        self.slideWindow()
                    self.acquired_window_buffer.release()
                    i += 1

    def updateWindow(self, packet):
        # Handle ack packet
        if packet.header.has_flag(ACK_FLAG):
            if self.connectionState == ConnState.FIN_WAIT:
                logServer("Received Final ACK from CLIENT. Bye-Bye!")
                self.finack = True
                self.connectionState = ConnState.NO_CONNECT
                logServer("Server is available again for new Connections!")
                return
            ack_num = packet.header.ACK_NO
            logServer(
                f"Received an ACK packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}"
            )
            if len(self.window_packet_buffer) != 0:
                self.acquired_window_buffer.acquire()
                base_seq = self.window_packet_buffer[0][0].header.SEQ_NO
                if ack_num > base_seq - 1:
                    index = ack_num - base_seq - 1
                    logServer(
                        f"Index: {index}, max index = {len(self.window_packet_buffer)-1}"
                    )
                    if index >= 0:
                        logServer(
                            f"Updating packet {self.window_packet_buffer[index][0].header.SEQ_NO} to ACK'd"
                        )
                        self.window_packet_buffer[index][2] = PacketState.ACKED
                self.acquired_window_buffer.release()

        elif packet.header.has_flag(FIN_FLAG):
            logServer("State changed to CLOSE_WAIT")
            self.connectionState = ConnState.CLOSE_WAIT
            logServer(
                f"Received a FIN packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}"
            )
            self.has_window_buffer.set()

        # Handle data packet
        else:
            base_seq = 0
            self.acquired_window_buffer.acquire()
            if len(self.window_packet_buffer) != 0:
                base_seq = self.window_packet_buffer[0][0].header.SEQ_NO
            self.acquired_window_buffer.release()
            if packet.header.ACK_NO >= base_seq + 1:
                self.received_data_packets.add(packet)
                if packet.header.SEQ_NO == self.ACK_NO + 1:
                    # self.appl_recv_wait.release()
                    self.ACK_NO += 1

            seq_no = packet.header.SEQ_NO
            logServer(
                f"Received a data packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}"
            )
            seq_no = packet.header.SEQ_NO

            logServer(f"Sending ACK packet: seq_no:{self.SEQ_NO} ack_no:{seq_no+1}")
            ackPacket = Packet(
                Header(ACK_NO=seq_no + 1, SEQ_NO=self.SEQ_NO, FLAGS=ACK_FLAG)
            )

            self.sock.sendto(ackPacket.as_bytes(), self.client_loc)

    def tryConnect(self, packet: Packet):
        if self.connectionState == ConnState.SYN:

            if packet.header.has_flag(SYN_FLAG):

                self.ACK_NO = packet.header.SEQ_NO + 1

                self.connectionState = ConnState.SYN
                logServer(
                    f"SYN_ACK of SEQ:{self.SEQ_NO} and ack:{self.ACK_NO} being sent to client at {self.temp_loc}"
                )

                synAckPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG)
                )
                self.sock.sendto(synAckPacket.as_bytes(), self.temp_loc)
                self.connectionState = ConnState.SYNACK

            else:
                logServer(
                    f"Expected SYN_FLAG with NO_CONNECT state, got {bytearray(packet.header.FLAGS).hex()} instead"
                )

        elif self.connectionState is ConnState.SYNACK:

            if packet.header.has_flag(ACK_FLAG):
                logServer(
                    f"Received an ACK packet of seq: {packet.header.SEQ_NO} and ACK {packet.header.ACK_NO}"
                )
                self.connectionState = ConnState.CONNECTED
                logServer(f"State changed to connected")
                self.SEQ_NO += 1
                self.client_loc = self.temp_loc

            elif packet.header.has_flag(SYN_FLAG):
                # self.connectionState = ConnState.SYN
                logServer(f"SYN_ACK again being sent to client at {self.temp_loc}")

                synAckPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG)
                )
                self.sock.sendto(synAckPacket.as_bytes(), self.temp_loc)
                self.connectionState = ConnState.SYNACK

            elif packet.header.has_flag(SYNACK_FLAG):
                logServer(f"SYN_ACK again being sent to client at {self.temp_loc}")
                self.sock.sendto(packet.as_bytes(), self.temp_loc)
                self.connectionState = ConnState.SYNACK

            else:
                logServer(
                    f"Expected ACK_FLAG with SYNACK state, got {bytearray(packet.header.FLAGS).hex()} instead"
                )
                logServer(f"SYN_ACK again being sent to client at {self.temp_loc}")
                synAckPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG)
                )
                self.sock.sendto(synAckPacket.as_bytes(), self.temp_loc)
                self.connectionState = ConnState.SYNACK
        else:
            logServer(f"Invalid state {self.connectionState}")

    def processData(self, packet):
        """
        Respond to a packet
        """
        pass

    def slideWindow(self):
        if len(self.temp_buffer) != 0:
            logServer("Adding packet to window")
            packet = self.temp_buffer.popleft()
            self.SEQ_NO += 1
            packet.header.SEQ_NO = self.SEQ_NO
            packet.header.ACK_NO = self.ACK_NO
            self.window_packet_buffer.append(
                [packet, time.time(), PacketState.NOT_SENT]
            )
            # logServer("NOTIFY WINDOW")
            self.has_window_buffer.set()
        else:
            self.has_window_buffer.clear()

    def handleHandshakeTimeout(self):
        """
        Timeouts for handshake, fin
        """
        if (
            self.connectionState not in CONNECTED_STATES
            and self.connectionState != ConnState.NO_CONNECT
        ):
            if (
                self.connectionState == ConnState.SYNACK
                or self.connectionState == ConnState.SYN
            ):
                logServer(f"Timed out recv , cur state {self.connectionState}")
                self.synack_packet_fails += 1

                if self.synack_packet_fails >= MAX_FAIL_COUNT:
                    logServer(
                        f"Timed out recv when in state {self.connectionState}, expecting ACK. Giving up"
                    )
                    self.temp_loc = None
                    self.connectionState = ConnState.NO_CONNECT
                else:
                    synAckPacket = Packet(
                        Header(
                            SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYNACK_FLAG
                        )
                    )
                    self.pushPacketToReceiveBuffer(synAckPacket, self.temp_loc)
            else:
                logServer(f"Invalid state at timeout: {self.connectionState}")
                exit(1)
        else:
            # FIN ...
            pass

    def receive(self):
        logServer(f"Listening for connections on port {self.port}")
        while True:
            try:
                if self.connectionState not in CONNECTED_STATES:
                    self.sock.settimeout(SOCKET_TIMEOUT)
                else:
                    self.sock.settimeout(None)

                message, location = self.sock.recvfrom(self.buf_size)
                if message is not None:
                    packet = Packet(message)
                    message = None

                if self.connectionState not in CONNECTED_STATES:
                    if packet is not None:
                        if packet.header.has_flag(SYN_FLAG):
                            self.connectionState = ConnState.SYN
                        else:
                            self.connectionState = ConnState.SYNACK

                        self.pushPacketToReceiveBuffer(packet, location)
                        packet = None
                else:
                    self.updateWindow(packet)

            except socket.timeout as e:
                self.handleHandshakeTimeout()


if __name__ == "__main__":
    setupLogging()
    serv = Server()
    while serv.connectionState != ConnState.CONNECTED:
        pass
    # print("Hey: ")
    a = b""
    Start = time.time()
    """
    with open("client.py", "rb") as f:
        f.seek(0, os.SEEK_END)
        a += f.tell().to_bytes(16, byteorder="big")
        with open("servsize", "w") as f2:
            f2.write(str(f.tell()))
        serv.fileTransfer(a)

    time.sleep(10)
    """
    """
    a = b""
    with open("client.py", "rb") as f:
        data = f.read()
        print(len(data))
        serv.fileTransfer(data)
    """
    size = 1650442
    byte = 0
    # poll till it's done
    while len(serv.received_data_packets) * PACKET_LENGTH < size:
        print(len(serv.received_data_packets))
        time.sleep(1)
        pass
    End = time.time()
    print(f"DONE: {End-Start}")
    with open("f2", "wb") as f:
        while byte < size:
            packet = serv.received_data_packets[0]
            serv.received_data_packets.remove(serv.received_data_packets[0])
            byte += len(packet.data)
            # print(f"bytes: {byte}")
            f.write(packet.data)
    # serv.close()
