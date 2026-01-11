#!/usr/bin/env python

"""
Seplos BMSv3 Reader.

I started looking at https://github.com/ferelarg/Seplos3MQTT, and have used the protocol
documentation from there, but don't think any of the code remains.

But that code is passive only. It listens, but never sends anything down the wire. And if
your Seplos BMS isn't linked in parallel to others (or maybe to an inverter) then it never
broadcasts info spontaneously.

Also we have added the ability to edit parameters over modbus. There is no available
documentation for this, but the field structure in SeplosBattery below serves as a
sort of refernce

---------------------------------------------------------------------------


"""
import signal
import sys
import serial
import configparser
import logging
import paho.mqtt.client as mqtt
import os
import sys
import time
import math

from modbus import ModBus

class logFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno == logging.INFO:
            self._style._fmt = "%(asctime)-15s %(message)s"
        elif record.levelno == logging.DEBUG:
            self._style._fmt = f"%(asctime)-15s \033[36m%(levelname)-8s\033[0m: %(message)s"
        else:   
            color = {
                logging.WARNING: 33,
                logging.ERROR: 31,
                logging.FATAL: 31,
            }.get(record.levelno, 0)
            self._style._fmt = f"%(asctime)-15s \033[{color}m%(levelname)-8s %(threadName)-15s-%(module)-15s:%(lineno)-8s\033[0m: %(message)s"
        return super().format(record)
                
log = logging.getLogger("seplos")

def config(section, name):
    c = configparser.ConfigParser()

    # *.secret.ini in .gitignore to prevent leaking secrets
    c.read('./seplos.secret.ini')
    if len(c.sections())==0:
        c.read('./seplos.ini')
    return c[section][name]
   
def field(page, name, devcls, unit, precision, address, factor, offset, negatives, writeable = False):
    return {"page": page, "name": name, "devcls": devcls, "unit": unit, "precision": precision, "address": address, "factor": factor, "offset": offset, "negatives": negatives, "writeable": writeable}

class SeplosBattery:
    def __init__(self, unitIdentifier, publish_update_cb, modbus):
        log.debug("Create battery info object")
        self.unitIdentifier = unitIdentifier
        self.publish_update_cb = publish_update_cb
        self.silent = False
        self.single_field = None
        self.modbus = modbus
        self.pack_data = {}
        self.old_data = {}
        self.cell_data = {}
        self.fields = [
                #     Pack   Name                        Device Cls     Unit     Prec  i  factor    Offset Negatives
                field("PIA", "Pack Voltage",             "voltage",     "V",     0.01, 0,  1/100.0,       0, False),
                field("PIA", "Current",                  "current",     "A",     0.1,  1,  1/100.0,       0, True),
                field("PIA", "Remaining Capacity",       "",            "Ah",    0.1,  2,  1/100.0,       0, False),
                field("PIA", "Total Capacity",           "",            "Ah",    0.1,  3,  1/100.0,       0, False),
                field("PIA", "Total Discharge Capacity", "",            "Ah",    0.1,  4,       10,       0, False),
                field("PIA", "SOC",                      "",            "%",     0.1,  5,   1/10.0,       0, False),
                field("PIA", "SOH",                      "",            "%",     0.1,  6,   1/10.0,       0, False),
                field("PIA", "Cycles",                   "",            "cycles",  1,  7,        1,       0, False),
                field("PIA", "Average Cell Voltage",     "voltage",     "V",   0.001,  8, 1/1000.0,       0, False),
                field("PIA", "Average Cell Temp",        "temperature", "°C",    0.1,  9,   1/10.0, -273.15, False),
                field("PIA", "Max Cell Voltage",         "voltage",     "V",   0.001, 10, 1/1000.0,       0, False),
                field("PIA", "Min Cell Voltage",         "voltage",     "V",   0.001, 11, 1/1000.0,       0, False),
                field("PIA", "Max Cell Temp",            "temperature", "°C",    0.1, 12,   1/10.0, -273.15, False),
                field("PIA", "Min Cell Temp",            "temperature", "°C",    0.1, 13,   1/10.0, -273.15, False),
                field("PIA", "MaxDisCurt",               "current",     "A",     0.1, 15,        1,       0, False),
                field("PIA", "MaxChgCurt",               "current",     "A",     0.1, 16,        1,       0, False),
                field("PIA", "Power",                    "power",       "W",     0.1, -1,        1,       0, False), # Calculated
                field("PIA", "Cell Delta",               "voltage",     "V",   0.001, -1,        1,       0, False), # Calculated
                field("PIB", "Cell Temp 1",              "temperature", "°C",    0.1, 16,   1/10.0, -273.15, False),
                field("PIB", "Cell Temp 2",              "temperature", "°C",    0.1, 17,   1/10.0, -273.15, False),
                field("PIB", "Cell Temp 3",              "temperature", "°C",    0.1, 18,   1/10.0, -273.15, False),
                field("PIB", "Cell Temp 4",              "temperature", "°C",    0.1, 19,   1/10.0, -273.15, False),

                # PRM is my made up term for the parameters. These are editable, and not mentioned in Seplos modbus
                # docs, but have been reverse engineered from sniffing the protocol. Some of these appear to be
                # duplicated in the info page above (eg. MaxDisCurt in PIA is not editable, but seems to always be
                # the same as Discharge Request Current).
                # Descriptions are as per Seplos software. Don't play with these unless you know what you're doing you can
                # easily destroy your battery, or start a fire, with the wrong settings here.
                # As a precaution lets always do a read before any set, and refuse to change if significant differences.
                field("PRM", "Battery high voltage recovery",               "voltage",      "V", 0.01, 0x02, 1/100.0, 0, False, True), # 54.00
                field("PRM", "Battery high voltage alarm",                  "voltage",      "V", 0.01, 0x03, 1/100.0, 0, False, True), # 56.00
                field("PRM", "Battery over voltage recovery",               "voltage",      "V", 0.01, 0x04, 1/100.0, 0, False, True), # 54.00
                field("PRM", "Battery over voltage protection",             "voltage",      "V", 0.01, 0x05, 1/100.0, 0, False, True), # 57.60
                field("PRM", "Battery low voltage recovery",                "voltage",      "V", 0.01, 0x06, 1/100.0, 0, False, True), # 48.00
                field("PRM", "Battery low voltage alarm",                   "voltage",      "V", 0.01, 0x07, 1/100.0, 0, False, True), # 46.40
                field("PRM", "Battery under voltage recovery",              "voltage",      "V", 0.01, 0x08, 1/100.0, 0, False, True), # 48.00
                field("PRM", "Battery under voltage protection",            "voltage",      "V", 0.01, 0x09, 1/100.0, 0, False, True), # 43.20
                field("PRM", "Cell high voltage recovery",                  "voltage",      "V", 0.001, 0x0A, 1/1000.0, 0, False, True), # 3.400
                field("PRM", "Cell high voltage alarm",                     "voltage",      "V", 0.001, 0x0B, 1/1000.0, 0, False, True), # 3.500
                field("PRM", "Cell over voltage recovery",                  "voltage",      "V", 0.001, 0x0C, 1/1000.0, 0, False, True), # 3.400
                field("PRM", "Cell over voltage protection",                "voltage",      "V", 0.001, 0x0D, 1/1000.0, 0, False, True), # 3.650
                field("PRM", "Cell low voltage recovery",                   "voltage",      "V", 0.001, 0x0E, 1/1000.0, 0, False, True), # 3.100
                field("PRM", "Cell low voltage alarm",                      "voltage",      "V", 0.001, 0x0F, 1/1000.0, 0, False, True), # 2.900
                field("PRM", "Cell under voltage recovery",                 "voltage",      "V", 0.001, 0x10, 1/1000.0, 0, False, True), # 3.100
                field("PRM", "Cell under voltage protection",               "voltage",      "V", 0.001, 0x11, 1/1000.0, 0, False, True), # 2.700
                field("PRM", "Cell under voltage failure",                  "voltage",      "V", 0.001, 0x12, 1/1000.0, 0, False, True), # 2.000
                field("PRM", "Cell diff pressure protection",               "voltage",      "V", 0.001, 0x13, 1/1000.0, 0, False, True), # 1.000
                field("PRM", "Diff pressure protection recovery",           "voltage",      "V", 0.001, 0x14, 1/1000.0, 0, False, True), # 0.500
                field("PRM", "Charge over current recovery",                "current",      "A", 1, 0x15, 1, 0, False, True), # 203
                field("PRM", "Charge over current alarm",                   "current",      "A", 1, 0x16, 1, 0, False, True), # 205
                field("PRM", "Charge over current protection",              "current",      "A", 1, 0x17, 1, 0, False, True), # 210
                field("PRM", "Charge over current delay",                   "",             "s", 0.1, 0x18, 1/10.0, 0, False, True), # 10.0
                field("PRM", "Secondary charge over current protection",    "current",      "A", 1, 0x19, 1, 0, False, True), # 300
                field("PRM", "Secondary charge over current delay",         "",             "s", 1, 0x1A, 1, 0, False, True), # 300
                field("PRM", "Discharge over current recovery",             "current",      "A", 1, 0x1B, 1, 0, True, True), # -203
                field("PRM", "Discharge over current alarm",                "current",      "A", 1, 0x1C, 1, 0, True, True), # -205
                field("PRM", "Discharge over current protection",           "current",      "A", 1, 0x1D, 1, 0, True, True), # -210
                field("PRM", "Discharge over current delay",                "",             "s", 0.1, 0x1E, 1/10.0, 0, False, True), # 10.0
                field("PRM", "Secondary discharge over current protection", "current",      "A", 1, 0x1F, 1, 0, True, True), # -350
                field("PRM", "Secondary discharge over current  delay",     "",             "s", 1, 0x20, 1, 0, False, True), # 300
                field("PRM", "Over current recovery delay",                 "",             "s", 0.1, 0x23, 1/10.0, 0, False, True), # 60.0
                field("PRM", "Number of over current lock times",           "",              "", 0.1, 0x24, 1, 0, False, True), # 5
                field("PRM", "Pluse current limiting current",              "current",      "A", 0.1, 0x26, 1, 0, False, True), # 205
                field("PRM", "Precharge over time",                         "",             "s", 0.1, 0x2E, 1/10.0, 0, False, True), # 3.0
                field("PRM", "Charge high temperature recovery",            "temperature", "°C", 0.1, 0x2F, 1/10.0, -273.15, False, True), # 47.0
                field("PRM", "Charge high temperature alarm",               "temperature", "°C", 0.1, 0x30, 1/10.0, -273.15, False, True), # 50.0
                field("PRM", "Charge over temperature recovery",            "temperature", "°C", 0.1, 0x31, 1/10.0, -273.15, False, True), # 50.0
                field("PRM", "Charge over temperature protection",          "temperature", "°C", 0.1, 0x32, 1/10.0, -273.15, False, True), # 55.0
                field("PRM", "Charge low temperature recovery",             "temperature", "°C", 0.1, 0x33, 1/10.0, -273.15, False, True), # 5.0
                field("PRM", "Charge low temperature alarm",                "temperature", "°C", 0.1, 0x34, 1/10.0, -273.15, False, True), # 2.0
                field("PRM", "Charge under temperature recovery",           "temperature", "°C", 0.1, 0x35, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Charge under temperature protection",         "temperature", "°C", 0.1, 0x36, 1/10.0, -273.15, False, True), # -10.0
                field("PRM", "Discharge high temperature recovery",         "temperature", "°C", 0.1, 0x37, 1/10.0, -273.15, False, True), # 50.0
                field("PRM", "Discharge high temperature alarm",            "temperature", "°C", 0.1, 0x38, 1/10.0, -273.15, False, True), # 55.0
                field("PRM", "Discharge over temperature recovery",         "temperature", "°C", 0.1, 0x39, 1/10.0, -273.15, False, True), # 55.0
                field("PRM", "Discharge over temperature protection",       "temperature", "°C", 0.1, 0x3A, 1/10.0, -273.15, False, True), # 60.0
                field("PRM", "Discharge low temperature recovery",          "temperature", "°C", 0.1, 0x3B, 1/10.0, -273.15, False, True), # 3.0
                field("PRM", "Discharge low temperature alarm",             "temperature", "°C", 0.1, 0x3C, 1/10.0, -273.15, False, True), # -10.0
                field("PRM", "Discharge under temperature recovery",        "temperature", "°C", 0.1, 0x3D, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Discharge under temperature protection",      "temperature", "°C", 0.1, 0x3E, 1/10.0, -273.15, False, True), # -15.0
                field("PRM", "High ambient temperature recovery",           "temperature", "°C", 0.1, 0x3F, 1/10.0, -273.15, False, True), # 47.0
                field("PRM", "High ambient temperature alarm",              "temperature", "°C", 0.1, 0x40, 1/10.0, -273.15, False, True), # 50.0
                field("PRM", "Over ambient temperature recovery",           "temperature", "°C", 0.1, 0x41, 1/10.0, -273.15, False, True), # 55.0
                field("PRM", "Over ambient temperature protection",         "temperature", "°C", 0.1, 0x42, 1/10.0, -273.15, False, True), # 60.0
                field("PRM", "Low ambient temperature recovery",            "temperature", "°C", 0.1, 0x43, 1/10.0, -273.15, False, True), # 3.0
                field("PRM", "Low ambient temperature alarm",               "temperature", "°C", 0.1, 0x44, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Under ambient temperature recovery",          "temperature", "°C", 0.1, 0x45, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Under ambient temperature protection",        "temperature", "°C", 0.1, 0x46, 1/10.0, -273.15, False, True), # -10.0
                field("PRM", "Power high temperature recovery",             "temperature", "°C", 0.1, 0x47, 1/10.0, -273.15, False, True), # 85.0
                field("PRM", "power high temperature alarm",                "temperature", "°C", 0.1, 0x48, 1/10.0, -273.15, False, True), # 95.0
                field("PRM", "Power over temperature recovery",             "temperature", "°C", 0.1, 0x49, 1/10.0, -273.15, False, True), # 85.0
                field("PRM", "Power over temperature protection",           "temperature", "°C", 0.1, 0x4A, 1/10.0, -273.15, False, True), # 110.0
                field("PRM", "Temperature regulate stop",                   "temperature", "°C", 0.1, 0x4B, 1/10.0, -273.15, False, True), # 10.0
                field("PRM", "Temperature regulate open",                   "temperature", "°C", 0.1, 0x4C, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Equalization high temperature prohibition",   "temperature", "°C", 0.1, 0x4D, 1/10.0, -273.15, False, True), # 50.0
                field("PRM", "Equalization low temperature prohibition",    "temperature", "°C", 0.1, 0x4E, 1/10.0, -273.15, False, True), # 0.0
                field("PRM", "Static equalization timing",                  "", "", 0.1, 0x4F, 1, 0, False, True), # 10
                field("PRM", "Equalization open voltage",                   "voltage",      "V", 0.001, 0x50, 1/1000.0, 0, False, True), # 3.400
                field("PRM", "Equalization open difference pressure",       "voltage",      "V", 0.001, 0x51, 1/1000.0, 0, False, True), # 0.050
                field("PRM", "Equalization stop difference pressure",       "voltage",      "V", 0.001, 0x52, 1/1000.0, 0, False, True), # 0.030
                field("PRM", "Power supply SOC",                            "",             "%", 0.1, 0x53, 1/10.0, 0, False, True), # 96.0
                field("PRM", "SOC low recovery",                            "",             "%", 0.1, 0x54, 1/10.0, 0, False, True), # 15.0
                field("PRM", "SOC low alarm",                               "",             "%", 0.1, 0x55, 1/10.0, 0, False, True), # 10.0
                field("PRM", "SOC protection recovery",                     "",             "%", 0.1, 0x56, 1/10.0, 0, False, True), # 5.0
                field("PRM", "SOC low protection",                          "",             "%", 0.1, 0x57, 1/10.0, 0, False, True), # 2.0
                field("PRM", "Rated battery capacity",                      "",            "Ah", 0.01, 0x58, 1/100.0, 0, False, True), # 314.00
                field("PRM", "Stand-by time",                               "",             "h", 0.1, 0x5B, 1, 0, False, True), # 48
                field("PRM", "Forced output delay",                         "",             "s", 0.1, 0x5C, 1/10.0, 0, False, True), # 6.0
                field("PRM", "Compensation site 1",                         "", "", 0.1, 0x5F, 1, 0, False, True), # 7
                field("PRM", "Compensation site 1 resistance",              "", "", 0.1, 0x60, 1, 0, False, True), # 0.0
                field("PRM", "Compensating site 2",                         "", "", 0.1, 0x61, 1, 0, False, True), # 13
                field("PRM", "Compensation site 2 resistance",              "", "", 0.1, 0x62, 1, 0, False, True), # 0.0
                field("PRM", "Cell diff pressure alarm",                    "", "", 0.1, 0x63, 1, 0, False, True), # 150
                field("PRM", "Diff pressure alarm recovery",                "", "", 0.1, 0x64, 1, 0, False, True), # 100
                field("PRM", "Charging request voltage",                    "voltage",      "V", 0.01, 0x65, 1/100.0, 0, False, True), # 56.00
                field("PRM", "Charging request current",                    "current",      "A", 0.1, 0x66, 1, 0, False, True), # 50
                field("PRM", "Discharge request current",                   "current",      "A", 0.1, 0x67, 1, 0, True, True), # -62

                ]
        for i in range(1, 17):
            self.fields.append(field("PIB", f"Cell {i}", "voltage", "V", 0.001, i-1, 1/1000.0, 0, False))

    # Return a list of field names that can be read from
    def read_fields(self):
        return [ self.to_lower_under(f["name"]) for f in self.fields ]

    # Return a list of field names that can be written to
    def write_fields(self):
        return [ self.to_lower_under(f["name"]) for f in self.fields if f["writeable"]]

    # Call back cb_sensor with details of each read_only field.
    def autodiscovery(self, cb_sensor, cb_number=False):
        for f in self.fields:
            if not f["writeable"]:
                cb_sensor(f["devcls"], "measurement", f["unit"], f["name"], self.to_lower_under (f["name"]), self.unitIdentifier, f["precision"])
            else:
                log.debug(f"Skip publishing of writable number {f["name"]}")

    def batteryIdFromModbus(b: bytearray) -> int:
        return b[0]

    def parse_modbus(self, b: bytearray):
        if len(b)<3:
            log.error("Can't parse a modbus frame this short. Abandoning")
            return
        if b[0]!=self.unitIdentifier:
            log.error(f"Error: parsing someone elses info. We are {self.unitIdentifier}, but we received {b[0]}")
            return
        if b[1]==0x04:
            # Read data.
            data_len = b[2]
            if len(b) != 3 + data_len + 2: # ID 0x04 n fields CRC1 CRC2
                log.error(f"Malformed modbus message. Aborting. Len is {len(b)}. Fields = {data_len}. 3+data_len + 2 = {3 + data_len+2}")
                return
            uint32s = []
            for i in range(3, len(b)-1, 2):
                uint32s.append((b[i] << 8) | b[i + 1])

            # The data packs PIA and PIB are only distinguishable by their lengths (and which response we're waiting for).
            if data_len == 36:
                self.decodeMainPackInfo(uint32s)
            if data_len == 52:
                self.decodeCellInfo(uint32s)
            if data_len == 210:
                self.decodeParams(uint32s)
            if data_len == 2: # single field
                # id rd A         CRC
                # 00 04 02 0c d1 41 ac
                v = uint32s[0]
                if self.single_field["negatives"]:
                    v = v if v <= 32767 else v - 65536
                v *= self.single_field["factor"]
                v += self.single_field["offset"]
                v = round(v, -int(math.log10(self.single_field["precision"])))

                k = self.to_lower_under(self.single_field["name"])
                self.pack_data[k] = v # haven't decided if this is helpful.

                log.info(f"{self.single_field["name"]} = {v} (0x{b.hex()})")
                self.single_field = None

    def writeFieldModbus(self, fieldName: str, value: float, unsafe = False):
        f = None
        for x in self.fields:
            if self.to_lower_under(x["name"])==fieldName:
                f = x
        if f == None:
            log.error(f"Didn't find field: {fieldName}")
            return
        if f["page"] != "PRM":
            return
        page = 0x13

        if not unsafe:
            if fieldName not in self.pack_data:
                log.warning("Please do a read of this field first so we can sense check the new value")
                return

            oldval = float(self.pack_data[fieldName])
            if oldval == value:
                log.info("No need to update - no change to value")
                return

            if value == 0 or oldval == 0:
                log.error("Not updating to 0 as a precaution in case something went wrong")
                return

            r = oldval/value if oldval>value else value/oldval
            r = (r-1) * 100
            if r>20:
                log.error(f"Not updating field as change too big")
                return

        f = self.fieldByName(fieldName)
        if f==None:
            log.error(f"Failed to find field to update it")
            return
        v = value + f["offset"]
        v /= f["factor"]
        if f["negatives"]:
            v = v if v >= 0 else v + 65536
        if v>0xFFFF or v<0:
            log.error(f"Not updating - value out of range: {v:x}")
        vi = int(v)
            
        v1 = (vi>>8) & 0xFF
        v2 = vi & 0xFF

        #                                    ID    Write Address       Write one Reg   2 data bytes  byte1  byte2
        b = bytearray( [self.unitIdentifier, 0x10, 0x13, f["address"], 0x00, 0x01,       0x02,          v1,    v2])
        log.debug(f"Sending 0x{b.hex()} to modbus.")
        self.modbus.send(b)

    def fieldByName(self, fieldName: str):
        for f in self.fields:
            if self.to_lower_under(f["name"])==fieldName:
                return f
        return None
 
    def readFieldModbus(self, fieldName: str):
        f = self.fieldByName(fieldName)
        if f == None:
            log.error(f"Didn't find field: {fieldName}")
            return
        page = 0x0
        if f["page"] == "PIA":
            return # Implement this later - not a priority
        if f["page"] == "PIB":
            return # Implement this later - not a priority
        if f["page"] == "PRM":
            page = 0x13
        b = bytearray( [self.unitIdentifier, 0x04, page, f["address"], 0x00, 0x01] )
        log.info(f"Send Data: 0x{b.hex()}")
        self.single_field = f
        self.modbus.send(b)

    def readCachedField(self, fieldName: str) -> float or None:
        if fieldName in self.pack_data:
            return float(self.pack_data[fieldName])
        return None

    # We can reduce network traffic (and Homeassistant data space when pushing there with MQTT) by only 
    # sending changed values. Some stuff (eg. no of cells) will probably never change. 
    def needsPublishing(self, k):
        if k not in self.old_data:
            return True
        v = self.pack_data[k]
        oldv = self.old_data[k]
        if v==oldv:
            return False
        if abs(v-oldv) < 0.003: # Cell voltages fluctuate by about this.
            return False
        return True

    # Reset data cache, so that all values will be published next time.
    # We do this periodically.
    def forcePublishAll(self):
        self.old_data = {}

    def publishUpdate(self, k):
        if self.needsPublishing(k):
            #mqtt_hass.publish(f"{mqtt_prefix}/battery_{self.unitIdentifier}/{k}", self.pack_data[k], retain=True)
            self.publish_update_cb(self.unitIdentifier, k, self.pack_data[k])
            s = f"{self.old_data[k]} --> " if k in self.old_data else ""
            self.old_data[k] = self.pack_data[k]
            if not self.silent:
                print(f"{k}: {s}{self.pack_data[k]}")

    # Use field description to read the data from the modbus buffer, and store it in our pack_data.
    # Return the key we stored it under.
    def recordData(self, modbusBuffer, fieldDesc):
        v = modbusBuffer[fieldDesc["address"]]
        if fieldDesc["negatives"]:
            v = v if v <= 32767 else v - 65536
        v *= fieldDesc["factor"]
        v += fieldDesc["offset"]
        v = round(v, -int(math.log10(fieldDesc["precision"])))

        k = self.to_lower_under(fieldDesc["name"])
        self.pack_data[k] = v
        return k
 
    def decodeMainPackInfo(self, modbusBuffer):
        for f in filter(lambda f: f["page"]=="PIA" and f["address"] >= 0, self.fields):
            k = self.recordData(modbusBuffer, f)
            self.publishUpdate(k)

        self.pack_data["power"] = -round(self.pack_data["current"] * self.pack_data["pack_voltage"], 2)
        self.pack_data["cell_delta"] = round((self.pack_data["max_cell_voltage"] - self.pack_data["min_cell_voltage"]), 3)
        self.publishUpdate("power")
        self.publishUpdate("cell_delta")

    def decodeCellInfo(self, modbusBuffer):
        for f in filter(lambda f: f["page"]=="PIB" and f["address"] >= 0, self.fields):
            k = self.recordData(modbusBuffer, f)
            self.publishUpdate(k)
    
    def decodeParams(self, modbusBuffer):
        for f in filter(lambda f: f["page"]=="PRM" and f["address"] >= 0, self.fields):
            k = self.recordData(modbusBuffer, f)
            self.publishUpdate(k)
            # log.warning(f"{k}: {self.pack_data[k]}")
    
    def to_lower_under(self, text):
        text = text.lower()
        text = text.replace(' ', '_')
        return text

class SeplosModbusMqttBridge:
    def __init__(self):

        serial = config("seplos", "serial")
        mqtt_server = config("mqtt", "server")
        mqtt_port = int(config("mqtt", "port"))
        mqtt_user = config("mqtt", "user")
        mqtt_pass = config("mqtt", "pass")
        mqtt_prefix = config("mqtt", "prefix")

        self.modbus = ModBus(serial)
        self.battery_data = {}
        self.update_cb = None
        self.first_poll = True

        self.mqtt_hass = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.mqtt_hass.username_pw_set(username=mqtt_user, password=mqtt_pass)
        try:
            log.info(f"Opening MQTT connection, server: {mqtt_server}\tport: {mqtt_port}")
            self.mqtt_hass.connect(mqtt_server, mqtt_port) 
            self.mqtt_hass.loop_start()
        except ConnectionRefusedError:
            log.error("Error: Unable to connect to MQTT server.")
        except Exception as e:
            log.error(f"MQTT Unexpected error: {str(e)}")
        self.mqtt_prefix = mqtt_prefix
    
    # Our poll function sends 3 messages. Can reduce poll frequency a little,
    # but not below 1s or our modbus implementation won't keep up.
    def run(self):
        self.modbus.run_with_callbacks(self.modbus_data, self.poll, 2)

    async def run_async(self):
        await self.modbus.run(self.modbus_data, self.poll, 2)

    def set_update_cb(self, update_cb):
        self.update_cb  = update_cb

    def available_batteries(self):
        return [k for k in self.battery_data]

    def getBattery(self, bid) -> SeplosBattery or None:
        return self.battery_data[bid] if bid in self.battery_data else None

    def readFieldModbus(self, batteryid, fieldname: str):
        self.battery_data[batteryid].readFieldModbus(fieldname)

    def writeFieldModbus(self, batteryid, fieldname: str, value: float):
        self.battery_data[batteryid].writeFieldModbus(fieldname, value)

    def modbus_data(self, modbus, data):
        # print("Got data", data.hex())
        bid = SeplosBattery.batteryIdFromModbus(data)
        bids = f"b{bid}"

        if bids not in self.battery_data:
            self.battery_data[bids] = SeplosBattery(bid, self.receiveUpdate, self.modbus)
            self.battery_data[bids].autodiscovery(self.publish_sensor_autodiscovery)
            log.info(f"Sending online signal for Battery {bid}")
            self.mqtt_hass.publish(f"{self.mqtt_prefix}/battery_{bid}/state", "online", retain=True)

        self.battery_data[bids].parse_modbus(data)

    def publish_sensor_autodiscovery(self, dev_cla, state_class, sensor_unit, sensor_name, name_under, batt_number, precision=0):
        log.debug(f"Autodiscovery publish: {name_under} {state_class} {sensor_unit} {precision}")
        if dev_cla != "": dev_cla = f""" "dev_cla": "{dev_cla}", """
        if state_class != "": state_class = f""" "stat_cla": "{state_class}", """
        if sensor_unit != "": sensor_unit = f""" "unit_of_meas": "{sensor_unit}", """

        precisions = ""
        if precision != 0:
            precisions = f""" "suggested_display_precision": {precision}, """
        mqtt_packet = f"""
                        {{	 
                            "name": "{sensor_name}",
                            "stat_t": "{self.mqtt_prefix}/battery_{batt_number}/{name_under}",
                            "avty_t": "{self.mqtt_prefix}/battery_{batt_number}/state",
                            "uniq_id": "seplos_battery_{batt_number}_{name_under}",
                            {dev_cla}
                            {sensor_unit}
                            {state_class}
                            {precisions}
                            "dev": {{
                                "ids": "seplos_battery_{batt_number}",
                                "name": "Seplos BMS {batt_number}",
                                "sw": "seplos-bms-console 1.0",
                                "mdl": "Seplos BMSv3 MQTT",
                                "mf": "Seplos"
                                }},
                            "origin": {{
                                "name":"seplos-bms-console",
                                "sw": "1.0",
                                "url": "https://github.com/hollo42/seplose-bms-console/"
                            }}
                        }}
                        """

        # print(mqtt_packet)
        self.mqtt_hass.publish(f"homeassistant/sensor/seplos_bms_{batt_number}/{name_under}/config", mqtt_packet, retain=True)

    # N.B. If adding additional poll messages then we may need to change frequency in run_with_callbacks / run_async
    def poll(self, m):
        if self.first_poll:
            self.requestParams(m)
            self.first_poll = False
        # print("Sending requests for PIA, PIB and PIC")
        #             BMSADDR  0x4=Read  0x1000 = start of PIA data page 0x0012=No of registers to read
        m.send_modbus(0x0,     0x4,      0x1000,                         0x0012)
        #             BMSADDR  0x4=Read  0x1100 = start of PIB data page 0x001A=No of registers to read
        m.send_modbus(0x0,     0x4,      0x1100,                         0x001A)
        #             BMSADDR  0x4=ReadC 0x1200 = start of PIC data page 0x0090=No of bits to read
        m.send_modbus(0x0,     0x1,      0x1000,                         0x0090)

    def requestParams(self, m):
        #             BMSADDR  0x4=Read  0x1300 = start of PRM data page 0x0069=No of registers to read
        m.send_modbus(0x0,     0x4,      0x1300,                         0x0069)


    def receiveUpdate(self, uid, k, value):
        self.mqtt_hass.publish(f"{self.mqtt_prefix}/battery_{uid}/{k}", value, retain=True)
        if self.update_cb != None:
            self.update_cb(uid, k, value)

# This class implements a command line interface to the Seplos battery for displaying and
# setting BMS info/parameters. No MQTT involed here.
class SeplosCmdline:
    def __init__(self):
        self.serial = config("seplos", "serial")
        self.battery_data = {}
        self.modbus = ModBus(self.serial)
        self.battery = SeplosBattery(0, self.receiveBatteryUpdate, self.modbus)
        self.battery.silent = True # Lets not clutter command line.

    def usage(self):
        print(f"Usage: {sys.argv[0]} [OPTION] [ARGS]...")
        print("Show and edit information about a seplos BMS, and mirror to mqtt")
        print("Call with no args to run as an mqtt mirror. Or with one of the following:")
        print("  -h, --help             show this text")
        print("  -l, --list             show list of available variables")
        print("  -p, --params           show list of available parameters")
        print("  -s  parameter|var      show current value of a parameter or variable")
        print("  -a  --all              show values of all parameters and variables")
        print("  -e  parameter value    edit parameter to new value")
        print("")
        print("There is a distinction between read-only variables and read-write parameters.")
        print("Values are what we transmit with mqtt. Examples are state of charge, or pack")
        print("voltage. Parameters are editable, and are not usually transmitted on mqtt.")
        print("These include settings like protection voltages, overall pack capacity etc.")
        print("Some parameters are exposed as values by Seplos. For example the remaining")
        print("capacity can be set, but is also emitted as a value.")
        print("")
        print("-l -p -s and -a should be safe. -e could brick your bms, or be used to")
        print("set parameters to dangerous values")

    # Check sys.argv[2] exists and is a valid field name for the requested operation.
    # Also retrieve the relevant field info and store to self.field
    def check_arg(self):
        if len(sys.argv) < 3:
            print(f"{sys.argv[1]} expects an argument")
            exit(-1)
        self.arg = sys.argv[2]
        self.field = self.battery.fieldByName(self.arg)
        if self.field==None:
            print(f"unrecognised field name {self.arg}")
            exit(-1)
        if sys.argv[1]=="-e":
            if self.field["page"] != "PRM":
                print(f"Cannot edit field {self.arg}")
                exit(-1)
            if len(sys.argv) < 4:
                print(f"-e expects two arguments")
                exit(-1)
            self.newval = float(sys.argv[3])
        self.arg = sys.argv[2]

    def run(self):
        self.poll_count = 0
        match sys.argv[1]:
            case "-h" | "--help":
                self.usage()
            case "-l" | "--list":
                self.list()
            case "-p" | "--params":
                self.list()
            case "-s":
                self.check_arg()
                self.run_modbus(self.poll_read_field)
            case "-a":
                self.run_modbus(self.poll_read_all, 1) # Needs a longer timeout
            case "-e":
                self.check_arg()
                self.run_modbus(self.poll_edit)
            case _:
                self.usage()
                exit(-1)

    def list(self):
        for f in self.battery.read_fields():
            print(f)
        exit(0)

    def params(self):
        for f in self.battery.write_fields():
            print(f)
        exit(0)

    def run_modbus(self, pollfn, pollfreq=0.1):
        self.modbus.run_with_callbacks(self.receive_modbus_data, pollfn, pollfreq)

    # This is where we receive data from modbus. Check battery id = 0, and then
    # send to battery for processing.
    def receive_modbus_data(self, modbus, data):
        bid = SeplosBattery.batteryIdFromModbus(data)
        if bid != 0:
            print("Error: Unexpected battery with ID other than 0.")
            print("We can only handle a single battery at present.")
            exit(-1)
        self.battery.parse_modbus(data)

    # We then receive processed modbus data back from the battery.
    # For simplicity we just store it and move on.
    # All logic for what to do with the data is therefore in the poll function.
    def receiveBatteryUpdate(self, uid, k, value):
        self.battery_data[k] = value

    # Now we have our various poll functions. As modbus data is received picemeal
    # via callbacks this is where we need to send requests, and decide whether we
    # have everything we need, and print output and quit as appropriate.
    def poll_read_field(self, m):
        self.poll_count += 1
        if self.poll_count == 1:
            self.send_modbus_request(self.field["page"])
            return

        if self.arg in self.battery_data:
            print(f"{self.arg} {self.battery_data[self.arg]} {self.field["unit"]}")
            exit(0)
        if self.poll_count > 50:
            print("error: timeout waiting for field result")
            exit(-1)

    def poll_read_all(self, m):
        self.poll_count += 1
        if self.poll_count == 1:
            self.send_modbus_request("all")
            self.result_count = 0
            return

        if self.poll_count > 50:
            print("error: timeout waiting for results")
            exit(-1)

        if len(self.battery_data) > self.result_count:
            self.result_count = len(self.battery_data)
            return # Still receiving data.

        for f in self.battery.fields:
            k = self.battery.to_lower_under(f["name"])
            if k in self.battery_data:
                print(f"{k} {self.battery_data[k]} {f["unit"]}")
        exit(0)

    def poll_edit(self, m):
        self.poll_count += 1
        if self.poll_count == 1:
            self.edit_awaits = "read"
            self.send_modbus_request("PRM")
            return

        if self.poll_count > 50:
            print("error: timeout waiting for results")
            if self.edit_awaits == "check_read":
                print("It is possible that the edit completed and we lost connection while trying to confirm the value")
            exit(-1)

        if self.edit_awaits == "read" and self.arg in self.battery_data:
            print(f"\nWe will change {self.arg} from {self.battery_data[self.arg]} {self.field["unit"]} to {self.newval} {self.field["unit"]}\n")
            check = input("If this looks right and you wish to go ahead then type 'yes'. All other values abort: ")
            if check != 'yes':
                print("Aborting edit")
                exit(0)
            del self.battery_data[self.arg]
            self.battery.writeFieldModbus(self.arg, self.newval, True)
            print("Update sent over modbus")
            self.send_modbus_request("PRM")
            self.edit_awaits = "check_read"

        if self.edit_awaits == "check_read" and self.arg in self.battery_data:
            print(f"Write completed. New value of {self.arg} is confirmed as {self.battery_data[self.arg]} {self.field["unit"]}")
            exit(0)
 
    def send_modbus_request(self, page):
        if page=="all" or page=="PIA":
            #             BMSADDR  0x4=Read  0x1000 = start of PIA data page 0x0012=No of registers to read
            self.modbus.send_modbus(0x0,     0x4,      0x1000,                         0x0012)
        if page=="all" or page=="PIB":
            #             BMSADDR  0x4=Read  0x1100 = start of PIB data page 0x001A=No of registers to read
            self.modbus.send_modbus(0x0,     0x4,      0x1100,                         0x001A)
        if page=="all" or page=="PRM":
            #             BMSADDR  0x4=Read  0x1300 = start of PRM data page 0x0069=No of registers to read
            self.modbus.send_modbus(0x0,     0x4,      0x1300,                         0x0069)

# --------------------------------------------------------------------------- #
# main routine
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.setFormatter(logFormatter())
    log.addHandler(handler)
    log.debug("Starting in non interactive mode")

    if len(sys.argv)>1:
        SeplosCmdline().run()
    else:
        log.setLevel(logging.DEBUG)
        smmb = SeplosModbusMqttBridge()
        smmb.run()
