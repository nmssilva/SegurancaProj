# encoding: utf-8
#
# jpbarraca@ua.pt
# jmr@ua.pt 2016

# vim setings:
# :set expandtab ts=4

from socket import *
from select import *
import json
import sys
import time
import logging
import CipherHelper
import base64

# Server address
HOST = ""   # All available interfaces
PORT = 8080  # The server port

BUFSIZE = 512 * 1024
TERMINATOR = "\n\n"
MAX_BUFSIZE = 64 * 1024

STATE_NONE = 0
STATE_CONNECTED = 1
STATE_DISCONNECTED = 2

#place to store the latest generated key pair.
KEYS = None

# server supported cipher combinations in order of preference
CIPHERS = ["ECDHE-RSA-AES256-CTR-SHA256",
           "ECDHE-RSA-AES256-OFB-SHA256",
           "ECDHE-RSA-AES256-CFB8-SHA256",
           "ECDHE-RSA-AES256-CTR-SHA512",
           "ECDHE-RSA-AES256-OFB-SHA512",
           "ECDHE-RSA-AES256-CFB8-SHA512"
           ]

class CipherData:
    def __init__(self, chipherSpec):
        self.cipherSpec = chipherSpec
        self.my_private_key = None
        self.my_public_key = None
        self.peer_public_key = None
        self.sharedKey = None
        self.iv = None

class Client:
    count = 0

    def __init__(self, socket, addr):
        self.socket = socket
        self.bufin = ""
        self.bufout = ""
        self.addr = addr
        self.id = None
        self.cd = None
        self.level = 0
        self.state = STATE_NONE
        self.name = "Unknown"

    def __str__(self):
        """ Converts object into string.
        """
        return "Client(id=%r addr:%s name:%s level:%d state:%d)" % (self.id, str(self.addr), self.name, self.level, self.state)

    def asDict(self):
        return {'id': self.id, 'level': self.level, 'name': self.name}

    def setState(self, state):
        if state not in [STATE_CONNECTED, STATE_NONE, STATE_DISCONNECTED]:
            return

        self.state = state

    def parseReqs(self, data):
        """Parse a chunk of data from this client.
        Return any complete requests in a list.
        Leave incomplete requests in the buffer.
        This is called whenever data is available from client socket."""
        if len(self.bufin) + len(data) > MAX_BUFSIZE:
            logging.error("Client (%s) buffer exceeds MAX BUFSIZE. %d > %d", 
                (self, len(self.bufin) + len(data), MAX_BUFSIZE))
            self.bufin = ""

        self.bufin += data
        reqs = self.bufin.split(TERMINATOR)
        print "REQUESTS: "
        print reqs
        self.bufin = reqs[-1]
        return reqs[:-1]

    def send(self, obj):
        """Send an object to this client.
        """
        try:
            self.bufout += json.dumps(obj) + "\n\n"
        except:
            # It should never happen! And not be reported to the client!
            logging.exception("Client.send(%s)", self)

    def close(self):
        """Shuts down and closes this client's socket.
        Will log error if called on a client with closed socket.
        Never fails.
        """
        logging.info("Client.close(%s)", self)
        try:
            # Shutdown will fail on a closed socket...
            self.socket.close()
        except:
            logging.exception("Client.close(%s)", self)

        logging.info("Client Closed")


class ChatError(Exception):
    """This exception should signal a protocol error in a client request.
    It is not a server error!
    It just means the server must report it to the sender.
    It should be dealt with inside handleRequest.
    (It should allow leaner error handling code.)
    """
    pass


def ERROR(msg):
    """Raise a Chat protocol error."""
    raise ChatError(msg)


class Server:
    def __init__(self, host, port):
        self.ss = socket(AF_INET, SOCK_STREAM)  # the server socket (IP \ TCP)
        self.ss.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self.ss.bind((host, port))
        self.ss.listen(10)
        logging.info("Secure IM server listening on %s", self.ss.getsockname())
        # clients to manage (indexed by socket and by name):
        self.clients = {}       # clients (key is socket)
        self.id2client = {}   # clients (key is id)

    def stop(self):
        """ Stops the server closing all sockets
        """
        logging.info("Stopping Server")
        try:
            self.ss.close()
        except:
            logging.exception("Server.stop")

        for csock in self.clients:
            try:
                self.clients[csock].close()  # Client.close!
            except:
                # this should not happen since close is protected...
                logging.exception("clients[csock].close")

        # If we delClient instead, the following would be unnecessary...
        self.clients.clear()
        self.id2client.clear()

    def addClient(self, csock, addr):
        """Add a client connecting in csock."""
        if csock in self.clients:
            logging.error("Client NOT Added: %s already exists", self.clients[csock])
            return

        client = Client(csock, addr)
        self.clients[client.socket] = client
        logging.info("Client added: %s", client)

    def delClient(self, csock):
        """Delete a client connected in csock."""
        if csock not in self.clients:
            logging.error("Client NOT deleted: %s not found", self.clients[csock])
            return

        client = self.clients[csock]
        assert client.socket == csock, "client.socket (%s) should match key (%s)" % (client.socket, csock)
        if client.id in self.id2client.keys():
            del self.id2client[client.id]
        del self.clients[client.socket]
        client.close()
        logging.info("Client deleted: %s", client)

    def accept(self):

        """Accept a new connection.
        """
        try:
            csock, addr = self.ss.accept()
            self.addClient(csock, addr)
        except:
            logging.exception("Could not accept client")

    def flushin(self, s):
        """Read a chunk of data from this client.
        Enqueue any complete requests.
        Leave incomplete requests in buffer.
        This is called whenever data is available from client socket.
        """
        client = self.clients[s]
        data = None
        try:
            data = s.recv(BUFSIZE)
            logging.info("Received data from %s. Message:\n%r", client, data)
        except:
            logging.exception("flushin: recv(%s)", client)
            logging.error("Received invalid data from %s. Closing", client)
            self.delClient(s)
        else:
            if len(data) > 0:
                reqs = client.parseReqs(data)
                for req in reqs:
                    self.handleRequest(s, req)
            else:
                self.delClient(s)

    def flushout(self, s):
        """Write a chunk of data to client.
        This is called whenever client socket is ready to transmit data."""
        if s not in self.clients:
            # this could happen before, because a flushin might have deleted the client
            logging.error("BUG: Flushing out socket that is not on client list! Socket=%s", str(s))
            return

        client = self.clients[s]
        try:
            sent = client.socket.send(client.bufout[:BUFSIZE])
            logging.info("Sent %d bytes to %s. Message:\n%r", sent, client, client.bufout[:sent])
            client.bufout = client.bufout[sent:]  # leave remaining to be sent later
        except:
            logging.exception("flushout: send(%s)", client)
            # logging.error("Cannot write to client %s. Closing", client)
            self.delClient(client.socket)

    def loop(self):
        while True:
            # sockets to select for reading: (the server socket + every open client connection)
            rlist = [self.ss] + self.clients.keys()
            # sockets to select for writing: (those that have something in bufout)
            wlist = [ sock for sock in self.clients if len(self.clients[sock].bufout)>0 ]
            logging.debug("select waiting for %dR %dW %dX", len(rlist), len(wlist), len(rlist))
            (rl, wl, xl) = select(rlist, wlist, rlist)
            logging.debug("select: %s %s %s", rl, wl, xl)

            # Deal with incoming data:
            for s in rl:
                if s is self.ss:
                    self.accept()
                elif s in self.clients:
                    self.flushin(s)
                else:
                    logging.error("Incoming, but %s not in clients anymore", s)

            # Deal with outgoing data:
            for s in wl:
                if s in self.clients:
                    self.flushout(s)
                else:
                    logging.error("Outgoing, but %s not in clients anymore", s)

            for s in xl:
                logging.error("EXCEPTION in %s. Closing", s)
                self.delClient(s)

    def handleRequest(self, s, request):
        """Handle a request from a client socket.
        """
        client = self.clients[s]
        try:
            logging.info("HANDLING message from %s: %r", client, repr(request))

            try:
                req = json.loads(request)
            except:
                return

            if not isinstance(req, dict):
                return

            if 'type' not in req:
                return

            if req['type'] == 'ack':
                return  # Ignore for now

            client.send({'type': 'ack'})

            if req['type'] == 'connect':
                self.processConnect(client, req)
            elif req['type'] == 'secure':
                self.processSecure(client, req)

        except Exception, e:
            logging.exception("Could not handle request")

    def clientList(self):
        """
        Return the client list
        """
        cl = []
        for k in self.clients:
            cl.append(self.clients[k].asDict())
        return cl

    def processConnect(self, sender, request):
        """
        Process a connect message from a client
        """
        if sender.state == STATE_CONNECTED:
            logging.warning("Client is already connected: %s" % sender)
            return

        if not all (k in request.keys() for k in ("name", "ciphers", "phase", "id")):
            logging.warning("Connect message with missing fields")
            return

        msg = {'type': 'connect', 'phase': request['phase'] + 1, 'ciphers': CIPHERS}

        if len(request['ciphers']) == 0:
            logging.info("Client did not provide any list of cipherspec")
            logging.info("Connect continue to phase " + str(msg['phase']))
            sender.send(msg)
            return

        if 'data' in request.keys():
            self.id2client[request['id']] = sender
            sender.id = request['id']
            sender.name = request['name']
            sender.state = STATE_CONNECTED
            sender.cd.peer_public_key = CipherHelper.deserializeKey(str(request['data']))
            sender.cd.sharedKey = CipherHelper.exchangeSecret(sender.cd.my_private_key,
                                                                   sender.cd.peer_public_key)
            logging.info("Client %s Connected" % request['id'])
            return

        # if len(request['ciphers']) == 1:
        #     print str(request['ciphers'][0])
        #     if str(request['ciphers'][0]) not in CIPHERS:
        #         logging.info("Cipherspec provided is not supported by the server")
        #         logging.info("Connect continue to phase " + str(msg['phase']))
        #         msg['ciphers'] = []
        #         sender.send(msg)
        #         return

        # client send more than one cipher spec
        for cipher in request['ciphers']:
            print cipher
            print type(cipher)
            str(cipher)
            print type(cipher)
            if str(cipher) in CIPHERS:
                print "123456789"
                msg['ciphers'] = [cipher]
                logging.info("Cipher spec agreement reached.\nGenerating keys.\nSending information to Client")
                sender.cd = CipherData(cipher)
                sender.cd.my_private_key, sender.cd.my_public_key = CipherHelper.generateKeyPair('ECDHE')
                msg['data'] = sender.cd.my_public_key
                logging.info("Connect continue to phase " + str(msg['phase']))
                sender.send(msg)
                return

        # No cipherspec supported in both sides
        msg['ciphers'] = []
        sender.send(msg)


        #original code
        # if len(request['ciphers']) > 1 or 'NONE' not in request['ciphers']:
        #     logging.info("Connect continue to phase " + str(msg['phase']))
        #     sender.send(msg)
        #     return
        #
        #
        # self.id2client[request['id']] = sender
        # sender.id = request['id']
        # sender.name = request['name']
        # sender.state = STATE_CONNECTED
        # sender.sa_data = request['ciphers']
        # logging.info("Client %s Connected" % request['id'])

    def processList(self, sender, request):
        """
        Process a list message from a client
        """
        if sender.state != STATE_CONNECTED:
            logging.warning("LIST from disconnected client: %s" % sender)
            return
        list = {'type': 'list', 'data': self.clientList()}
        encapsulatedMessage = self.encapsulateSecure(sender, list)
        return sender.send(encapsulatedMessage)


    def processSecure(self, sender, request):
        """
        Process a secure message from a client
        """
        if sender.state != STATE_CONNECTED:
            logging.warning("SECURE from disconnected client: %s" % sender)
            return

        if 'payload' not in request:
            logging.warning("Secure message with missing fields")
            return

        # This is a secure message.
        # TODO: Inner message is encrypted for us. Must decrypt and validate.

        # Update peer public key
        sender.cd.peer_public_key = CipherHelper.deserializeKey(str(request['sa-data']['public-key']))
        sender.cd.sharedKey = CipherHelper.exchangeSecret(sender.cd.my_private_key,
                                                               sender.cd.peer_public_key)

        # Verify HMAC from secure message
        rcHMAC = request['sa-data']['hash']
        if not CipherHelper.checkHMAC(sender, request, rcHMAC):
            logging.error("Integrity Validation failed on Secure Message")
            return

        iv = base64.b64decode(request['sa-data']['iv'])
        payload = CipherHelper.decrypt(sender, request['payload'], iv)
        request['payload'] = json.loads(payload)
        if 'type' not in request['payload'].keys():
            logging.warning("Secure message without inner frame type")
            return

        if request['payload']['type'] == 'list':
            self.processList(sender, request['payload'])
            return

        if not all (k in request['payload'].keys() for k in ("src", "dst")):
            return

        dstId = int(request['payload']['dst'])
        if not dstId in self.id2client.keys():
            logging.warning("Message to unknown client: %s" % request['payload']['dst'])
            return

        dst = self.id2client[dstId]
        dst_message = self.encapsulateSecure(dst,(request['payload']))
        dst.send(dst_message)

    def encapsulateSecure(self,sender, message):

        # generate a new secret for each message sent
        sender.cd.my_private_key, sender.cd.my_public_key = CipherHelper.generateKeyPair('ECDHE')
        sender.cd.sharedKey = CipherHelper.exchangeSecret(sender.cd.my_private_key,
                                                               sender.cd.peer_public_key)

        cipherText, iv = CipherHelper.encrypt(sender, message)
        secure = {'type': 'secure', 'payload':cipherText}
        secure['sa-data'] = {'iv': iv,
                             'public-key' : sender.cd.my_public_key}

        hmac = CipherHelper.generateHMAC(sender, secure)
        secure['sa-data']['hash'] = hmac

        return secure


if __name__ == "__main__":
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])

    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, formatter=logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

    serv = None
    while True:
        try:
            logging.info("Starting Secure IM Server v1.0")
            serv = Server(HOST, PORT)
            serv.loop()
        except KeyboardInterrupt:
            serv.stop()
            try:
                logging.info("Press CTRL-C again within 2 sec to quit")
                time.sleep(2)
            except KeyboardInterrupt:
                logging.info("CTRL-C pressed twice: Quitting!")
                break
        except:
            logging.exception("Server ERROR")
            if serv is not (None):
                serv.stop()
            time.sleep(10)
