import socket
from header import Header
import multiprocessing
from lib import logClient, logServer, setupLogging
from config import *
from threading import Timer
import threading
import time
from random import randint
from header import Header, Packet
from collections import deque
import copy
from sortedcontainers import SortedSet


def keySort(l: Packet):
    return l.header.SEQ_NO


class Client:
    def __init__(self, serv_addr="127.0.0.1", serv_port=8000):
        self.sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)

        self.rwnd_size = 10
        self.buf_size = 1024
        self.server_loc = (serv_addr, serv_port)

        self.connectionState: ConnState = ConnState.NO_CONNECT

        # use this buffer when a packet has been received, which requires to be sent
        self.receive_packet_buffer = deque([])
        # temp buffer for application to send packet
        self.temp_buffer = deque([])
        # the window
        self.window_packet_buffer = deque([])

        self.has_receive_buffer = threading.Event()

        self.has_window_buffer = threading.Event()
        self.acquired_window_buffer = threading.Lock()
        # self.appl_recv_wait = threading.Semaphore(0)

        self.ack_packet_fails = 0
        self.finack = False

        self.received_data_packets = SortedSet([], key=keySort)

        self.can_proceed_fin = threading.Event()
        self.can_proceed_fin.set()  # Setting initial value to True
        # Current sequence number stored by the client
        self.SEQ_NO = randint(1, 2536)

        # Current ack number stored by the client
        self.ACK_NO = 1

        self.BASE_RECEIVED_PACKET_SEQ_NO = -1

        self.process_packet_thread = threading.Thread(target=self.processPacketLoop)

        self.process_packet_thread.start()

        self.sendConnection()
        self.receive_thread = threading.Thread(target=self.receiveLoop)
        self.receive_thread.start()

    def sendConnection(self):
        """
        Send a SYN packet to server
        """

        logClient("Starting client!")
        initialSynPacket = Packet(
            Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYN_FLAG)
        )
        self.connectionState = ConnState.SYN
        self.sock.sendto(initialSynPacket.as_bytes(), self.server_loc)

    def fileTransfer(self, data):
        self.can_proceed_fin.clear()  # To enforce fin only after complete transfer
        pack_len = PACKET_LENGTH

        # for i in range(0, len(data), pack_len):
        #    print(i)

        if len(data) > PACKET_LENGTH:
            packets = [
                Packet(Header(), data[i : min(i + pack_len, len(data))])
                for i in range(0, len(data) + 1, pack_len)
            ]
            # print("Number of packets: ", len(packets))
        else:
            packets = [Packet(Header(), data)]

        print("len: ", len(packets))
        for packet in packets:
            self.temp_buffer.append(packet)

        self.fillWindowBuffer()
        # print("temp, ", len(self.temp_buffer))

    """
    def packetReceive(self, size):
        logServer("APPLICATION: Waiting on packet")
        self.appl_recv_wait.acquire()
        logServer("APPLICATION: Cleared packet")
        packet = self.received_data_packets[0]
        self.received_data_packets.remove(self.received_data_packets[0])
        return packet
    """

    def slideWindow(self):
        if len(self.temp_buffer) != 0:
            logClient("Adding packet to window")
            packet = self.temp_buffer.popleft()
            self.SEQ_NO += 1
            packet.header.SEQ_NO = self.SEQ_NO
            packet.header.ACK_NO = self.ACK_NO
            self.window_packet_buffer.append(
                [packet, time.time(), PacketState.NOT_SENT]
            )
            logClient("NOTIFY WINDOW")
            self.has_window_buffer.set()
            return True
        else:
            logClient("CANT SLIDE")
            self.has_window_buffer.clear()
            return False

    def fillWindowBuffer(self):
        """
        Update window, manage time
        """
        print("here")
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
        logClient("Notify WINDOW")
        self.has_window_buffer.set()

    def pushPacketToReceiveBuffer(self, packet: Packet):
        """
        push a packet to the "received" buffer
        """
        print(self.connectionState)
        logClient(
            f"found packet in buffer, with flags: {bytearray(packet.header.FLAGS).hex()}"
        )
        self.receive_packet_buffer.append(packet)
        self.has_receive_buffer.set()

    def processSinglePacket(self, packet: Packet):

        if self.connectionState is not ConnState.CONNECTED:
            self.tryConnect(packet)
        else:
            self.processData(packet)

    def processPacketLoop(self):
        """
        main process
        """
        while self.connectionState != ConnState.CLOSED:
            if self.connectionState != ConnState.CONNECTED:
                # Connection Packet
                logClient("Waiting on receive buffer")
                self.has_receive_buffer.wait()
                # Sanity check
                if len(self.receive_packet_buffer) != 0:
                    self.processSinglePacket(self.receive_packet_buffer.popleft())
                self.has_receive_buffer.clear()
            else:

                self.acquired_window_buffer.acquire()
                if not self.window_packet_buffer:
                    self.acquired_window_buffer.release()
                    self.has_window_buffer.clear()
                    logClient("Waiting on window")
                    self.has_window_buffer.wait()
                else:
                    self.acquired_window_buffer.release()

                i = 0
                while i < len(self.window_packet_buffer):
                    # logClient("Waiting to acquire lock")

                    # logClient("Acquired!")
                    """
                    if(i >= len(self.window_packet_buffer)):
                        # logClient("Releasing lock")
                        # self.acquired_window_buffer.release()
                        break
                    """

                    self.acquired_window_buffer.acquire()
                    packet, timestamp, status = self.window_packet_buffer[i]

                    if status == PacketState.NOT_SENT:
                        self.window_packet_buffer[i][2] = PacketState.SENT
                        logClient(
                            f"Sending Packet with SEQ#{packet.header.SEQ_NO} to server"
                        )
                        self.sock.sendto(packet.as_bytes(), self.server_loc)
                    elif status == PacketState.SENT:
                        if time.time() - timestamp > PACKET_TIMEOUT:
                            logClient(
                                f"Resending Packet with SEQ#{packet.header.SEQ_NO} to server"
                            )
                            self.sock.sendto(packet.as_bytes(), self.server_loc)
                            self.window_packet_buffer[i][1] = time.time()

                    elif status == PacketState.ACKED and i == 0:
                        self.window_packet_buffer.popleft()
                        i -= 1
                        could_slide = self.slideWindow()
                        if not could_slide and not len(self.window_packet_buffer):
                            # Both temp buffer and window buffer are empty
                            self.can_proceed_fin.set()
                    self.acquired_window_buffer.release()
                    # logClient("Releasing lock")

                    i += 1

    def updateWindow(self, packet):
        # Handle synack packet
        if packet.header.has_flag(SYNACK_FLAG):
            self.has_window_buffer.set()  # To allow client to read from receive buffer
            self.tryConnect(packet)
            self.has_window_buffer.clear()

        elif packet.header.has_flag(FINACK_FLAG):
            logClient("Received FIN_ACK from server")
            self.finack = True

        elif packet.header.has_flag(ACK_FLAG):
            ack_num = packet.header.ACK_NO
            logClient(
                f"Received an ACK packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}"
            )
            if len(self.window_packet_buffer) != 0:
                self.acquired_window_buffer.acquire()
                logClient("acquired lock")
                base_seq = self.window_packet_buffer[0][0].header.SEQ_NO
                if ack_num > base_seq - 1:
                    index = ack_num - base_seq - 1
                    logClient(f"base window: {base_seq}, index:{index}")
                    if index >= 0:
                        logClient(
                            f"Updating packet {self.window_packet_buffer[index][0].header.SEQ_NO} to ACK'd"
                        )
                        self.window_packet_buffer[index][2] = PacketState.ACKED
                self.acquired_window_buffer.release()

        # Handle data packet
        else:
            base_seq = 0
            self.acquired_window_buffer.acquire()
            if len(self.window_packet_buffer) != 0:
                base_seq = self.window_packet_buffer[0][0].header.SEQ_NO
            self.acquired_window_buffer.release()
            if packet.header.ACK_NO >= base_seq + 1:
                self.received_data_packets.add(packet)
                if self.received_data_packets[0].header.SEQ_NO == self.ACK_NO + 1:
                    # self.appl_recv_wait.release()
                    self.ACK_NO += 1

            seq_no = packet.header.SEQ_NO
            logClient(
                f"Received a DATA packet of SEQ_NO:{packet.header.SEQ_NO} and ACK_NO: {packet.header.ACK_NO}"
            )
            logClient(f"Sending ACK packet: seq_no:{self.SEQ_NO} ack_no:{seq_no+1}")
            ackPacket = Packet(
                Header(ACK_NO=seq_no + 1, SEQ_NO=self.SEQ_NO, FLAGS=ACK_FLAG)
            )

            self.sock.sendto(ackPacket.as_bytes(), self.server_loc)

    def close(self):
        self.can_proceed_fin.wait()
        logClient("Finished Sending packets, Initiating FIN")
        initialFinPacket = Packet(
            Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=FIN_FLAG)
        )
        logClient("Sending FIN to Server")
        self.sock.sendto(initialFinPacket.as_bytes(), self.server_loc)
        self.connectionState = ConnState.FIN_WAIT
        logClient("Client State changed to FIN_WAIT")
        attempt = 1
        logClient("Waiting for server to send FIN_ACK")
        timestamp = time.time()
        while not self.finack:
            if time.time() - timestamp > PACKET_TIMEOUT:
                logClient(f"Resending FIN to server")
                self.sock.sendto(initialFinPacket.as_bytes(), self.server_loc)
                attempt += 1
            if attempt >= MAX_FAIL_COUNT:
                logClient("Server Down!! Terminating FIN and closing.")
                self.connectionState = ConnState.CLOSED
                return
        logClient("Sending FINAL ACK to server. Bye-Bye Server!")
        finalFinPacket = Packet(
            Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=ACK_FLAG)
        )
        self.sock.sendto(finalFinPacket.as_bytes(), self.server_loc)
        logClient("Closing Connection BYE!")
        self.connectionState = ConnState.CLOSED

    def tryConnect(self, packet):
        """
        Try to establish a connection or move across a connection state
        """

        if self.connectionState is ConnState.NO_CONNECT:  # WTF
            logClient(f"Invalid state: Not connected!\n Exitting!")
            exit(1)

        elif self.connectionState is ConnState.SYN:
            if packet.header.has_flag(SYNACK_FLAG):

                logClient(
                    f"Received a SYNACK packet with SEQ:{packet.header.SEQ_NO} and ACK:{packet.header.ACK_NO}"
                )
                self.ACK_NO = packet.header.SEQ_NO + 1
                self.SEQ_NO = packet.header.ACK_NO

                self.connectionState = ConnState.SYNACK

                logClient(
                    f"Sending an ACK packet of seq:{self.SEQ_NO} and ACK:{self.ACK_NO}"
                )
                ackPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=ACK_FLAG)
                )

                self.sock.sendto(ackPacket.as_bytes(), self.server_loc)
                self.connectionState = ConnState.CONNECTED

                logClient("Connection established (hopefully)")

            elif packet.header.has_flag(SYN_FLAG):

                logClient("Resending SYN Packet to server")
                initialSynPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYN_FLAG)
                )
                self.connectionState = ConnState.SYN
                self.sock.sendto(initialSynPacket.as_bytes(), self.server_loc)

            else:
                logClient(
                    f"Expecting SYNACK flag at state SYN, got {packet.header.FLAGS}"
                )
        else:
            if packet.header.has_flag(SYNACK_FLAG):

                logClient(
                    f"Received a SYNACK packet with SEQ:{packet.header.SEQ_NO} and ACK:{packet.header.ACK_NO}"
                )
                self.ACK_NO = packet.header.SEQ_NO + 1
                self.SEQ_NO = packet.header.ACK_NO

                self.connectionState = ConnState.SYNACK

                ackPacket = Packet(
                    Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=ACK_FLAG)
                )

                self.sock.sendto(ackPacket.as_bytes(), self.server_loc)
                self.connectionState = ConnState.CONNECTED
                self.has_receive_buffer.set()
                logClient("Connection established (again, hopefully)")

    def processData(self, packet):
        """
        Respond to a packet
        """
        pass

    """
    def slideWindow(self):
        if len(self.temp_buffer) != 0:
            self.window_packet_buffer.append(self.temp_buffer.popleft())
            return True
        else:
            self.has_window_buffer.clear()
            return False
    """

    def handleHandshakeTimeout(self):
        """
        Timeouts for handshake, fin
        """
        if self.connectionState is not ConnState.CONNECTED:
            if self.connectionState == ConnState.SYN:
                logClient(f"Timed out recv , cur state {self.connectionState}")
                self.ack_packet_fails += 1

                if self.ack_packet_fails >= MAX_FAIL_COUNT:
                    logClient(
                        f"Timed out recv when in state {self.connectionState}, expecting SYN_ACK. Giving up"
                    )
                    exit(1)
                else:
                    synPacket = Packet(
                        Header(SEQ_NO=self.SEQ_NO, ACK_NO=self.ACK_NO, FLAGS=SYN_FLAG)
                    )
                    self.pushPacketToReceiveBuffer(synPacket)
            else:
                logClient(f"Invalid state: {self.connectionState}")
                exit(1)
        else:
            # FIN ...
            pass

    def receiveLoop(self):
        """
        Receive a packet
        """
        packet = None
        while self.connectionState != ConnState.CLOSED:
            try:
                if (
                    self.connectionState not in CONNECTED_STATES
                    and self.connectionState != ConnState.CLOSED
                ):
                    self.sock.settimeout(SOCKET_TIMEOUT)
                else:
                    self.sock.settimeout(None)

                message = self.sock.recv(self.buf_size)
                if message is not None:
                    packet = Packet(message)
                    message = None
                if self.connectionState not in CONNECTED_STATES:
                    self.pushPacketToReceiveBuffer(packet)
                else:
                    self.updateWindow(packet)

                ## client specific stuff to avoid locks and a race condition
                if packet is not None:
                    if packet.header.has_flag(FINACK_FLAG):
                        break

            except socket.timeout as e:
                self.handleHandshakeTimeout()


if __name__ == "__main__":
    setupLogging()
    client = Client()
    while client.connectionState != ConnState.CONNECTED:
        pass
    # client.fileTransfer("ABCDEFG" * 1000)
    # time.sleep(0.1)
    # client.fileTransfer("A"*1000)
    # time.sleep(40)
    # client.close()
    # time.sleep(30)
    # print("gothere")
    a = ""
    """
    while not client.received_data_packets:
        pass
    """
    """
    packet = client.received_data_packets[0]
    client.received_data_packets.remove(client.received_data_packets[0])
    size = int.from_bytes(packet.data, byteorder="big")
    time.sleep(10)
    with open("size", "w") as f:
        f.write(str(size))
    """
    """
    size = 17725
    byte = 0
    # poll till it's done
    while len(client.received_data_packets) * PACKET_LENGTH < size:
        time.sleep(1)
        pass
    print("DONE")
    with open("f2", "wb") as f:
        while byte < size:
            packet = client.received_data_packets[0]
            client.received_data_packets.remove(client.received_data_packets[0])
            byte += len(packet.data)
            # print(f"bytes: {byte}")
            f.write(packet.data)
    client.close()
    """
    a = b""
    with open("../RealTimeOpt.pdf", "rb") as f:
        data = f.read()
        print(len(data))
        client.fileTransfer(data)

    while 1:
        print(len(client.window_packet_buffer), len(client.temp_buffer))
        time.sleep(1)
