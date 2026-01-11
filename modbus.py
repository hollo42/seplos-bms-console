#!/usr/bin/env python

"""
Modbus reader.
This module deals with reading and writing modbus protocol (for now over serial, presumed with
a serial-->rs485 adapter of some sort, but could be made more generic in future).
"""

import serial
import asyncio
import serial_asyncio
import logging

log = logging.getLogger("modbus")

# --------------------------------------------------------------------------- #
# Modbus protocol is a request-response protocol. Each request/response is a
# series of bytes:
#    byte 0: Address (which client is expected to respond).
#    byte 1: Command type (read, write, etc.)
#    bytes: a series of bytes of message. Usually an address of register to
#           read or write, followed by some other data.
#    byte n-2, n-1: A checksum.
#
# Individual requests/responses are delineated by pauses in the data of at
# least 3.5 characters in length. This is c.1ms, which is difficult to
# determine reliably in a python script.
# Fortunately as the client we get to chose the rate of messages, so we go
# with much bigger gaps than this. Only sending every 0.5s seems to work fine
# paired with going for new frames if >0.1s gap.
# --------------------------------------------------------------------------- #
class ModBus:

    def __init__(self, port):
        self.port = port
        self.data = bytearray(0)
        self.trashdata = False
        self.trashdataf = bytearray(0)
        self.outbuf = [] # An array of bytearrays

    async def run(self, response_cb, timer_cb, freq):
        self.response_cb = response_cb
        self.timer_cb = timer_cb
        self.poll_freq = freq

        self.readstream, self.writestream = await serial_asyncio.open_serial_connection(url=self.port, baudrate=19200, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE)
        sender = self.flush_outbuf()
        reader = self.reader()

        await asyncio.gather(sender, reader, self.poll())

    # Simple API if you don't want to use asyncio. This connects the serial, and
    # sends any responses back to response_cb. timer_cb will be called every
    # freq seconds (can be used to poll the modbus).
    def run_with_callbacks(self, response_cb, timer_cb, freq):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.run(response_cb, timer_cb, freq))
        loop.run_forever()
        loop.close()

    # We expect addr and func to be a single byte. Args is a list of two byte ints to be sent in MSB first order.
    def send_modbus(self, addr, func, *args):
        ba = bytearray([addr, func])
        for a in args:
            ba.append( (a >> 8) & 0xFF)
            ba.append(a & 0xFF)
        self.send(ba)

    # Now we have our internals.
    # reader reads from modbus, sending completed frames that pass checksum out to our callback.
    async def reader(self):
        data = bytearray(0)
        while True:
            try:
                chunk = await asyncio.wait_for(self.readstream.read(63), timeout=0.1)
                data += chunk
            except asyncio.TimeoutError as te:
                # Timeout means we've completed the gap between frames and should have
                # a full response.
                if len(data)>0:
                    if self.check_crc(data):
                        if self.response_cb:
                            self.response_cb(self, data)
                    else:
                        print("Discarding data with crc failure", data.hex())
                    data = bytearray(0)

    # flush_outbuf throttles writes.
    async def flush_outbuf(self):
        while True:
            # See notes on reader() about timeouts.
            await asyncio.sleep(0.3) 
            if len(self.outbuf) > 0:
                self.writestream.write(self.outbuf.pop(0))
            if len(self.outbuf) > 5:
                log.warning(f"not keeping up with outgoing modbus messages. {len(self.outbuf)} queued.")

    # Calculate a Modbus RTU checksum for byte array ba, and append it to the end, LSB first.
    def append_crc(self, ba: bytearray) -> bytearray:
        crc = self.create_crc(ba)
        ba.append(crc & 0xFF)
        ba.append((crc >> 8) & 0xFF)
        return ba

    def check_crc(self, ba: bytearray) -> bool:
        if len(ba)<3:
            return False
        crc = self.create_crc(ba[0:-2])
        if crc & 0xFF != ba[-2]:
            return False
        if (crc >> 8) & 0xFF != ba[-1]:
            return False
        return True

    def create_crc(self, ba: bytearray) -> int:
        crc = 0xFFFF
        for i, x in enumerate(ba, start=0):
            crc = crc ^ x
            for j in range(8):
                #print(f"  0x{crc:02x} {crc:17b}")
                if crc & 0x01:
                    crc = crc >> 1
                    crc = crc ^ 0xA001
                else:
                    crc = crc >> 1
        return crc

    # Send bytearray down the wire. Should already have the checksum.
    def send_raw(self, ba: bytearray):
        self.outbuf.append(ba.copy())

    def send(self, ba: bytearray):
        if self.check_crc(ba):
            print("Ooops. sent ba already has crc")
        bacrc = ba.copy()
        self.append_crc(bacrc)
        if not self.check_crc(bacrc):
            print("Ooops. self generated crc doesn't check out");
        self.send_raw(bacrc)

    # Poll our external timer_cb at poll_freq intervals.
    async def poll(self):
        while True:
            await asyncio.sleep(self.poll_freq)
            self.timer_cb(self)

# Sample polling function, used for testing if we're called as main routine.
# This works with a seplos bms
def test_poll_cb(m):
    print("Sending requests for PIA, PIB and PIC")
    #             BMSADDR  0x4=Read  0x1000 = start of PIA data page 0x0012=No of registers to read
    m.send_modbus(0x0,     0x4,      0x1000,                         0x0012)
    #             BMSADDR  0x4=Read  0x1100 = start of PIB data page 0x001A=No of registers to read
    m.send_modbus(0x0,     0x4,      0x1100,                         0x001A)
    #             BMSADDR  0x4=ReadC 0x1200 = start of PIC data page 0x0090=No of bits to read
    m.send_modbus(0x0,     0x1,      0x1000,                         0x0090)

def test_result_cb(m, data):
    print("Received data: ", data.hex())

# --------------------------------------------------------------------------- #
# main routine
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    port = '/dev/ttyUSB0'
    m = ModBus(port)
    m.run_with_callbacks(test_result_cb, test_poll_cb, 3)
